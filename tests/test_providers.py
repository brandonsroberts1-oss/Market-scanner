"""Provider normalisation, OCC symbols, caching and fallback behaviour."""
import asyncio
from datetime import date

import pytest

from backend.providers.base import (Bars, NewsItem, OptionChain, OptionContract,
                                    Quote, occ_symbol, parse_occ)
from backend.providers.demo import DemoProvider
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
    a = DemoProvider(as_of=date(2026, 8, 31))
    b = DemoProvider(as_of=date(2026, 8, 31))
    assert run(a.quotes(["SPY"]))["SPY"].last == run(b.quotes(["SPY"]))["SPY"].last
    assert run(a.history("NVDA", 60)).closes == run(b.history("NVDA", 60)).closes


def test_demo_history_ends_on_the_as_of_date():
    p = DemoProvider(as_of=date(2026, 8, 31))
    bars = run(p.history("SPY", 120))
    assert bars.bars[-1].date == "2026-08-31"
    assert len(bars) == 121
    assert all(b.high >= b.low for b in bars.bars)
    assert all(b.high >= b.close >= b.low for b in bars.bars)


def test_demo_history_skips_weekends():
    bars = run(DemoProvider(as_of=date(2026, 8, 31)).history("SPY", 60))
    assert all(date.fromisoformat(b.date).weekday() < 5 for b in bars.bars)


def test_demo_chain_is_priced_and_two_sided():
    p = DemoProvider(as_of=date(2026, 8, 31))
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
    p = DemoProvider(as_of=date(2026, 8, 31))
    assert run(p.chain("SPY", "1999-01-01")) is None
    assert run(p.chain("SPY", "not-a-date")) is None


def test_demo_smile_bids_up_downside_puts():
    """Equity index skew: out-of-the-money puts carry higher IV than OTM calls."""
    p = DemoProvider(as_of=date(2026, 8, 31))
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


# ---------------- fallback ----------------
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


def test_failed_provider_falls_back_and_flags_degraded():
    md = MarketData(BrokenProvider(), fallback=True)
    quotes = run(md.quotes(["SPY"]))
    assert "SPY" in quotes
    assert md.degraded, "serving simulated data must be flagged, never silent"
    assert len(run(md.history("SPY", 120))) > 0
    assert run(md.chain("SPY", run(md.expirations("SPY"))[2])) is not None


def test_fallback_can_be_disabled():
    md = MarketData(BrokenProvider(), fallback=False)
    assert run(md.quotes(["SPY"])) == {}
    assert not md.degraded


def test_healthy_provider_is_not_marked_degraded():
    md = MarketData(DemoProvider(as_of=date(2026, 8, 31)), fallback=True)
    run(md.quotes(["SPY"]))
    assert not md.degraded
