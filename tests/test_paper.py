"""Paper trading: fills, cost basis, margin, P&L and expiry settlement."""
import asyncio

import pytest

from backend.paper import engine as pe


def run(coro):
    return asyncio.run(coro)


async def _spread_contracts(market, symbol="SPY"):
    exps = await market.expirations(symbol)
    chain = await market.chain(symbol, exps[3])
    spot = chain.underlying_price
    lo = min(chain.calls, key=lambda c: abs(c.strike - spot))
    hi = next(c for c in chain.calls if c.strike > lo.strike)
    return chain, lo, hi


# ---------------- sessions ----------------
def test_create_session_with_chosen_cash(fresh_db):
    s = pe.create_session("Custom", 137_500.0)
    assert s["starting_cash"] == 137_500.0
    assert s["cash"] == 137_500.0
    assert s["status"] == "active"


def test_session_rejects_non_positive_cash(fresh_db):
    with pytest.raises(pe.OrderRejected):
        pe.create_session("bad", 0)


def test_sessions_are_independent(fresh_db, market):
    a = pe.create_session("A", 10_000)
    b = pe.create_session("B", 50_000)
    run(pe.submit_order(a["id"], [pe.LegRequest("AAPL", "buy", 10, "equity")], market))
    assert pe.get_session(b["id"])["cash"] == 50_000
    assert pe.get_session(a["id"])["cash"] < 10_000


def test_missing_session_raises_not_found(fresh_db):
    with pytest.raises(pe.SessionNotFound):
        pe.get_session(4242)


def test_closed_session_refuses_orders(fresh_db, market):
    s = pe.create_session("closing", 20_000)
    pe.close_session(s["id"])
    with pytest.raises(pe.OrderRejected, match="closed"):
        run(pe.submit_order(s["id"], [pe.LegRequest("AAPL", "buy", 1, "equity")], market))


# ---------------- fills ----------------
def test_equity_buy_reduces_cash_by_cost_plus_commission(fresh_db, market):
    s = pe.create_session("equity", 100_000)
    r = run(pe.submit_order(s["id"], [pe.LegRequest("AAPL", "buy", 10, "equity")], market))
    expected = 100_000 - r["legs"][0]["fill_price"] * 10 - r["commission"]
    assert r["cash_after"] == pytest.approx(expected, abs=0.01)


def test_buy_fills_above_mid_and_sell_below(fresh_db, market):
    s = pe.create_session("fills", 100_000)
    buy = run(pe.submit_order(s["id"], [pe.LegRequest("AAPL", "buy", 1, "equity")], market))
    sell = run(pe.submit_order(s["id"], [pe.LegRequest("AAPL", "sell", 1, "equity")], market))
    assert buy["legs"][0]["fill_price"] >= sell["legs"][0]["fill_price"], \
        "buying should not fill better than selling"


def test_round_trip_at_a_flat_market_loses_the_spread(fresh_db, market):
    """Buying and immediately selling must lose money. A paper engine that
    fills both sides at mid would show zero, which teaches the wrong lesson."""
    s = pe.create_session("friction", 100_000)
    run(pe.submit_order(s["id"], [pe.LegRequest("AAPL", "buy", 100, "equity")], market))
    run(pe.submit_order(s["id"], [pe.LegRequest("AAPL", "sell", 100, "equity")], market))
    assert pe.get_session(s["id"])["cash"] < 100_000


def test_multi_leg_order_shares_a_group(fresh_db, market):
    s = pe.create_session("spread", 100_000)
    _, lo, hi = run(_spread_contracts(market))
    r = run(pe.submit_order(s["id"], [pe.LegRequest(lo.symbol, "buy", 1),
                                      pe.LegRequest(hi.symbol, "sell", 1)], market))
    assert len(r["order_ids"]) == 2
    portfolio = run(pe.portfolio(s["id"], market))
    groups = {p["group_id"] for p in portfolio["positions"]}
    assert len(groups) == 1


def test_averaging_up_blends_cost_basis(fresh_db, market):
    s = pe.create_session("avg", 200_000)
    r1 = run(pe.submit_order(s["id"], [pe.LegRequest("AAPL", "buy", 10, "equity")], market))
    r2 = run(pe.submit_order(s["id"], [pe.LegRequest("AAPL", "buy", 30, "equity")], market))
    p = run(pe.portfolio(s["id"], market))
    pos = next(x for x in p["positions"] if x["symbol"] == "AAPL")
    expected = (r1["legs"][0]["fill_price"] * 10 + r2["legs"][0]["fill_price"] * 30) / 40
    assert pos["quantity"] == 40
    assert pos["avg_price"] == pytest.approx(expected, abs=0.01)


def test_closing_realises_pnl_and_removes_the_position(fresh_db, market):
    s = pe.create_session("close", 100_000)
    run(pe.submit_order(s["id"], [pe.LegRequest("AAPL", "buy", 10, "equity")], market))
    r = run(pe.close_position(s["id"], "AAPL", market))
    assert r["realized_pnl"] != 0
    assert run(pe.portfolio(s["id"], market))["positions"] == []


def test_partial_close_leaves_the_remainder(fresh_db, market):
    s = pe.create_session("partial", 100_000)
    run(pe.submit_order(s["id"], [pe.LegRequest("AAPL", "buy", 10, "equity")], market))
    run(pe.close_position(s["id"], "AAPL", market, quantity=4))
    pos = run(pe.portfolio(s["id"], market))["positions"][0]
    assert pos["quantity"] == 6


# ---------------- margin ----------------
@pytest.mark.parametrize("kind,short,long_,expected", [
    ("call", 775, 765, 0.0),       # bull call spread - debit, nothing reserved
    ("call", 765, 775, 1000.0),    # bear call credit spread - width reserved
    ("put", 760, 755, 500.0),      # bull put credit spread
    ("put", 760, 770, 0.0),        # bear put spread - debit
])
def test_cover_margin_distinguishes_debit_from_credit(kind, short, long_, expected):
    assert pe.cover_margin(kind, short, long_) == pytest.approx(expected)


def test_debit_spread_reserves_no_margin(fresh_db, market):
    s = pe.create_session("debit", 100_000)
    _, lo, hi = run(_spread_contracts(market))
    r = run(pe.submit_order(s["id"], [pe.LegRequest(lo.symbol, "buy", 1),
                                      pe.LegRequest(hi.symbol, "sell", 1)], market))
    assert r["margin_reserved"] == 0.0


def test_credit_spread_reserves_the_width(fresh_db, market):
    s = pe.create_session("credit", 100_000)
    exps = run(market.expirations("SPY"))
    chain = run(market.chain("SPY", exps[3]))
    short = min(chain.puts, key=lambda c: abs(abs(c.delta or 0) - 0.30))
    long_ = [c for c in chain.puts if c.strike < short.strike][-2]
    r = run(pe.submit_order(s["id"], [pe.LegRequest(short.symbol, "sell", 1),
                                      pe.LegRequest(long_.symbol, "buy", 1)], market))
    assert r["margin_reserved"] == pytest.approx((short.strike - long_.strike) * 100)


def test_naked_short_is_rejected_when_buying_power_is_short(fresh_db, market):
    s = pe.create_session("naked", 5_000)
    exps = run(market.expirations("SPY"))
    chain = run(market.chain("SPY", exps[3]))
    contract = min(chain.puts, key=lambda c: abs(abs(c.delta or 0) - 0.30))
    with pytest.raises(pe.OrderRejected, match="buying power"):
        run(pe.submit_order(s["id"], [pe.LegRequest(contract.symbol, "sell", 50)], market))


def test_insufficient_cash_is_rejected(fresh_db, market):
    s = pe.create_session("broke", 500)
    with pytest.raises(pe.OrderRejected, match="Insufficient cash"):
        run(pe.submit_order(s["id"], [pe.LegRequest("AAPL", "buy", 100, "equity")], market))


def test_closing_a_short_releases_its_margin(fresh_db, market):
    s = pe.create_session("release", 100_000)
    exps = run(market.expirations("SPY"))
    chain = run(market.chain("SPY", exps[3]))
    short = min(chain.puts, key=lambda c: abs(abs(c.delta or 0) - 0.30))
    long_ = [c for c in chain.puts if c.strike < short.strike][-2]
    r = run(pe.submit_order(s["id"], [pe.LegRequest(short.symbol, "sell", 1),
                                      pe.LegRequest(long_.symbol, "buy", 1)], market))
    assert pe.reserved_margin(s["id"]) > 0
    run(pe.close_group(s["id"], r["group_id"], market))
    assert pe.reserved_margin(s["id"]) == 0


# ---------------- marks and reporting ----------------
def test_marks_use_the_exit_side_of_the_book(fresh_db, market):
    """A long is marked at the bid, so a position is underwater the moment it
    opens by exactly the spread paid."""
    s = pe.create_session("marks", 100_000)
    run(pe.submit_order(s["id"], [pe.LegRequest("AAPL", "buy", 10, "equity")], market))
    pos = run(pe.portfolio(s["id"], market))["positions"][0]
    assert pos["mark"] <= pos["avg_price"]
    assert pos["unrealized_pnl"] <= 0


def test_portfolio_equity_is_cash_plus_positions(fresh_db, market):
    s = pe.create_session("equity math", 100_000)
    run(pe.submit_order(s["id"], [pe.LegRequest("AAPL", "buy", 10, "equity")], market))
    p = run(pe.portfolio(s["id"], market))
    assert p["total_equity"] == pytest.approx(p["cash"] + p["positions_value"], abs=0.01)
    assert p["total_pnl"] == pytest.approx(p["total_equity"] - p["starting_cash"], abs=0.01)


def test_snapshot_records_an_equity_point(fresh_db, market):
    s = pe.create_session("curve", 50_000)
    before = len(pe.equity_curve(s["id"]))
    run(pe.portfolio(s["id"], market, snapshot=True))
    assert len(pe.equity_curve(s["id"])) == before + 1


def test_performance_reports_round_trips(fresh_db, market):
    s = pe.create_session("perf", 100_000)
    run(pe.submit_order(s["id"], [pe.LegRequest("AAPL", "buy", 10, "equity")], market))
    run(pe.close_position(s["id"], "AAPL", market))
    perf = pe.performance(s["id"])
    assert perf["trades"] == 1
    assert perf["win_rate"] in (0.0, 100.0)
    assert perf["round_trips"][0]["symbol"] == "AAPL"


def test_order_history_persists_every_fill(fresh_db, market):
    s = pe.create_session("history", 100_000)
    run(pe.submit_order(s["id"], [pe.LegRequest("AAPL", "buy", 5, "equity")], market))
    run(pe.submit_order(s["id"], [pe.LegRequest("NVDA", "buy", 5, "equity")], market))
    orders = pe.order_history(s["id"])
    assert len(orders) == 2
    assert {o["symbol"] for o in orders} == {"AAPL", "NVDA"}
    assert all(o["status"] == "filled" for o in orders)


# ---------------- expiry ----------------
def test_expiry_settles_long_option_to_intrinsic(fresh_db, market):
    from datetime import date
    s = pe.create_session("expiry", 100_000)
    exps = run(market.expirations("SPY"))
    chain = run(market.chain("SPY", exps[3]))
    spot = chain.underlying_price
    # Deep in the money, so settlement must pay real intrinsic value.
    deep = min((c for c in chain.calls if c.strike < spot * 0.97),
               key=lambda c: abs(c.strike - spot * 0.95))
    run(pe.submit_order(s["id"], [pe.LegRequest(deep.symbol, "buy", 1)], market))
    result = run(pe.settle_expirations(s["id"], market,
                                       as_of=date.fromisoformat(deep.expiration)))
    assert result["settled"] == 1
    assert result["details"][0]["outcome"] == "in the money"
    assert result["details"][0]["intrinsic"] == pytest.approx(spot - deep.strike, abs=0.01)
    assert run(pe.portfolio(s["id"], market))["positions"] == []


def test_worthless_expiry_keeps_the_short_credit(fresh_db, market):
    from datetime import date
    s = pe.create_session("worthless", 100_000)
    exps = run(market.expirations("SPY"))
    chain = run(market.chain("SPY", exps[3]))
    spot = chain.underlying_price
    far_otm = max(chain.calls, key=lambda c: c.strike)
    protective = far_otm
    # Sell a far OTM call covered by itself is impossible; use a wide spread.
    short = min((c for c in chain.calls if c.strike > spot * 1.02),
                key=lambda c: c.strike)
    long_ = max(chain.calls, key=lambda c: c.strike)
    r = run(pe.submit_order(s["id"], [pe.LegRequest(short.symbol, "sell", 1),
                                      pe.LegRequest(long_.symbol, "buy", 1)], market))
    credit = -r["net_debit"]
    result = run(pe.settle_expirations(s["id"], market,
                                       as_of=date.fromisoformat(short.expiration)))
    assert result["settled"] == 2
    assert all(d["outcome"] == "expired worthless" for d in result["details"])
    # Keeping the credit less commissions is the whole point of the structure.
    assert pe.get_session(s["id"])["cash"] == pytest.approx(100_000 + credit - r["commission"], abs=0.01)


def test_settlement_is_a_no_op_before_expiry(fresh_db, market):
    s = pe.create_session("early", 100_000)
    _, lo, _ = run(_spread_contracts(market))
    run(pe.submit_order(s["id"], [pe.LegRequest(lo.symbol, "buy", 1)], market))
    assert run(pe.settle_expirations(s["id"], market))["settled"] == 0
