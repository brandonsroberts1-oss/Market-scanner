"""CBOE delayed quotes - option chains and underlying prices, no key required.

CBOE publishes its own delayed data as plain JSON with no authentication, no
cookie handshake and no crumb. For option chains that makes it far more
dependable than scraping Yahoo, and it comes from the exchange rather than a
portal: one request returns the underlying quote plus every listed contract
with bid/ask, volume, open interest, implied volatility and greeks.

Delayed roughly 15 minutes, which the app reports. Options only - it carries no
historical bars, so it is paired with another source for price history.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from .. import market_hours
from .base import NewsItem, OptionChain, OptionContract, Quote, parse_occ
from .ratelimit import RateLimiter, SourcePaused

log = logging.getLogger(__name__)

BASE = "https://cdn.cboe.com/api/global/delayed_quotes/options"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Broad-based index options are published under an underscore prefix.
INDEX_SYMBOLS = {"SPX", "NDX", "RUT", "VIX", "DJX", "XSP", "OEX"}


def _f(value) -> float | None:
    if value in (None, "", "NaN"):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


class CboeProvider:
    name = "cboe"
    realtime = False          # delayed ~15 minutes

    def __init__(self, timeout: float = 20.0):
        self.limiter = RateLimiter("CBOE", 4, 0.12)
        self._client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=True,
            headers={"User-Agent": UA, "Accept": "application/json"},
        )
        # One payload carries the quote and the whole chain, so it is fetched
        # once per symbol and shared between both calls.
        self._cache: dict[str, tuple[float, dict]] = {}
        self.last_error: str | None = None

    def _url(self, symbol: str) -> str:
        sym = symbol.upper().replace(".", "")
        prefix = "_" if sym in INDEX_SYMBOLS else ""
        return f"{BASE}/{prefix}{sym}.json"

    async def _payload(self, symbol: str) -> dict | None:
        import time
        key = symbol.upper()
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < 45:
            return hit[1]
        try:
            async with self.limiter:
                response = await self._client.get(self._url(symbol))
        except SourcePaused as exc:
            self.last_error = str(exc)
            return None
        except httpx.HTTPError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.debug("cboe request failed %s: %s", symbol, exc)
            return None
        if response.status_code != 200:
            self.last_error = f"HTTP {response.status_code}"
            log.debug("cboe %s -> HTTP %s", symbol, response.status_code)
            return None
        try:
            data = response.json()
        except ValueError:
            self.last_error = "response was not JSON"
            return None
        payload = data.get("data")
        if not isinstance(payload, dict):
            self.last_error = "unexpected payload shape"
            return None
        self.last_error = None
        self._cache[key] = (time.monotonic(), payload)
        return payload

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        # Concurrent, bounded by the shared limiter. CBOE has no batch endpoint,
        # so a sequential loop would make it too slow to carry a full scan.
        import asyncio
        payloads = await asyncio.gather(
            *(self._payload(s) for s in symbols), return_exceptions=True)

        out: dict[str, Quote] = {}
        for symbol, payload in zip(symbols, payloads):
            if isinstance(payload, Exception) or not payload:
                continue
            last = _f(payload.get("current_price")) or _f(payload.get("close"))
            if last is None:
                continue
            prev = _f(payload.get("prev_day_close"))
            out[symbol.upper()] = Quote(
                symbol=symbol.upper(), last=last,
                bid=_f(payload.get("bid")), ask=_f(payload.get("ask")),
                previous_close=prev, open=_f(payload.get("open")),
                high=_f(payload.get("high")), low=_f(payload.get("low")),
                volume=_f(payload.get("volume")),
                timestamp=datetime.now(timezone.utc).isoformat(),
                name=payload.get("symbol"), delayed=True,
                market_session=market_hours.session(),
            )
        return out

    async def history(self, symbol: str, days: int = 180, interval: str = "1d"):
        from .base import Bars
        return Bars(symbol.upper(), [])          # CBOE carries no bar history

    async def expirations(self, symbol: str) -> list[str]:
        payload = await self._payload(symbol)
        if not payload:
            return []
        dates: set[str] = set()
        for row in payload.get("options") or []:
            info = parse_occ(str(row.get("option", "")))
            if info:
                dates.add(info["expiration"])
        today = datetime.now(timezone.utc).date().isoformat()
        return sorted(d for d in dates if d >= today)

    async def chain(self, symbol: str, expiration: str) -> OptionChain | None:
        payload = await self._payload(symbol)
        if not payload:
            return None

        spot = _f(payload.get("current_price")) or _f(payload.get("close"))
        if not spot:
            return None

        calls: list[OptionContract] = []
        puts: list[OptionContract] = []
        for row in payload.get("options") or []:
            occ = str(row.get("option", ""))
            info = parse_occ(occ)
            if not info or info["expiration"] != expiration:
                continue
            if info["underlying"] != symbol.upper().replace(".", ""):
                continue

            contract = OptionContract(
                symbol=occ, underlying=symbol.upper(), expiration=expiration,
                strike=info["strike"], kind=info["kind"],
                bid=_f(row.get("bid")), ask=_f(row.get("ask")),
                last=_f(row.get("last_trade_price")),
                volume=_f(row.get("volume")) or 0,
                open_interest=_f(row.get("open_interest")) or 0,
                implied_volatility=_f(row.get("iv")),
                delta=_f(row.get("delta")), gamma=_f(row.get("gamma")),
                # CBOE publishes theta per day and vega per volatility point,
                # which is already the convention this app uses.
                theta=_f(row.get("theta")), vega=_f(row.get("vega")),
            )
            (calls if info["kind"] == "call" else puts).append(contract)

        if not calls and not puts:
            return None
        calls.sort(key=lambda c: c.strike)
        puts.sort(key=lambda c: c.strike)
        return OptionChain(symbol.upper(), expiration, spot, calls, puts)

    async def news(self, symbols: list[str], limit: int = 30) -> list[NewsItem]:
        return []

    async def close(self) -> None:
        await self._client.aclose()
