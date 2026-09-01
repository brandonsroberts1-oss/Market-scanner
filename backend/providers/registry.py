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
from .cboe import CboeProvider
from .stooq import StooqProvider
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


SIMULATED_NAMES = {"demo", "simulated", "fake", "offline"}


def _make(name: str):
    if name == "tradier":
        if not settings.tradier_token:
            raise ProviderUnavailable(
                "MARKET_DATA_PROVIDER=tradier but TRADIER_TOKEN is not set. Add your "
                "token to .env, or leave MARKET_DATA_PROVIDER=auto for the free feeds."
            )
        return TradierProvider(settings.tradier_token, settings.tradier_base_url,
                               risk_free=settings.risk_free_rate)
    if name == "yahoo":
        return YahooProvider(risk_free=settings.risk_free_rate)
    if name == "cboe":
        return CboeProvider()
    if name == "stooq":
        return StooqProvider()
    raise ProviderUnavailable(
        f"Unknown MARKET_DATA_PROVIDER {name!r}. Valid values are 'auto', 'tradier', "
        f"'yahoo', 'cboe' and 'stooq', or a comma-separated list to try in order."
    )


def build_providers(name: str | None = None) -> list:
    """Build the ordered list of live sources to try.

    Relying on one vendor is what made a Yahoo block look like "no data". The
    default now chains several independent sources so a single one refusing
    requests degrades coverage instead of emptying the screen:

      * Tradier  - real-time, authenticated, used first when a token is set
      * Yahoo    - quotes with extended hours, option chains, price history
      * CBOE     - option chains and underlying prices, no key or crumb needed
      * Stooq    - daily bars as plain CSV, no authentication at all

    Only real vendors can be selected; there is no simulated option.
    """
    raw = (name or settings.provider_name or "auto").lower().strip()

    if raw in SIMULATED_NAMES:
        raise ProviderUnavailable(
            "Simulated market data is not available. Leave MARKET_DATA_PROVIDER "
            "as 'auto', or set it to 'tradier' (with TRADIER_TOKEN), 'yahoo', "
            "'cboe' or 'stooq'."
        )

    if raw == "auto":
        names = (["tradier"] if settings.tradier_token else []) + ["yahoo", "cboe", "stooq"]
    else:
        names = [n.strip() for n in raw.split(",") if n.strip()]
        for n in names:
            if n in SIMULATED_NAMES:
                raise ProviderUnavailable(
                    "Simulated market data is not available. Remove "
                    f"{n!r} from MARKET_DATA_PROVIDER."
                )

    providers = []
    errors = []
    for n in names:
        try:
            providers.append(_make(n))
        except ProviderUnavailable as exc:
            errors.append(str(exc))
    if not providers:
        raise ProviderUnavailable(" ".join(errors) or "No usable data provider configured.")
    return providers


def build_provider(name: str | None = None):
    """The primary provider. Kept for callers that want a single source."""
    return build_providers(name)[0]


class MarketData:
    """Caching facade over an ordered chain of live sources.

    Each request tries the sources in turn and takes the first usable answer,
    recording which one supplied it. If every source fails, the persistent
    last-known-good store serves the most recent REAL data, marked stale. If
    even that is empty the symbol is reported as having no data - never filled
    in with a generated number.

    A per-source circuit breaker stops a dead vendor from being retried on
    every symbol, which otherwise turns one outage into dozens of timeouts.
    """

    FAILURE_THRESHOLD = 3
    COOLDOWN_SECONDS = 60.0

    def __init__(self, providers=None, use_store: bool = True):
        if providers is None:
            providers = build_providers()
        elif not isinstance(providers, (list, tuple)):
            providers = [providers]
        self.providers = list(providers)
        if not self.providers:
            raise ProviderUnavailable("No data providers configured.")

        self.use_store = use_store
        self.cache = TTLCache()

        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._order_cache: dict[str, list] = {}

        self.stale_symbols: dict[str, str] = {}
        self.missing_symbols: set[str] = set()
        self.source_errors: dict[str, str] = {}
        self.sources_used: set[str] = set()

    # -- identity -----------------------------------------------------------
    @property
    def provider(self):
        return self.providers[0]

    @property
    def name(self) -> str:
        return "+".join(p.name for p in self.providers)

    @property
    def realtime(self) -> bool:
        return any(getattr(p, "realtime", False) for p in self.providers)

    @property
    def serving_stale(self) -> bool:
        return bool(self.stale_symbols)

    # -- circuit breaker, per source ---------------------------------------
    def _is_open(self, provider) -> bool:
        return time.monotonic() < self._open_until.get(provider.name, 0.0)

    @property
    def circuit_open(self) -> bool:
        """True when every source is in cooldown."""
        return all(self._is_open(p) for p in self.providers)

    def _record_success(self, provider) -> None:
        self._failures[provider.name] = 0
        self._open_until[provider.name] = 0.0
        self.source_errors.pop(provider.name, None)
        self.sources_used.add(provider.name)

    def _record_failure(self, provider, error: str | None = None) -> None:
        count = self._failures.get(provider.name, 0) + 1
        self._failures[provider.name] = count
        if error:
            self.source_errors[provider.name] = error
        elif getattr(provider, "last_error", None):
            self.source_errors[provider.name] = provider.last_error
        if count >= self.FAILURE_THRESHOLD and not self._is_open(provider):
            self._open_until[provider.name] = time.monotonic() + self.COOLDOWN_SECONDS
            log.warning(
                "Data source '%s' is not responding (%d failures in a row%s). "
                "Skipping it for %.0fs and trying the other sources.",
                provider.name, count,
                f": {self.source_errors[provider.name]}"
                if provider.name in self.source_errors else "",
                self.COOLDOWN_SECONDS,
            )

    # Each source is good at different things, and asking the wrong one first
    # costs requests. CBOE returns an entire option chain - every expiration
    # and contract - in a single response, where Yahoo needs one request per
    # expiration, so it leads for options. Yahoo leads for quotes because it is
    # the only free source with pre- and post-market prices.
    CAPABILITY_PREFERENCE = {
        "quotes": ["tradier", "yahoo", "cboe", "stooq"],
        "history": ["tradier", "yahoo", "stooq"],
        "options": ["tradier", "cboe", "yahoo"],
        "news": ["yahoo"],
    }

    def _ordered_for(self, capability: str) -> list:
        """Sources for one capability, best-suited first, configured order kept
        as the tie-break so an explicit MARKET_DATA_PROVIDER list still wins."""
        cached = self._order_cache.get(capability)
        if cached is not None:
            return cached
        preference = self.CAPABILITY_PREFERENCE.get(capability, [])

        def rank(provider):
            try:
                return (preference.index(provider.name), 0)
            except ValueError:
                # Not ranked for this capability: keep it, but after the ones
                # that are, in the order the user configured.
                return (len(preference), self.providers.index(provider))

        ordered = sorted(self.providers, key=rank)
        self._order_cache[capability] = ordered
        return ordered

    async def _try_each(self, call, accept, label: str, capability: str = ""):
        """Run `call(provider)` across the chain, returning the first accepted result."""
        for provider in (self._ordered_for(capability) if capability else self.providers):
            if self._is_open(provider):
                continue
            try:
                result = await call(provider)
            except Exception as exc:                           # noqa: BLE001
                log.debug("%s failed for %s: %s", provider.name, label, exc)
                self._record_failure(provider, f"{type(exc).__name__}: {exc}")
                continue
            if accept(result):
                self._record_success(provider)
                return result, provider
            self._record_failure(provider)
        return None, None

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
        self.sources_used.clear()

    # -- quotes -------------------------------------------------------------
    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        symbols = [s.upper() for s in dict.fromkeys(symbols) if s]
        if not symbols:
            return {}
        key = "q:" + ",".join(sorted(symbols))

        async def load():
            data: dict[str, Quote] = {}
            # Take what each source can supply, then ask the next one only for
            # what is still missing.
            for provider in self._ordered_for("quotes"):
                outstanding = [s for s in symbols if s not in data]
                if not outstanding or self._is_open(provider):
                    continue
                try:
                    fetched = await provider.quotes(outstanding)
                except Exception as exc:                       # noqa: BLE001
                    log.debug("%s quotes failed: %s", provider.name, exc)
                    self._record_failure(provider, f"{type(exc).__name__}: {exc}")
                    continue
                if fetched:
                    self._record_success(provider)
                    for symbol, quote in fetched.items():
                        quote.source = provider.name
                        data[symbol] = quote
                else:
                    self._record_failure(provider)

            for symbol, quote in data.items():
                self._mark_fresh(symbol)
                if self.use_store:
                    store.put_quote(quote, quote.source)

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
            # Daily bars only change once a day. If the store already holds
            # bars through the latest completed session, use them and make no
            # request at all - this is what keeps a 90-symbol rescan from
            # issuing 90 requests and being throttled.
            if self.use_store and interval == "1d":
                cached = store.get(store_key)
                if cached and isinstance(cached[0], Bars) and len(cached[0]) >= 30:
                    from .. import market_hours
                    newest = cached[0].bars[-1].date[:10]
                    if newest >= market_hours.latest_completed_session().isoformat():
                        return cached[0]

            bars, provider = await self._try_each(
                lambda p: p.history(symbol, days, interval),
                lambda b: b is not None and len(b) >= 30,
                f"history {symbol}", capability="history",
            )
            if bars is not None and provider is not None:
                self._mark_fresh(symbol)
                if self.use_store:
                    store.put(store_key, "bars", bars, symbol.upper(), provider.name)
                return bars

            if self.use_store:
                hit = store.get(store_key)
                if hit and isinstance(hit[0], Bars) and len(hit[0]) >= 30:
                    self._mark_stale(symbol, hit[1])
                    return hit[0]
            self._mark_missing(symbol)
            return Bars(symbol.upper(), [])

        return await self.cache.get_or_set(key, settings.history_ttl, load)

    # -- options ------------------------------------------------------------
    async def expirations(self, symbol: str) -> list[str]:
        key = f"e:{symbol.upper()}"
        store_key = f"exp:{symbol.upper()}"

        async def load():
            exps, provider = await self._try_each(
                lambda p: p.expirations(symbol),
                lambda e: bool(e),
                f"expirations {symbol}", capability="options",
            )
            if exps and provider is not None:
                if self.use_store:
                    store.put(store_key, "expirations", exps, symbol.upper(), provider.name)
                return exps

            if self.use_store:
                hit = store.get(store_key)
                if hit and isinstance(hit[0], list):
                    self._mark_stale(symbol, hit[1])
                    # A remembered list must never offer a date already past.
                    from datetime import datetime, timezone
                    today = datetime.now(timezone.utc).date()
                    return [e for e in hit[0]
                            if _safe_date(e) is not None and _safe_date(e) >= today]
            return []

        return await self.cache.get_or_set(key, settings.history_ttl, load)

    async def chain(self, symbol: str, expiration: str) -> OptionChain | None:
        key = f"c:{symbol.upper()}:{expiration}"
        store_key = f"chain:{symbol.upper()}:{expiration}"

        async def load():
            chain, provider = await self._try_each(
                lambda p: p.chain(symbol, expiration),
                lambda c: c is not None and bool(c.calls),
                f"chain {symbol} {expiration}", capability="options",
            )
            if chain is not None and provider is not None:
                if self.use_store:
                    store.put(store_key, "chain", chain, symbol.upper(), provider.name)
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
            items, _ = await self._try_each(
                lambda p: p.news(symbols, limit), lambda n: bool(n), "news",
                capability="news")
            return items or []

        return await self.cache.get_or_set(key, settings.news_ttl, load)

    # -- reporting ----------------------------------------------------------
    def data_status(self) -> dict:
        """What the UI needs to explain how trustworthy this data is."""
        oldest = min(self.stale_symbols.values(), default=None)
        return {
            "provider": self.name,
            "sources": [p.name for p in self.providers],
            "sources_used": sorted(self.sources_used),
            "sources_down": sorted(p.name for p in self.providers if self._is_open(p)),
            "source_errors": dict(self.source_errors),
            "realtime": self.realtime,
            "provider_reachable": not self.circuit_open,
            "stale_symbols": sorted(self.stale_symbols),
            "stale_count": len(self.stale_symbols),
            "stale_since": oldest,
            "stale_age": store.describe_age(oldest) if oldest else None,
            "missing_symbols": sorted(self.missing_symbols),
        }

    async def close(self) -> None:
        for provider in self.providers:
            try:
                await provider.close()
            except Exception:                                   # noqa: BLE001
                pass


def _safe_date(value: str):
    from datetime import date
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
