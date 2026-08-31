"""Provider selection plus a small async TTL cache.

The scanner asks for the same quotes and chains repeatedly within one pass, so
an in-process cache turns an O(symbols x strategies) call pattern into one
request per symbol per TTL window.  Single-flight de-duplication means twenty
concurrent callers asking for the same chain produce one upstream request.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from ..config import settings
from .base import Bars, NewsItem, OptionChain, Quote
from .demo import DemoProvider
from .tradier import TradierProvider
from .yahoo import YahooProvider

log = logging.getLogger(__name__)


class TTLCache:
    def __init__(self):
        self._data: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get_or_set(self, key: str, ttl: float, factory: Callable[[], Awaitable[Any]]) -> Any:
        now = time.monotonic()
        hit = self._data.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]

        async with self._guard:
            lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Re-check: another coroutine may have filled it while we waited.
            hit = self._data.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            value = await factory()
            self._data[key] = (time.monotonic(), value)
            return value

    def invalidate(self, prefix: str = "") -> None:
        for key in [k for k in self._data if k.startswith(prefix)]:
            self._data.pop(key, None)

    def stats(self) -> dict:
        return {"entries": len(self._data)}


def build_provider(name: str | None = None):
    """Instantiate the configured provider, resolving 'auto'."""
    name = (name or settings.provider_name or "auto").lower()
    if name == "auto":
        name = "tradier" if settings.tradier_token else "yahoo"

    if name == "tradier":
        if not settings.tradier_token:
            log.warning("tradier selected but TRADIER_TOKEN is unset; using yahoo")
            return YahooProvider(risk_free=settings.risk_free_rate)
        return TradierProvider(settings.tradier_token, settings.tradier_base_url,
                               risk_free=settings.risk_free_rate)
    if name == "demo":
        return DemoProvider()
    return YahooProvider(risk_free=settings.risk_free_rate)


class MarketData:
    """Caching facade over a provider, with automatic fallback to demo data.

    If the live vendor returns nothing for a symbol (rate limit, outage, a
    ticker it does not carry), the facade falls back to the offline simulator
    and marks the response so the UI can tell the user the data is simulated
    rather than silently showing invented prices as real.
    """

    def __init__(self, provider=None, fallback: bool = True):
        self.provider = provider or build_provider()
        self.fallback = DemoProvider() if fallback else None
        self.cache = TTLCache()
        self.degraded = False           # True once we have served fallback data

    @property
    def name(self) -> str:
        return self.provider.name

    @property
    def realtime(self) -> bool:
        return bool(getattr(self.provider, "realtime", False))

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        symbols = [s.upper() for s in dict.fromkeys(symbols) if s]
        if not symbols:
            return {}
        key = "q:" + ",".join(sorted(symbols))

        async def load():
            try:
                data = await self.provider.quotes(symbols)
            except Exception as exc:                       # noqa: BLE001
                log.warning("provider quotes failed: %s", exc)
                data = {}
            missing = [s for s in symbols if s not in data]
            if missing and self.fallback:
                self.degraded = True
                log.info("falling back to simulated quotes for %s", ",".join(missing))
                try:
                    for sym, quote in (await self.fallback.quotes(missing)).items():
                        quote.name = (quote.name or sym)
                        data[sym] = quote
                except Exception as exc:                   # noqa: BLE001
                    log.warning("fallback quotes failed: %s", exc)
            return data

        return await self.cache.get_or_set(key, settings.quote_ttl, load)

    async def history(self, symbol: str, days: int = 180, interval: str = "1d") -> Bars:
        key = f"h:{symbol.upper()}:{days}:{interval}"

        async def load():
            try:
                bars = await self.provider.history(symbol, days, interval)
            except Exception as exc:                       # noqa: BLE001
                log.warning("provider history %s failed: %s", symbol, exc)
                bars = Bars(symbol.upper(), [])
            if len(bars) < 30 and self.fallback:
                self.degraded = True
                bars = await self.fallback.history(symbol, days, interval)
            return bars

        return await self.cache.get_or_set(key, settings.history_ttl, load)

    async def expirations(self, symbol: str) -> list[str]:
        key = f"e:{symbol.upper()}"

        async def load():
            try:
                exps = await self.provider.expirations(symbol)
            except Exception as exc:                       # noqa: BLE001
                log.warning("provider expirations %s failed: %s", symbol, exc)
                exps = []
            if not exps and self.fallback:
                self.degraded = True
                exps = await self.fallback.expirations(symbol)
            return exps

        return await self.cache.get_or_set(key, settings.history_ttl, load)

    async def chain(self, symbol: str, expiration: str) -> OptionChain | None:
        key = f"c:{symbol.upper()}:{expiration}"

        async def load():
            try:
                chain = await self.provider.chain(symbol, expiration)
            except Exception as exc:                       # noqa: BLE001
                log.warning("provider chain %s %s failed: %s", symbol, expiration, exc)
                chain = None
            if (chain is None or not chain.calls) and self.fallback:
                self.degraded = True
                chain = await self.fallback.chain(symbol, expiration)
            return chain

        return await self.cache.get_or_set(key, settings.chain_ttl, load)

    async def news(self, symbols: list[str], limit: int = 30) -> list[NewsItem]:
        key = "n:" + ",".join(sorted(s.upper() for s in symbols)) + f":{limit}"

        async def load():
            try:
                items = await self.provider.news(symbols, limit)
            except Exception as exc:                       # noqa: BLE001
                log.warning("provider news failed: %s", exc)
                items = []
            return items

        return await self.cache.get_or_set(key, settings.news_ttl, load)

    async def close(self) -> None:
        await self.provider.close()
        if self.fallback:
            await self.fallback.close()
