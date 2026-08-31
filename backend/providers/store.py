"""Last-known-good market data.

When a vendor cannot be reached, the app shows the most recent REAL data it
has, labelled with its age. It never fabricates a price. This store is only
ever written from a successful live fetch, so everything it can serve actually
traded at some point.

Entries are kept indefinitely: a two-day-old close is still a fact, and it is
the user's call whether that is useful. Staleness is reported, never hidden.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone

from ..db import connect
from .base import Bar, Bars, OptionChain, OptionContract, Quote

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        stamp = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).total_seconds()


def describe_age(iso: str | None) -> str:
    """Human phrasing for how old a cached value is."""
    seconds = age_seconds(iso)
    if seconds is None:
        return "unknown age"
    if seconds < 90:
        return "moments ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------
def _encode(value) -> str:
    if isinstance(value, Quote):
        return json.dumps({"type": "quote", "data": asdict(value)})
    if isinstance(value, Bars):
        return json.dumps({"type": "bars", "symbol": value.symbol,
                           "data": [asdict(b) for b in value.bars]})
    if isinstance(value, OptionChain):
        return json.dumps({
            "type": "chain", "underlying": value.underlying,
            "expiration": value.expiration, "underlying_price": value.underlying_price,
            "calls": [asdict(c) for c in value.calls],
            "puts": [asdict(c) for c in value.puts],
        })
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return json.dumps({"type": "strings", "data": value})
    raise TypeError(f"cannot cache {type(value).__name__}")


def _decode(raw: str):
    payload = json.loads(raw)
    kind = payload.get("type")
    if kind == "quote":
        return Quote(**payload["data"])
    if kind == "bars":
        return Bars(payload["symbol"], [Bar(**b) for b in payload["data"]])
    if kind == "chain":
        return OptionChain(
            payload["underlying"], payload["expiration"], payload["underlying_price"],
            [OptionContract(**c) for c in payload["calls"]],
            [OptionContract(**c) for c in payload["puts"]],
        )
    if kind == "strings":
        return payload["data"]
    raise ValueError(f"unknown cached type {kind!r}")


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
def put(key: str, kind: str, value, symbol: str | None = None,
        source: str | None = None) -> None:
    try:
        payload = _encode(value)
    except TypeError as exc:
        log.debug("not caching %s: %s", key, exc)
        return
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO market_cache (key, kind, symbol, payload, source, fetched_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
                "source=excluded.source, fetched_at=excluded.fetched_at",
                (key, kind, symbol, payload, source, _now()),
            )
    except Exception as exc:                                    # noqa: BLE001
        log.debug("cache write failed for %s: %s", key, exc)


def get(key: str) -> tuple[object, str] | None:
    """Return (value, fetched_at) for a cached entry, or None."""
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT payload, fetched_at, source FROM market_cache WHERE key=?", (key,)
            ).fetchone()
    except Exception as exc:                                    # noqa: BLE001
        log.debug("cache read failed for %s: %s", key, exc)
        return None
    if row is None:
        return None
    try:
        return _decode(row["payload"]), row["fetched_at"]
    except Exception as exc:                                    # noqa: BLE001
        log.debug("cache decode failed for %s: %s", key, exc)
        return None


def get_quote(symbol: str) -> tuple[Quote, str] | None:
    hit = get(f"quote:{symbol.upper()}")
    if hit and isinstance(hit[0], Quote):
        return hit                                              # type: ignore[return-value]
    return None


def put_quote(quote: Quote, source: str | None = None) -> None:
    put(f"quote:{quote.symbol.upper()}", "quote", quote, quote.symbol.upper(), source)


def stats() -> dict:
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT kind, COUNT(*) AS n, MAX(fetched_at) AS newest "
                "FROM market_cache GROUP BY kind"
            ).fetchall()
    except Exception:                                           # noqa: BLE001
        return {}
    return {r["kind"]: {"entries": r["n"], "newest": r["newest"]} for r in rows}


def clear() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM market_cache")
