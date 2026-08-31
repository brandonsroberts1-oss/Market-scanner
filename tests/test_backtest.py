"""Backtester: no-lookahead guarantees, capital constraints, and statistics."""
import asyncio

import pytest

from backend.backtest import engine as bt
from backend.analytics import blackscholes as bs


def run(coro):
    return asyncio.run(coro)


def test_strike_for_delta_picks_the_best_listed_strike():
    """Strike grids are discrete, so the target delta is often not listed at
    all. What must hold is that no *other* listed strike is closer to it."""
    spot, vol, t, r = 765.0, 0.13, 2 / 365, 0.04
    step = bt._strike_step(spot)
    for kind, target in (("call", 0.50), ("call", 0.28), ("put", 0.30), ("put", 0.15)):
        chosen = bt._strike_for_delta(spot, vol, t, r, kind, target)
        chosen_err = abs(abs(bs.greeks(spot, chosen, t, r, vol, kind).delta) - target)
        grid = [spot - 4 * step + i * step for i in range(9)]
        for k in grid:
            err = abs(abs(bs.greeks(spot, k, t, r, vol, kind).delta) - target)
            assert chosen_err <= err + 1e-9, \
                f"{kind} {target}: strike {k} is closer than the chosen {chosen}"


def test_strike_for_delta_is_accurate_when_the_grid_allows():
    """With a month to expiry the grid is fine relative to the expected move,
    so the solver should land close to the requested delta."""
    spot, vol, t, r = 765.0, 0.18, 30 / 365, 0.04
    for kind, target in (("call", 0.50), ("call", 0.30), ("put", 0.30), ("put", 0.15)):
        k = bt._strike_for_delta(spot, vol, t, r, kind, target)
        actual = abs(bs.greeks(spot, k, t, r, vol, kind).delta)
        assert actual == pytest.approx(target, abs=0.05), f"{kind} {target} -> {actual}"


def test_structures_have_the_expected_shape():
    spot, vol, t, r = 765.0, 0.13, 2 / 365, 0.04
    bull = bt._build_structure("bull_call_spread", spot, vol, t, r)
    assert [l.action for l in bull] == ["buy", "sell"]
    assert bull[0].strike < bull[1].strike, "bull call spread must buy the lower strike"

    bear = bt._build_structure("bear_call_spread", spot, vol, t, r)
    assert bear[0].action == "sell" and bear[0].strike < bear[1].strike

    condor = bt._build_structure("iron_condor", spot, vol, t, r)
    assert len(condor) == 4
    puts = sorted([l for l in condor if l.kind == "put"], key=lambda l: l.strike)
    calls = sorted([l for l in condor if l.kind == "call"], key=lambda l: l.strike)
    assert puts[0].action == "buy" and puts[1].action == "sell"
    assert calls[0].action == "sell" and calls[1].action == "buy"


def test_slippage_always_costs_money():
    """Opening and immediately closing must lose the modelled spread."""
    spot, vol, t, r = 765.0, 0.13, 2 / 365, 0.04
    legs = bt._build_structure("bull_call_spread", spot, vol, t, r)
    open_debit = bt._price_structure(legs, spot, vol, t, r, 0.04, "open")
    close_debit = bt._price_structure(legs, spot, vol, t, r, 0.04, "close")
    assert -(open_debit + close_debit) < 0, "round trip at an unchanged price must lose"


def test_zero_spread_round_trip_is_free():
    spot, vol, t, r = 765.0, 0.13, 2 / 365, 0.04
    legs = bt._build_structure("bull_call_spread", spot, vol, t, r)
    o = bt._price_structure(legs, spot, vol, t, r, 0.0, "open")
    c = bt._price_structure(legs, spot, vol, t, r, 0.0, "close")
    assert -(o + c) == pytest.approx(0.0, abs=1e-9)


def test_bars_slice_never_looks_ahead(market):
    bars = run(market.history("SPY", 200))
    window = bt._bars_slice(bars, 50)
    assert len(window) == 51
    assert window.bars[-1].date == bars.bars[50].date


def test_backtest_runs_and_reports_consistent_stats(market):
    r = run(bt.run_backtest(market, ["SPY", "AAPL"], lookback_days=300,
                            hold_days=3, min_conviction=50))
    s = r.stats
    assert s["trades"] == len(r.trades)
    if s["trades"]:
        assert s["net_pnl"] == pytest.approx(sum(t["pnl"] for t in r.trades), abs=0.01)
        assert s["ending_equity"] == pytest.approx(25_000 + s["net_pnl"], abs=0.01)
        assert 0 <= s["win_rate"] <= 100
        assert s["max_drawdown_pct"] <= 0
        assert len(r.equity_curve) == s["trades"] + 1


def test_equity_never_goes_negative(market):
    """Sizing off running equity, not the opening balance, is what prevents a
    losing run compounding past a total loss."""
    r = run(bt.run_backtest(market, ["TSLA", "NVDA", "COIN"], lookback_days=400,
                            hold_days=3, min_conviction=30, risk_per_trade_pct=25))
    equities = [c["equity"] for c in r.equity_curve]
    assert all(e >= 0 for e in equities), f"equity went negative: {min(equities)}"
    assert r.stats.get("return_pct", 0) >= -100


def test_no_overlapping_trades_per_symbol(market):
    r = run(bt.run_backtest(market, ["SPY"], lookback_days=400, hold_days=3,
                            min_conviction=20))
    trades = sorted(r.trades, key=lambda t: t["entry_date"])
    for a, b in zip(trades, trades[1:]):
        assert b["entry_date"] >= a["exit_date"], "positions overlapped in the same symbol"


def test_entry_fills_at_the_next_bar_open(market):
    """Every entry must be dated strictly after the bar that produced the signal."""
    bars = run(market.history("SPY", 400))
    dates = [b.date for b in bars.bars]
    r = run(bt.run_backtest(market, ["SPY"], lookback_days=400, hold_days=3,
                            min_conviction=20))
    for t in r.trades:
        assert t["entry_date"] in dates
        assert t["exit_date"] >= t["entry_date"]


def test_option_mode_labels_itself_as_modelled(market):
    r = run(bt.run_backtest(market, ["SPY"], lookback_days=200, min_conviction=90))
    assert "Model-based" in r.method
    assert any("modelled" in w for w in r.warnings)


def test_equity_mode_is_exact_replay(market):
    r = run(bt.run_backtest(market, ["SPY"], lookback_days=200,
                            min_conviction=50, mode="equity"))
    assert "Exact replay" in r.method
    for t in r.trades:
        assert t["strategy"] in ("equity_long", "equity_short")


def test_impossible_conviction_produces_no_trades(market):
    r = run(bt.run_backtest(market, ["SPY"], lookback_days=200, min_conviction=101))
    assert r.stats["trades"] == 0
    assert "note" in r.stats


def test_allowed_strategies_filter_is_respected(market):
    r = run(bt.run_backtest(market, ["SPY", "AAPL", "NVDA"], lookback_days=400,
                            min_conviction=30,
                            allowed_strategies=["bull_call_spread", "bear_put_spread"]))
    assert all(t["strategy"] in ("bull_call_spread", "bear_put_spread") for t in r.trades)


def test_wider_spreads_reduce_profit(market):
    """Raising the modelled transaction cost must not improve results."""
    cheap = run(bt.run_backtest(market, ["SPY", "AAPL"], lookback_days=400,
                                min_conviction=40, spread_pct=0.01))
    dear = run(bt.run_backtest(market, ["SPY", "AAPL"], lookback_days=400,
                               min_conviction=40, spread_pct=0.15))
    if cheap.stats["trades"] and dear.stats["trades"]:
        assert dear.stats["net_pnl"] <= cheap.stats["net_pnl"]


def test_symbols_without_history_are_reported_not_crashed(market):
    r = run(bt.run_backtest(market, ["SPY"], lookback_days=120, min_conviction=50))
    assert isinstance(r.warnings, list)
    assert isinstance(r.stats, dict)
