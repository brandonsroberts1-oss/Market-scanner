"""Provider normalisation, OCC symbols, caching and fallback behaviour."""
import asyncio
from datetime import date

import pytest

from backend.providers.base import (Bars, NewsItem, OptionChain, OptionContract,
                                    Quote, occ_symbol, parse_occ)
from tests.simulated_provider import SimulatedProvider
from backend.providers.registry import MarketData, TTLCache


def run(coro):
    return asyncio.run(coro)


# ---------------- OCC symbols ----------------
def test_occ_symbol_round_trips():
    sym = occ_symbol("SPY", "2026-09-02", "call", 766.0)
    assert sym == "SPY260902C00766000"
    assert parse_occ(sym) == {"underlying": "SPY", "expiration": "2026-09-02",
                             "kind": "call", "strike": 766.0}


def test_occ_handles_fractional_strikes():
    sym = occ_symbol("AMD", "2026-12-18", "put", 162.5)
    assert parse_occ(sym)["strike"] == pytest.approx(162.5)


def test_parse_occ_rejects_garbage():
    for bad in ("NOTREAL", "", "SPY123", "SPY260902X00766000"):
        assert parse_occ(bad) is None


# ---------------- normalised types ----------------
def test_quote_change_and_mid():
    q = Quote("SPY", last=765.0, bid=764.9, ask=765.1, previous_close=769.35)
    assert q.change == pytest.approx(-4.35)
    assert q.change_pct == pytest.approx(-0.5654, abs=1e-3)
    assert q.mid == pytest.approx(765.0)


def test_quote_mid_falls_back_to_last_without_a_book():
    assert Quote("SPY", last=765.0, bid=0, ask=0).mid == 765.0
    assert Quote("SPY", last=765.0).mid == 765.0


def test_quote_change_is_none_without_a_previous_close():
    q = Quote("SPY", last=765.0)
    assert q.change is None and q.change_pct is None


def test_contract_mid_and_spread():
    c = OptionContract("X", "SPY", "2026-09-02", 766, "call", bid=2.71, ask=2.73)
    assert c.mid == pytest.approx(2.72)          # matches the real broker mark
    assert c.spread == pytest.approx(0.02)
    assert c.spread_pct == pytest.approx(0.00735, abs=1e-4)


def test_contract_without_a_market_reports_no_spread():
    c = OptionContract("X", "SPY", "2026-09-02", 766, "call", bid=None, ask=None, last=3.0)
    assert c.mid == 3.0
    assert c.spread_pct is None


# ---------------- demo provider ----------------
def test_demo_provider_is_deterministic():
    a = SimulatedProvider(as_of=date(2026, 8, 31))
    b = SimulatedProvider(as_of=date(2026, 8, 31))
    assert run(a.quotes(["SPY"]))["SPY"].last == run(b.quotes(["SPY"]))["SPY"].last
    assert run(a.history("NVDA", 60)).closes == run(b.history("NVDA", 60)).closes


def test_demo_history_ends_on_the_as_of_date():
    p = SimulatedProvider(as_of=date(2026, 8, 31))
    bars = run(p.history("SPY", 120))
    assert bars.bars[-1].date == "2026-08-31"
    assert len(bars) == 121
    assert all(b.high >= b.low for b in bars.bars)
    assert all(b.high >= b.close >= b.low for b in bars.bars)


def test_demo_history_skips_weekends():
    bars = run(SimulatedProvider(as_of=date(2026, 8, 31)).history("SPY", 60))
    assert all(date.fromisoformat(b.date).weekday() < 5 for b in bars.bars)


def test_demo_chain_is_priced_and_two_sided():
    p = SimulatedProvider(as_of=date(2026, 8, 31))
    exps = run(p.expirations("SPY"))
    chain = run(p.chain("SPY", exps[3]))
    assert chain.calls and chain.puts
    for c in chain.all():
        assert c.ask >= c.bid >= 0
        assert c.implied_volatility > 0
        assert c.open_interest >= 0
    # Call delta falls as strikes rise; put delta rises toward zero.
    calls = sorted(chain.calls, key=lambda c: c.strike)
    assert calls[0].delta > calls[-1].delta


def test_demo_chain_rejects_an_unlisted_expiration():
    p = SimulatedProvider(as_of=date(2026, 8, 31))
    assert run(p.chain("SPY", "1999-01-01")) is None
    assert run(p.chain("SPY", "not-a-date")) is None


def test_demo_smile_bids_up_downside_puts():
    """Equity index skew: out-of-the-money puts carry higher IV than OTM calls."""
    p = SimulatedProvider(as_of=date(2026, 8, 31))
    chain = run(p.chain("SPY", run(p.expirations("SPY"))[5]))
    spot = chain.underlying_price
    otm_put = min((c for c in chain.puts if c.strike < spot * 0.97),
                  key=lambda c: abs(c.strike - spot * 0.95))
    otm_call = min((c for c in chain.calls if c.strike > spot * 1.03),
                   key=lambda c: abs(c.strike - spot * 1.05))
    assert otm_put.implied_volatility > otm_call.implied_volatility


# ---------------- cache ----------------
def test_ttl_cache_serves_within_the_window():
    calls = []

    async def factory():
        calls.append(1)
        return len(calls)

    async def scenario():
        cache = TTLCache()
        first = await cache.get_or_set("k", 60, factory)
        second = await cache.get_or_set("k", 60, factory)
        return first, second

    first, second = run(scenario())
    assert first == second == 1
    assert len(calls) == 1


def test_ttl_cache_single_flights_concurrent_callers():
    """Twenty simultaneous callers must produce one upstream request, not twenty."""
    calls = []

    async def factory():
        calls.append(1)
        await asyncio.sleep(0.01)
        return "value"

    async def scenario():
        cache = TTLCache()
        return await asyncio.gather(*(cache.get_or_set("k", 60, factory) for _ in range(20)))

    results = run(scenario())
    assert all(r == "value" for r in results)
    assert len(calls) == 1


def test_ttl_cache_refetches_after_expiry():
    calls = []

    async def factory():
        calls.append(1)
        return len(calls)

    async def scenario():
        cache = TTLCache()
        await cache.get_or_set("k", 0.0, factory)
        await cache.get_or_set("k", 0.0, factory)

    run(scenario())
    assert len(calls) == 2


def test_cache_invalidation_by_prefix():
    async def scenario():
        cache = TTLCache()
        await cache.get_or_set("q:SPY", 60, lambda: _const(1))
        await cache.get_or_set("h:SPY", 60, lambda: _const(2))
        cache.invalidate("q:")
        return cache.stats()["entries"]

    async def _const(v):
        return v

    assert run(scenario()) == 1


# ---------------- last-known-good fallback ----------------
class BrokenProvider:
    """Stands in for a vendor that is down or rate limiting."""
    name = "broken"
    realtime = True

    async def quotes(self, symbols):
        raise RuntimeError("upstream is down")

    async def history(self, symbol, days=180, interval="1d"):
        return Bars(symbol.upper(), [])

    async def expirations(self, symbol):
        return []

    async def chain(self, symbol, expiration):
        return None

    async def news(self, symbols, limit=30):
        return []

    async def close(self):
        return None


class CountingBrokenProvider(BrokenProvider):
    """Counts how many times a dead vendor actually gets called."""

    def __init__(self):
        self.calls = 0

    async def quotes(self, symbols):
        self.calls += 1
        raise RuntimeError("upstream is down")

    async def history(self, symbol, days=180, interval="1d"):
        self.calls += 1
        return Bars(symbol.upper(), [])


def test_a_dead_provider_never_invents_a_price(fresh_db):
    """The core guarantee: no data means no number, not a made-up one."""
    md = MarketData(BrokenProvider(), use_store=True)
    quotes = run(md.quotes(["SPY"]))
    assert quotes == {}, "a price was produced with no data source"
    assert "SPY" in md.missing_symbols


def test_store_serves_the_last_real_price_when_the_vendor_dies(fresh_db):
    live = MarketData(SimulatedProvider(as_of=date(2026, 8, 31)), use_store=True)
    real = run(live.quotes(["SPY"]))["SPY"]

    dead = MarketData(BrokenProvider(), use_store=True)
    recovered = run(dead.quotes(["SPY"]))["SPY"]

    assert recovered.last == real.last, "the cached price should be the one really fetched"
    assert recovered.stale is True
    assert recovered.as_of, "stale data must carry the time it was captured"
    assert "SPY" in dead.stale_symbols


def test_stale_data_is_reported_not_hidden(fresh_db):
    live = MarketData(SimulatedProvider(as_of=date(2026, 8, 31)), use_store=True)
    run(live.quotes(["SPY", "QQQ"]))

    dead = MarketData(BrokenProvider(), use_store=True)
    run(dead.quotes(["SPY", "QQQ"]))
    status = dead.data_status()
    assert status["stale_count"] == 2
    assert set(status["stale_symbols"]) == {"SPY", "QQQ"}
    assert status["stale_age"]
    assert dead.serving_stale


def test_a_fresh_fetch_clears_the_stale_flag(fresh_db):
    md = MarketData(SimulatedProvider(as_of=date(2026, 8, 31)), use_store=True)
    run(md.quotes(["SPY"]))
    assert not md.serving_stale
    assert md.data_status()["stale_count"] == 0


def test_history_and_chains_also_fall_back_to_real_cached_data(fresh_db):
    live = MarketData(SimulatedProvider(as_of=date(2026, 8, 31)), use_store=True)
    bars = run(live.history("SPY", 120))
    exps = run(live.expirations("SPY"))
    chain = run(live.chain("SPY", exps[2]))
    assert len(bars) and exps and chain

    dead = MarketData(BrokenProvider(), use_store=True)
    assert len(run(dead.history("SPY", 120))) == len(bars)
    assert run(dead.chain("SPY", exps[2])) is not None
    assert "SPY" in dead.stale_symbols


def test_cached_expirations_drop_dates_that_have_passed(fresh_db):
    """A remembered expiry list must never offer a date already in the past."""
    from backend.providers import store
    past, future = "2020-01-17", "2099-01-15"
    store.put("exp:ZZZ", "expirations", [past, future], "ZZZ")

    dead = MarketData(BrokenProvider(), use_store=True)
    assert run(dead.expirations("ZZZ")) == [future]


def test_store_can_be_disabled(fresh_db):
    live = MarketData(SimulatedProvider(as_of=date(2026, 8, 31)), use_store=True)
    run(live.quotes(["SPY"]))
    dead = MarketData(BrokenProvider(), use_store=False)
    assert run(dead.quotes(["SPY"])) == {}


# ---------------- circuit breaker ----------------
def test_circuit_breaker_stops_calling_a_dead_provider(fresh_db):
    """Without this, a 60-symbol scan waits out 60 separate timeouts."""
    broken = CountingBrokenProvider()
    md = MarketData(broken, use_store=False)
    md.FAILURE_THRESHOLD = 3

    async def scenario():
        for i in range(12):
            await md.quotes([f"SYM{i}"])       # distinct keys defeat the cache

    run(scenario())
    assert md.circuit_open
    assert broken.calls == 3, f"provider was called {broken.calls} times after failing"


def test_circuit_breaker_stays_closed_for_a_healthy_provider(fresh_db):
    md = MarketData(SimulatedProvider(as_of=date(2026, 8, 31)), use_store=False)

    async def scenario():
        for sym in ("SPY", "QQQ", "AAPL", "NVDA", "TSLA"):
            await md.quotes([sym])

    run(scenario())
    assert not md.circuit_open
    assert not md.serving_stale


def test_a_success_resets_the_failure_count(fresh_db):
    md = MarketData(SimulatedProvider(as_of=date(2026, 8, 31)), use_store=False)
    md._consecutive_failures = 2
    run(md.quotes(["SPY"]))
    assert md._consecutive_failures == 0


def test_breaker_still_serves_cached_data_while_open(fresh_db):
    """Tripping the breaker must degrade the app, never break it."""
    live = MarketData(SimulatedProvider(as_of=date(2026, 8, 31)), use_store=True)
    run(live.quotes(["SPY"]))
    run(live.history("SPY", 120))

    md = MarketData(CountingBrokenProvider(), use_store=True)
    md.FAILURE_THRESHOLD = 2

    async def scenario():
        for i in range(6):
            await md.quotes([f"S{i}"])
        return await md.quotes(["SPY"]), await md.history("SPY", 120)

    quotes, bars = run(scenario())
    assert quotes["SPY"].last > 0 and quotes["SPY"].stale
    assert len(bars) > 60


# ---------------- provider selection ----------------
def test_simulated_providers_cannot_be_configured():
    """No setting may put fabricated prices in front of a user."""
    from backend.providers.registry import ProviderUnavailable, build_provider
    for name in ("demo", "simulated", "fake", "offline"):
        with pytest.raises(ProviderUnavailable, match="Simulated"):
            build_provider(name)


def test_unknown_provider_is_rejected_with_the_valid_options():
    from backend.providers.registry import ProviderUnavailable, build_provider
    with pytest.raises(ProviderUnavailable, match="yahoo"):
        build_provider("nonsense")


def test_tradier_without_a_token_is_an_explicit_error():
    from backend import config
    from backend.providers.registry import ProviderUnavailable, build_provider
    original = config.settings.tradier_token
    config.settings.tradier_token = ""
    try:
        with pytest.raises(ProviderUnavailable, match="TRADIER_TOKEN"):
            build_provider("tradier")
    finally:
        config.settings.tradier_token = original


def test_yahoo_is_the_default_without_a_token():
    from backend import config
    from backend.providers.registry import build_provider
    original = config.settings.tradier_token
    config.settings.tradier_token = ""
    try:
        assert build_provider("auto").name == "yahoo"
    finally:
        config.settings.tradier_token = original


# ---------------- expirations come only from the vendor ----------------
def test_expirations_are_never_synthesised(fresh_db):
    """Regression guard for an invented expiry date.

    A simulated feed generated every weekday as an expiration, which produced a
    Tuesday 1 September expiry for AAPL - a date that does not exist on the
    real chain (AAPL lists Aug 31, Sep 2, Sep 4, Sep 9, Sep 11...). Expirations
    must only ever come from the vendor's own list.
    """
    real_dates = ["2026-08-31", "2026-09-02", "2026-09-04", "2026-09-09", "2026-09-11"]

    class VendorProvider(BrokenProvider):
        name = "vendor"

        async def expirations(self, symbol):
            return list(real_dates)

        async def chain(self, symbol, expiration):
            if expiration not in real_dates:
                raise AssertionError(f"asked for an unlisted expiration: {expiration}")
            return None

    md = MarketData(VendorProvider(), use_store=True)
    assert run(md.expirations("AAPL")) == real_dates
    assert "2026-09-01" not in run(md.expirations("AAPL"))


def test_scanner_only_uses_expirations_the_vendor_lists(fresh_db):
    """End to end: no idea may carry an expiry outside the vendor's list."""
    import asyncio as _asyncio
    from backend.engine.scanner import Scanner

    provider = SimulatedProvider(as_of=date(2026, 8, 31))
    md = MarketData(provider, use_store=False)
    result = run(Scanner(md).scan("core", 0, 3, 0, limit=30, include_news=False))

    async def listed(symbol):
        return set(await provider.expirations(symbol))

    for idea in result.ideas:
        allowed = run(listed(idea["symbol"]))
        assert idea["expiration"] in allowed, (
            f"{idea['symbol']} idea uses {idea['expiration']}, which the provider "
            f"does not list")
        for leg in idea["legs"]:
            assert leg["expiration"] in allowed
