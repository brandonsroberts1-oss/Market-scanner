"""Signal extraction, conviction scoring and end-to-end scan behaviour."""
import asyncio
import json
import math

import pytest

from backend.engine.conviction import assess
from backend.engine.equities import rank_equity
from backend.engine.scanner import Scanner, _pick_expirations
from backend.engine.signals import build_signals
from backend.engine.universe import PRESETS, get_universe
from backend.providers.base import Bars, Quote


def run(coro):
    return asyncio.run(coro)


# ---------------- universe ----------------
def test_presets_resolve_to_unique_symbols():
    for name in PRESETS:
        symbols = get_universe(name)
        assert symbols
        assert len(symbols) == len(set(symbols)), f"{name} contains duplicates"


def test_custom_ticker_list_is_accepted():
    assert get_universe("spy, nvda amd") == ["SPY", "NVDA", "AMD"]


def test_empty_universe_falls_back_to_core():
    assert get_universe("") == get_universe("core")


# ---------------- expiry selection ----------------
def test_pick_expirations_filters_to_the_dte_window():
    from datetime import date
    today = date(2026, 8, 31)
    exps = ["2026-08-31", "2026-09-01", "2026-09-03", "2026-09-10", "2026-10-16"]
    assert _pick_expirations(exps, 0, 3, today) == ["2026-08-31", "2026-09-01", "2026-09-03"]
    assert _pick_expirations(exps, 1, 1, today) == ["2026-09-01"]
    assert _pick_expirations(exps, 40, 60, today) == ["2026-10-16"]


def test_pick_expirations_ignores_malformed_dates():
    from datetime import date
    assert _pick_expirations(["not-a-date", "2026-09-01"], 0, 3, date(2026, 8, 31)) \
        == ["2026-09-01"]


# ---------------- signals ----------------
def test_signals_are_json_safe(market):
    """NaN leaks from numpy would break JSON serialisation in the API."""
    bars = run(market.history("NVDA", 180))
    quote = (run(market.quotes(["NVDA"])))["NVDA"]
    chain = run(market.chain("NVDA", run(market.expirations("NVDA"))[3]))
    sig = build_signals("NVDA", bars, quote, chain, run(market.history("SPY", 180)).closes)
    encoded = json.dumps(sig.to_dict())
    assert "NaN" not in encoded and "Infinity" not in encoded
    for value in sig.to_dict().values():
        if isinstance(value, float):
            assert math.isfinite(value)


def test_signals_degrade_gracefully_without_history():
    sig = build_signals("XYZ", Bars("XYZ", []), None)
    assert sig.bars_available == 0
    assert sig.rsi is None
    assessment = assess(sig)
    assert assessment.conviction == 0
    assert assessment.warnings


def test_signals_without_a_chain_have_no_iv(market):
    bars = run(market.history("SPY", 180))
    sig = build_signals("SPY", bars, None, None)
    assert sig.atm_iv is None
    assert assess(sig).iv_regime == "unknown"


# ---------------- conviction ----------------
def test_conviction_is_bounded_and_explained(market):
    spy = run(market.history("SPY", 180)).closes
    for symbol in ("SPY", "NVDA", "TSLA", "AAPL", "COIN"):
        bars = run(market.history(symbol, 180))
        quote = (run(market.quotes([symbol])))[symbol]
        chain = run(market.chain(symbol, run(market.expirations(symbol))[3]))
        a = assess(build_signals(symbol, bars, quote, chain, spy))
        assert 0 <= a.conviction <= 100
        assert -1.0 <= a.bias <= 1.0
        assert 0.0 <= a.agreement <= 1.0
        assert 0.0 <= a.quality <= 1.0
        assert a.direction in ("bullish", "bearish", "neutral")
        assert a.factors, "every assessment must explain itself"
        assert all(f.detail for f in a.factors), "every factor needs a readable reason"


def test_direction_agrees_with_bias(market):
    spy = run(market.history("SPY", 180)).closes
    for symbol in ("SPY", "NVDA", "TSLA", "AAPL", "AMD", "META"):
        bars = run(market.history(symbol, 180))
        quote = (run(market.quotes([symbol])))[symbol]
        a = assess(build_signals(symbol, bars, quote, None, spy))
        if a.direction == "bullish":
            assert a.bias > 0
        elif a.direction == "bearish":
            assert a.bias < 0


def test_illiquid_options_cut_conviction(market):
    """A perfect signal on an untradeable chain must not rank as a good trade."""
    bars = run(market.history("AAPL", 180))
    quote = (run(market.quotes(["AAPL"])))["AAPL"]
    chain = run(market.chain("AAPL", run(market.expirations("AAPL"))[3]))
    sig = build_signals("AAPL", bars, quote, chain)

    sig.option_spread_pct, sig.option_oi = 0.02, 50_000.0
    liquid = assess(sig).conviction
    sig.option_spread_pct, sig.option_oi = 0.40, 40.0
    illiquid = assess(sig)

    assert illiquid.conviction < liquid
    assert illiquid.quality < 1.0
    assert any("spread" in w.lower() or "open interest" in w.lower()
               for w in illiquid.warnings)


# ---------------- equities ----------------
def test_equity_ranking_produces_sane_levels(market):
    spy = run(market.history("SPY", 180)).closes
    bars = run(market.history("AAPL", 180))
    quote = (run(market.quotes(["AAPL"])))["AAPL"]
    idea = rank_equity(build_signals("AAPL", bars, quote, None, spy), bars.closes)
    assert idea is not None
    assert 0 <= idea.score <= 100
    if idea.direction == "long":
        assert idea.stop < idea.entry < idea.target
    elif idea.direction == "short":
        assert idea.target < idea.entry < idea.stop
    assert idea.risk_reward == pytest.approx(2.0, abs=0.05)   # 3 ATR target on a 1.5 ATR stop


# ---------------- full scan ----------------
def test_scan_returns_a_complete_payload(market):
    result = run(Scanner(market).scan("core", 0, 3, 0, limit=25))
    assert result.universe
    assert result.narrative
    assert isinstance(result.ideas, list)
    assert isinstance(result.equities, list)
    assert result.elapsed_seconds >= 0
    json.dumps(result.to_dict())        # must survive API serialisation


def test_scan_ideas_are_internally_consistent(market):
    result = run(Scanner(market).scan("core", 0, 3, 0, limit=40))
    assert result.ideas, "expected at least one idea from the demo universe"
    for idea in result.ideas:
        assert idea["legs"], "an idea with no contracts is not tradeable"
        assert idea["max_loss"] > 0
        assert 0 <= idea["prob_profit"] <= 1
        assert idea["expiration"]
        assert idea["rationale"] and idea["exit_plan"]
        # Every leg must carry a real fill price.
        assert all(l["price"] > 0 for l in idea["legs"])
        # A defined-risk structure's max loss must not exceed its width.
        if idea["risk_reward"] is not None:
            assert idea["max_profit"] > 0


def test_scan_respects_the_conviction_floor(market):
    result = run(Scanner(market).scan("core", 0, 3, min_conviction=60, limit=40))
    assert all(i["conviction"] >= 60 for i in result.ideas)


def test_scan_respects_the_dte_window(market):
    result = run(Scanner(market).scan("core", 0, 2, 0, limit=40))
    assert all(0 <= i["dte"] <= 2 for i in result.ideas)


def test_scan_is_ranked_by_score(market):
    result = run(Scanner(market).scan("core", 0, 3, 0, limit=40))
    scores = [i["score"] for i in result.ideas]
    assert scores == sorted(scores, reverse=True)


def test_scan_handles_an_unknown_symbol_without_crashing(market):
    result = run(Scanner(market).scan("SPY,ZZZZNOTREAL", 0, 3, 0, limit=10))
    assert isinstance(result.ideas, list)
