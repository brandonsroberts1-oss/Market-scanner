"""Provider selection, caching, and last-known-good fallback.

Two layers sit in front of the vendor:

  * A short-lived in-process TTL cache with single-flight de-duplication, so
    one scan pass makes one request per symbol rather than one per strategy.

  * A persistent last-known-good store. When the vendor cannot be reached, the
    app serves the most recent REAL data it has, labelled with its age. It
    never substitutes a generated price. If nothing has ever been fetched for
    a symbol, the app says so instead of showing a number.

There is deliberately no simulator here. Fabricated prices produce fabricated
expirations and fabricated signals, and a scanner that quietly does that is
worse than one that says "no data".
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from ..config import settings
from . import store
from .base import Bars, NewsItem, OptionChain, Quote
from .tradier import TradierProvider
from .yahoo import YahooProvider

log = logging.getLogger(__name__)


class ProviderUnavailable(RuntimeError):
    """No configured provider could be built."""


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
    """Instantiate the configured live provider.

    Only real market-data vendors can be selected. There is no offline or
    demo option: see the module docstring.
    """
    name = (name or settings.provider_name or "auto").lower()
    if name == "auto":
        name = "tradier" if settings.tradier_token else "yahoo"

    if name in ("demo", "simulated", "fake", "offline"):
        raise ProviderUnavailable(
            "Simulated market data is not available. Set MARKET_DATA_PROVIDER to "
            "'tradier' (with TRADIER_TOKEN) or 'yahoo'."
        )
    if name == "tradier":
        if not settings.tradier_token:
            raise ProviderUnavailable(
                "MARKET_DATA_PROVIDER=tradier but TRADIER_TOKEN is not set. Add your "
                "token to .env, or set MARKET_DATA_PROVIDER=yahoo to use the free feed."
            )
        return TradierProvider(settings.tradier_token, settings.tradier_base_url,
                               risk_free=settings.risk_free_rate)
    if name == "yahoo":
        return YahooProvider(risk_free=settings.risk_free_rate)

    raise ProviderUnavailable(
        f"Unknown MARKET_DATA_PROVIDER {name!r}. Valid values are 'auto', 'tradier' "
        f"and 'yahoo'."
    )


class MarketData:
    """Caching facade over a live provider, backed by last-known-good data.

    A circuit breaker sits in front of the vendor: when it is fully
    unreachable, every call otherwise waits out its own timeout and a scan
    rediscovers the same outage once per symbol. After a few consecutive
    failures the vendor is skipped for a cooldown and requests are served from
    the store instead.
    """

    FAILURE_THRESHOLD = 3
    COOLDOWN_SECONDS = 60.0

    def __init__(self, provider=None, use_store: bool = True):
        self.provider = provider or build_provider()
        self.use_store = use_store
        self.cache = TTLCache()
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

        # Symbols currently being served from the store, mapped to the age of
        # the data. Reported to the UI so staleness is always visible.
        self.stale_symbols: dict[str, str] = {}
        self.missing_symbols: set[str] = set()

    @property
    def name(self) -> str:
        return self.provider.name

    @property
    def realtime(self) -> bool:
        return bool(getattr(self.provider, "realtime", False))

    @property
    def serving_stale(self) -> bool:
        return bool(self.stale_symbols)

    # -- circuit breaker ----------------------------------------------------
    @property
    def circuit_open(self) -> bool:
        return time.monotonic() < self._circuit_open_until

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.FAILURE_THRESHOLD and not self.circuit_open:
            self._circuit_open_until = time.monotonic() + self.COOLDOWN_SECONDS
            log.warning(
                "The %s data provider is not responding (%d failures in a row). "
                "Falling back to the most recent data already fetched, and marking "
                "it stale. Check your internet connection or API token.",
                self.provider.name, self._consecutive_failures,
            )

    def _skip_provider(self) -> bool:
        return self.circuit_open

    def _mark_stale(self, symbol: str, fetched_at: str) -> None:
        self.stale_symbols[symbol.upper()] = fetched_at
        self.missing_symbols.discard(symbol.upper())

    def _mark_fresh(self, symbol: str) -> None:
        self.stale_symbols.pop(symbol.upper(), None)
        self.missing_symbols.discard(symbol.upper())

    def _mark_missing(self, symbol: str) -> None:
        self.missing_symbols.add(symbol.upper())

    def reset_status(self) -> None:
        self.stale_symbols.clear()
        self.missing_symbols.clear()

    # -- quotes -------------------------------------------------------------
    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        symbols = [s.upper() for s in dict.fromkeys(symbols) if s]
        if not symbols:
            return {}
        key = "q:" + ",".join(sorted(symbols))

        async def load():
            data: dict[str, Quote] = {}
            if not self._skip_provider():
                try:
                    data = await self.provider.quotes(symbols)
                    self._record_success() if data else self._record_failure()
                except Exception as exc:                       # noqa: BLE001
                    log.debug("provider quotes failed: %s", exc)
                    self._record_failure()

            for symbol, quote in data.items():
                quote.source = self.provider.name
                self._mark_fresh(symbol)
                if self.use_store:
                    store.put_quote(quote, self.provider.name)

            for symbol in symbols:
                if symbol in data:
                    continue
                recovered = self._last_known_quote(symbol)
                if recovered is not None:
                    data[symbol] = recovered
                else:
                    self._mark_missing(symbol)
            return data

        return await self.cache.get_or_set(key, settings.quote_ttl, load)

    def _last_known_quote(self, symbol: str) -> Quote | None:
        """The most recent real quote for this symbol, marked stale."""
        if not self.use_store:
            return None
        hit = store.get_quote(symbol)
        if hit is None:
            return None
        quote, fetched_at = hit
        quote.stale = True
        quote.as_of = fetched_at
        self._mark_stale(symbol, fetched_at)
        return quote

    # -- history ------------------------------------------------------------
    async def history(self, symbol: str, days: int = 180, interval: str = "1d") -> Bars:
        key = f"h:{symbol.upper()}:{days}:{interval}"
        store_key = f"bars:{symbol.upper()}:{interval}"

        async def load():
            bars = Bars(symbol.upper(), [])
            if not self._skip_provider():
                try:
                    bars = await self.provider.history(symbol, days, interval)
                    self._record_success() if len(bars) else self._record_failure()
                except Exception as exc:                       # noqa: BLE001
                    log.debug("provider history %s failed: %s", symbol, exc)
                    self._record_failure()

            if len(bars) >= 30:
                if self.use_store:
                    store.put(store_key, "bars", bars, symbol.upper(), self.provider.name)
                return bars

            if self.use_store:
                hit = store.get(store_key)
                if hit and isinstance(hit[0], Bars) and len(hit[0]) >= 30:
                    self._mark_stale(symbol, hit[1])
                    return hit[0]
            self._mark_missing(symbol)
            return bars

        return await self.cache.get_or_set(key, settings.history_ttl, load)

    # -- options ------------------------------------------------------------
    async def expirations(self, symbol: str) -> list[str]:
        key = f"e:{symbol.upper()}"
        store_key = f"exp:{symbol.upper()}"

        async def load():
            exps: list[str] = []
            if not self._skip_provider():
                try:
                    exps = await self.provider.expirations(symbol)
                    self._record_success() if exps else self._record_failure()
                except Exception as exc:                       # noqa: BLE001
                    log.debug("provider expirations %s failed: %s", symbol, exc)
                    self._record_failure()

            if exps:
                if self.use_store:
                    store.put(store_key, "expirations", exps, symbol.upper(),
                              self.provider.name)
                return exps

            if self.use_store:
                hit = store.get(store_key)
                if hit and isinstance(hit[0], list):
                    self._mark_stale(symbol, hit[1])
                    # Drop anything that has since expired: a cached list must
                    # never offer a date that has already passed.
                    from datetime import date, datetime, timezone
                    today = datetime.now(timezone.utc).date()
                    return [e for e in hit[0]
                            if _safe_date(e) is not None and _safe_date(e) >= today]
            return []

        return await self.cache.get_or_set(key, settings.history_ttl, load)

    async def chain(self, symbol: str, expiration: str) -> OptionChain | None:
        key = f"c:{symbol.upper()}:{expiration}"
        store_key = f"chain:{symbol.upper()}:{expiration}"

        async def load():
            chain = None
            if not self._skip_provider():
                try:
                    chain = await self.provider.chain(symbol, expiration)
                    self._record_success() if chain else self._record_failure()
                except Exception as exc:                       # noqa: BLE001
                    log.debug("provider chain %s %s failed: %s", symbol, expiration, exc)
                    self._record_failure()

            if chain and chain.calls:
                if self.use_store:
                    store.put(store_key, "chain", chain, symbol.upper(), self.provider.name)
                return chain

            if self.use_store:
                hit = store.get(store_key)
                if hit and isinstance(hit[0], OptionChain) and hit[0].calls:
                    self._mark_stale(symbol, hit[1])
                    return hit[0]
            return None

        return await self.cache.get_or_set(key, settings.chain_ttl, load)

    # -- news ---------------------------------------------------------------
    async def news(self, symbols: list[str], limit: int = 30) -> list[NewsItem]:
        key = "n:" + ",".join(sorted(s.upper() for s in symbols)) + f":{limit}"

        async def load():
            if self._skip_provider():
                return []
            try:
                return await self.provider.news(symbols, limit)
            except Exception as exc:                           # noqa: BLE001
                log.debug("provider news failed: %s", exc)
                return []

        return await self.cache.get_or_set(key, settings.news_ttl, load)

    # -- reporting ----------------------------------------------------------
    def data_status(self) -> dict:
        """What the UI needs to tell the user how trustworthy this data is."""
        oldest = min(self.stale_symbols.values(), default=None)
        return {
            "provider": self.name,
            "realtime": self.realtime,
            "provider_reachable": not self.circuit_open,
            "stale_symbols": sorted(self.stale_symbols),
            "stale_count": len(self.stale_symbols),
            "stale_since": oldest,
            "stale_age": store.describe_age(oldest) if oldest else None,
            "missing_symbols": sorted(self.missing_symbols),
        }

    async def close(self) -> None:
        await self.provider.close()


def _safe_date(value: str):
    from datetime import date
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
