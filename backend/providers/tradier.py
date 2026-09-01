"""Tradier provider - real-time quotes and exchange-published option greeks.

This is the recommended source for the short-dated trades this app targets:
Tradier returns live NBBO bid/ask plus greeks/IV computed by ORATS, so the
scanner is not guessing at volatility from stale last-trade prices.

A free developer token (sandbox) gives delayed data; a brokerage account token
gives real-time.  Set TRADIER_TOKEN and, for sandbox, point TRADIER_BASE_URL at
https://sandbox.tradier.com/v1.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import httpx

from .. import market_hours
from ..analytics import blackscholes as bs
from .base import Bar, Bars, NewsItem, OptionChain, OptionContract, Quote

log = logging.getLogger(__name__)


def _f(value) -> float | None:
    """Coerce to float, mapping Tradier's nulls and empty strings to None."""
    if value in (None, "", "NaN"):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None      # drop NaN


class TradierProvider:
    name = "tradier"

    def __init__(self, token: str, base_url: str = "https://api.tradier.com/v1",
                 risk_free: float = 0.04, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.realtime = "sandbox" not in self.base_url
        self.risk_free = risk_free
        self.last_error: str | None = None
        self._exp_cache: dict[str, tuple[float, list[str]]] = {}
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        try:
            r = await self._client.get(f"{self.base_url}{path}", params=params or {})
        except httpx.HTTPError as exc:
            log.debug("tradier request failed %s: %s", path, exc)
            return None
        if r.status_code != 200:
            log.debug("tradier %s -> HTTP %s: %s", path, r.status_code, r.text[:200])
            return None
        try:
            return r.json()
        except ValueError:
            return None

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        if not symbols:
            return {}
        data = await self._get("/markets/quotes", {"symbols": ",".join(symbols), "greeks": "false"})
        rows = ((data or {}).get("quotes") or {}).get("quote")
        if rows is None:
            return {}
        if isinstance(rows, dict):
            rows = [rows]

        session = market_hours.session()
        out: dict[str, Quote] = {}
        for row in rows:
            quote = self._quote_from_row(row, session)
            if quote:
                out[quote.symbol] = quote
        return out

    def _quote_from_row(self, row: dict, session: str) -> Quote | None:
        """Split Tradier's single `last` into regular and extended prices.

        Tradier reports one last-trade price that includes extended-hours
        prints, with no field marking which session it came from. The split is
        inferred from the clock plus the two reference prices the payload does
        carry: `close` (today's regular close, populated only once the session
        has ended) and `prevclose`.
        """
        symbol = row.get("symbol")
        if not symbol:
            return None
        last = _f(row.get("last"))
        regular_close = _f(row.get("close"))
        prev_close = _f(row.get("prevclose"))
        if last is None and regular_close is None:
            return None

        extended = None
        if session == market_hours.REGULAR:
            regular = last if last is not None else regular_close
        elif session == market_hours.PRE:
            # Before the open the regular reference is yesterday's close; any
            # trade today is a pre-market print.
            regular = prev_close if prev_close is not None else last
            if last is not None and prev_close is not None and abs(last - prev_close) > 1e-9:
                extended = last
        else:
            # After hours, or fully closed: `close` is today's official close
            # once it exists, and anything since is an extended-hours trade.
            regular = regular_close if regular_close is not None else last
            if (last is not None and regular is not None
                    and abs(last - regular) > 1e-9):
                extended = last

        if regular is None:
            return None

        ts = row.get("trade_date")
        timestamp = (datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()
                     if isinstance(ts, (int, float)) and ts else None)
        return Quote(
            symbol=symbol.upper(), last=regular,
            bid=_f(row.get("bid")), ask=_f(row.get("ask")),
            previous_close=prev_close, open=_f(row.get("open")),
            high=_f(row.get("high")), low=_f(row.get("low")),
            volume=_f(row.get("volume")), timestamp=timestamp,
            name=row.get("description"), delayed=not self.realtime,
            extended_last=extended,
            extended_timestamp=timestamp if extended is not None else None,
            market_session=session,
        )

    async def history(self, symbol: str, days: int = 180, interval: str = "1d") -> Bars:
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=int(days * 1.5) + 10)
        data = await self._get("/markets/history", {
            "symbol": symbol, "interval": "daily",
            "start": start.isoformat(), "end": end.isoformat(),
        })
        rows = ((data or {}).get("history") or {}).get("day")
        if rows is None:
            return Bars(symbol.upper(), [])
        if isinstance(rows, dict):
            rows = [rows]
        bars = [
            Bar(r["date"], _f(r.get("open")) or 0.0, _f(r.get("high")) or 0.0,
                _f(r.get("low")) or 0.0, _f(r.get("close")) or 0.0, _f(r.get("volume")) or 0.0)
            for r in rows if _f(r.get("close"))
        ]
        return Bars(symbol.upper(), bars[-days:] if days else bars)

    async def expirations(self, symbol: str) -> list[str]:
        """Cached briefly in-process: chain() validates against this list, so an
        uncached lookup would double every option request."""
        import time
        hit = self._exp_cache.get(symbol.upper())
        if hit and time.monotonic() - hit[0] < 600:
            return hit[1]
        result = await self._expirations_uncached(symbol)
        if result:
            self._exp_cache[symbol.upper()] = (time.monotonic(), result)
        return result

    async def _expirations_uncached(self, symbol: str) -> list[str]:
        data = await self._get("/markets/options/expirations",
                               {"symbol": symbol, "includeAllRoots": "true"})
        dates = ((data or {}).get("expirations") or {}).get("date")
        if not dates:
            return []
        return [dates] if isinstance(dates, str) else list(dates)

    async def chain(self, symbol: str, expiration: str) -> OptionChain | None:
        # Only ever quote an expiration the vendor lists, so a bad date returns
        # nothing rather than a chain labelled with a date that does not exist.
        listed = await self.expirations(symbol)
        if listed and expiration not in listed:
            log.debug("%s is not a listed expiration for %s", expiration, symbol)
            return None

        chain_task = self._get("/markets/options/chains",
                               {"symbol": symbol, "expiration": expiration, "greeks": "true"})
        data, quotes = await asyncio.gather(chain_task, self.quotes([symbol]))
        rows = ((data or {}).get("options") or {}).get("option")
        if not rows:
            return None
        if isinstance(rows, dict):
            rows = [rows]

        q = quotes.get(symbol.upper())
        spot = q.last if q else 0.0
        if not spot:
            return None

        dte = max((date.fromisoformat(expiration) - datetime.now(timezone.utc).date()).days, 0)
        t = max(dte, 0.35) * bs.DAY

        calls, puts = [], []
        for row in rows:
            kind = "call" if str(row.get("option_type", "")).startswith("c") else "put"
            strike = _f(row.get("strike"))
            if strike is None:
                continue
            greeks = row.get("greeks") or {}
            c = OptionContract(
                symbol=row.get("symbol", ""), underlying=symbol.upper(), expiration=expiration,
                strike=strike, kind=kind, bid=_f(row.get("bid")), ask=_f(row.get("ask")),
                last=_f(row.get("last")), volume=_f(row.get("volume")) or 0,
                open_interest=_f(row.get("open_interest")) or 0,
                implied_volatility=_f(greeks.get("mid_iv")) or _f(greeks.get("smv_vol")),
                delta=_f(greeks.get("delta")), gamma=_f(greeks.get("gamma")),
                # Tradier publishes annual theta; the rest of this app works in
                # per-calendar-day terms.
                theta=(_f(greeks.get("theta")) * bs.DAY if _f(greeks.get("theta")) is not None else None),
                vega=(_f(greeks.get("vega")) * 0.01 if _f(greeks.get("vega")) is not None else None),
            )
            # Backfill anything the vendor left empty so downstream scoring
            # never has to special-case a missing greek.
            if c.implied_volatility is None and c.mid:
                c.implied_volatility = bs.implied_vol(c.mid, spot, strike, t, self.risk_free, kind)
            if c.implied_volatility and c.delta is None:
                g = bs.greeks(spot, strike, t, self.risk_free, c.implied_volatility, kind)
                c.delta, c.gamma, c.theta, c.vega = g.delta, g.gamma, g.theta, g.vega
            (calls if kind == "call" else puts).append(c)

        calls.sort(key=lambda c: c.strike)
        puts.sort(key=lambda c: c.strike)
        return OptionChain(symbol.upper(), expiration, spot, calls, puts)

    async def news(self, symbols: list[str], limit: int = 30) -> list[NewsItem]:
        # Tradier's market-data plans do not include a news feed.
        return []

    async def close(self) -> None:
        await self._client.aclose()
