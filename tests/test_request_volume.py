"""Request pacing and volume.

Written after a real failure: the app reported "no data" for every symbol while
a manual check of each endpoint succeeded. One request always worked; the scan
fired roughly 450 in four seconds and was rate-limited from the third onward.
These tests pin the behaviour that fixed it.
"""
import asyncio
import time
from datetime import date

import httpx
import pytest

from backend.engine.scanner import Scanner
from backend.engine.universe import get_universe
from backend.providers.ratelimit import RateLimiter, retry_after_seconds
from backend.providers.registry import MarketData
from tests.simulated_provider import SimulatedProvider


def run(coro):
    return asyncio.run(coro)


BASE = SimulatedProvider(as_of=date(2026, 8, 31))


class CountingProvider:
    """One HTTP request per public call, like the real vendor clients."""
    name = "counting"
    realtime = False

    def __init__(self):
        self.http = 0
        self._exp: dict[str, list[str]] = {}

    async def quotes(self, symbols):
        self.http += 1                        # batched, one request per call
        return await BASE.quotes(symbols)

    async def history(self, symbol, days=180, interval="1d"):
        self.http += 1
        return await BASE.history(symbol, days, interval)

    async def expirations(self, symbol):
        if symbol.upper() in self._exp:
            return self._exp[symbol.upper()]
        self.http += 1
        out = await BASE.expirations(symbol)
        self._exp[symbol.upper()] = out
        return out

    async def chain(self, symbol, expiration):
        await self.expirations(symbol)
        self.http += 1
        return await BASE.chain(symbol, expiration)

    async def news(self, symbols, limit=30):
        return []

    async def close(self):
        return None


# ---------------- volume ----------------
def test_quotes_are_batched_not_one_request_per_symbol():
    """The original bug: quotes(['SYM']) called 90 times instead of once."""
    provider = CountingProvider()
    md = MarketData(provider, use_store=False)
    symbols = get_universe("wide")
    quotes = run(md.quotes(symbols))
    assert len(quotes) >= 80
    assert provider.http == 1, f"{len(symbols)} symbols took {provider.http} requests"


def test_a_full_scan_stays_well_under_the_old_volume(fresh_db):
    provider = CountingProvider()
    md = MarketData(provider, use_store=True)
    run(Scanner(md).scan("wide", 0, 3, min_conviction=25, limit=40, include_news=False))
    symbols = len(get_universe("wide"))
    # The old design issued roughly 5 requests per symbol.
    assert provider.http < symbols * 2, (
        f"{provider.http} requests for {symbols} symbols is enough to get throttled")


def test_a_rescan_reuses_stored_daily_bars(fresh_db):
    """Daily bars change once a day; a rescan should not refetch them."""
    first = CountingProvider()
    run(Scanner(MarketData(first, use_store=True)).scan(
        "core", 0, 3, min_conviction=25, include_news=False))

    second = CountingProvider()
    run(Scanner(MarketData(second, use_store=True)).scan(
        "core", 0, 3, min_conviction=25, include_news=False))
    assert second.http < first.http, "the second scan should reuse stored history"


def test_option_chains_are_only_fetched_for_ranked_candidates(fresh_db):
    """Chains are the expensive call and only useful for names already scoring."""
    provider = CountingProvider()
    md = MarketData(provider, use_store=False)
    result = run(Scanner(md).scan("wide", 0, 3, min_conviction=25, limit=40,
                                  include_news=False, chain_budget=5))
    symbols_with_ideas = {i["symbol"] for i in result.ideas}
    assert len(symbols_with_ideas) <= 5
    assert result.equities, "equity ranking must still cover the whole universe"


def test_scan_still_produces_ideas_after_the_volume_work(fresh_db):
    provider = CountingProvider()
    result = run(Scanner(MarketData(provider, use_store=False)).scan(
        "core", 0, 3, min_conviction=20, include_news=False))
    assert result.ideas, "the request reduction must not cost coverage"
    assert all(i["legs"] for i in result.ideas)


# ---------------- pacing ----------------
def test_rate_limiter_spaces_requests():
    limiter = RateLimiter("test", max_concurrent=4, min_interval=0.05)

    async def scenario():
        started = time.monotonic()
        async def one():
            async with limiter:
                return None
        await asyncio.gather(*(one() for _ in range(6)))
        return time.monotonic() - started

    # Six requests, 50ms apart, cannot complete instantly.
    assert run(scenario()) >= 0.2


def test_rate_limiter_caps_concurrency():
    limiter = RateLimiter("test", max_concurrent=2, min_interval=0.0)
    peak = {"now": 0, "max": 0}

    async def scenario():
        async def one():
            async with limiter:
                peak["now"] += 1
                peak["max"] = max(peak["max"], peak["now"])
                await asyncio.sleep(0.02)
                peak["now"] -= 1
        await asyncio.gather(*(one() for _ in range(8)))

    run(scenario())
    assert peak["max"] <= 2


def test_a_429_pauses_the_whole_source():
    limiter = RateLimiter("test", max_concurrent=4, min_interval=0.0)
    assert limiter.paused_for == 0.0
    limiter.penalise(5.0)
    assert 4.0 < limiter.paused_for <= 5.0


def test_retry_after_header_is_honoured():
    assert retry_after_seconds(httpx.Headers({"retry-after": "45"})) == 45.0
    fallback = retry_after_seconds(httpx.Headers({}))
    assert 15 <= fallback <= 35, "a missing header should still back off"


def test_a_429_gives_up_immediately_rather_than_sleeping():
    """Sleeping and retrying per symbol is what made a scan never finish.

    A rate limit is definitive: drop the source and let the others answer.
    """
    from backend.providers.yahoo import YahooProvider
    calls = {"n": 0}

    def handler(request):
        url = str(request.url)
        if "getcrumb" in url:
            return httpx.Response(200, text="crumb")
        if "fc.yahoo.com" in url:
            return httpx.Response(200, text="ok")
        calls["n"] += 1
        return httpx.Response(429, headers={"retry-after": "30"}, text="slow down")

    provider = YahooProvider()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    started = time.monotonic()
    assert run(provider.quotes(["AAPL"])) == {}
    assert time.monotonic() - started < 2.0, "a 429 must not block on a sleep"
    assert calls["n"] == 1, f"the request was retried {calls['n']} times"
    assert provider.limiter.paused_for > 0, "the source should be paused"


def test_one_429_puts_the_source_straight_into_cooldown(fresh_db):
    """Retrying a throttled source for another 80 symbols deepens the throttle."""
    class Throttled:
        name = "throttled"
        realtime = False
        def __init__(self): self.calls = 0; self.last_error = None
        async def quotes(self, symbols):
            self.calls += 1
            self.last_error = "HTTP 429 rate limited (paused 30s)"
            return {}
        async def history(self, s, days=180, interval="1d"):
            from backend.providers.base import Bars
            return Bars(s.upper(), [])
        async def expirations(self, s): return []
        async def chain(self, s, e): return None
        async def news(self, s, limit=30): return []
        async def close(self): return None

    throttled = Throttled()
    md = MarketData([throttled, CountingProvider()], use_store=False)

    async def scenario():
        for i in range(8):
            await md.quotes([f"SYM{i}"])

    run(scenario())
    assert throttled.calls == 1, (
        f"a throttled source was called {throttled.calls} times")
    assert md._is_open(throttled)


def test_the_app_works_when_yahoo_throttles_everything(fresh_db):
    """The real failure: Yahoo rate limiting every request.

    The other sources are independent of it, so a scan must still produce
    results rather than hanging or coming back empty.
    """
    class AlwaysThrottled:
        name = "yahoo"
        realtime = False
        def __init__(self): self.last_error = "HTTP 429 rate limited"
        async def quotes(self, symbols): return {}
        async def history(self, s, days=180, interval="1d"):
            from backend.providers.base import Bars
            return Bars(s.upper(), [])
        async def expirations(self, s): return []
        async def chain(self, s, e): return None
        async def news(self, s, limit=30): return []
        async def close(self): return None

    backup = CountingProvider()
    backup.name = "cboe"
    md = MarketData([AlwaysThrottled(), backup], use_store=False)

    started = time.monotonic()
    result = run(Scanner(md).scan("core", 0, 3, min_conviction=20, limit=40,
                                  include_news=False))
    elapsed = time.monotonic() - started

    assert result.ideas, "the scan produced nothing despite a working source"
    assert result.equities
    assert elapsed < 30, f"the scan took {elapsed:.0f}s"
    assert not result.data_status["missing_symbols"]


def test_a_scan_finishes_even_when_a_source_hangs(fresh_db):
    """A stalled provider must cost coverage, not block the scan forever."""
    import backend.engine.scanner as scanner_module

    class Hanging(CountingProvider):
        name = "hanging"
        async def history(self, symbol, days=180, interval="1d"):
            await asyncio.sleep(600)          # never returns

    md = MarketData(Hanging(), use_store=False)
    original = scanner_module.SCAN_DEADLINE_SECONDS
    scanner_module.SCAN_DEADLINE_SECONDS = 3.0
    try:
        started = time.monotonic()
        result = run(Scanner(md).scan("core", 0, 3, include_news=False))
        elapsed = time.monotonic() - started
    finally:
        scanner_module.SCAN_DEADLINE_SECONDS = original

    assert elapsed < 15, f"the scan hung for {elapsed:.0f}s"
    assert isinstance(result.errors, list)


# ---------------- capability routing ----------------
def test_options_prefer_the_source_that_returns_a_whole_chain():
    """CBOE returns every expiration in one response; Yahoo needs one each."""
    from backend.providers.registry import build_providers
    md = MarketData(build_providers("auto"), use_store=False)
    assert md._ordered_for("options")[0].name == "cboe"
    assert md._ordered_for("quotes")[0].name == "yahoo"


def test_explicit_configuration_is_not_overridden():
    from backend.providers.registry import build_providers
    md = MarketData(build_providers("stooq,cboe"), use_store=False)
    assert [p.name for p in md._ordered_for("options")] == ["cboe", "stooq"]
    assert [p.name for p in md._ordered_for("history")] == ["stooq", "cboe"]
