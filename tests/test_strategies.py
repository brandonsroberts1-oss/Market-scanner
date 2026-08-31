"""Structure construction, payoff arithmetic and expected-value calibration."""
import pytest

from backend.analytics import blackscholes as bs
from backend.engine import strategies as st
from backend.providers.base import OptionContract


def leg(action, kind, strike, price, qty=1):
    return st.Leg(action=action, kind=kind, strike=strike, expiration="2026-09-02",
                  symbol=f"X{strike}{kind[0]}", quantity=qty, price=price)


def test_bull_call_spread_arithmetic():
    """Buy 765C at 4.06, sell 775C at 1.20: debit 2.86, max profit 7.14."""
    legs = [leg("buy", "call", 765, 4.06), leg("sell", "call", 775, 1.20)]
    assert st._net_cost(legs) == pytest.approx(286.0)
    assert st.payoff_at(legs, 760) == pytest.approx(-286.0)     # both expire worthless
    assert st.payoff_at(legs, 775) == pytest.approx(714.0)      # max profit
    assert st.payoff_at(legs, 900) == pytest.approx(714.0)      # capped above the short
    assert st._breakevens(legs, 765) == [pytest.approx(767.86, abs=0.02)]


def test_credit_spread_arithmetic():
    """Sell 760P at 1.91, buy 755P at 0.80: credit 1.11, max loss 3.89."""
    legs = [leg("sell", "put", 760, 1.91), leg("buy", "put", 755, 0.80)]
    assert st._net_cost(legs) == pytest.approx(-111.0)
    assert st.payoff_at(legs, 800) == pytest.approx(111.0)      # both expire worthless
    assert st.payoff_at(legs, 750) == pytest.approx(-389.0)     # max loss
    assert st._breakevens(legs, 765) == [pytest.approx(758.89, abs=0.02)]


def test_iron_condor_has_two_breakevens_and_capped_loss():
    legs = [leg("sell", "put", 750, 1.50), leg("buy", "put", 745, 0.80),
            leg("sell", "call", 780, 1.40), leg("buy", "call", 785, 0.70)]
    breakevens = st._breakevens(legs, 765)
    assert len(breakevens) == 2
    assert breakevens[0] < 765 < breakevens[1]
    credit = -st._net_cost(legs)
    assert st.payoff_at(legs, 765) == pytest.approx(credit)     # max profit in the middle
    assert st.payoff_at(legs, 700) == pytest.approx(st.payoff_at(legs, 600))  # loss is capped


def test_long_option_payoff():
    legs = [leg("buy", "call", 100, 3.0)]
    assert st.payoff_at(legs, 95) == pytest.approx(-300.0)
    assert st.payoff_at(legs, 103) == pytest.approx(0.0)
    assert st.payoff_at(legs, 110) == pytest.approx(700.0)


def test_quantity_scales_payoff_linearly():
    one = [leg("buy", "call", 100, 3.0, qty=1)]
    five = [leg("buy", "call", 100, 3.0, qty=5)]
    assert st.payoff_at(five, 110) == pytest.approx(st.payoff_at(one, 110) * 5)


def test_fair_priced_structures_have_near_zero_risk_neutral_ev():
    """The core calibration check: EV under the market's own measure is ~0.

    If this drifts, every 'expected value' the scanner reports is biased.
    """
    s, t, r, vol = 765.0, 2 / 365, 0.04, 0.13
    for legs in (
        [leg("buy", "call", 765, bs.price(s, 765, t, r, vol, "call"))],
        [leg("buy", "put", 770, bs.price(s, 770, t, r, vol, "put"))],
        [leg("buy", "call", 765, bs.price(s, 765, t, r, vol, "call")),
         leg("sell", "call", 775, bs.price(s, 775, t, r, vol, "call"))],
        [leg("sell", "put", 760, bs.price(s, 760, t, r, vol, "put")),
         leg("buy", "put", 755, bs.price(s, 755, t, r, vol, "put"))],
    ):
        ev = st.structure_metrics(legs, s, vol, 2, bias=0.0, rate=r)["ev_risk_neutral"]
        assert abs(ev) < 1.0, f"risk-neutral EV drifted to {ev}"


def test_model_ev_responds_to_directional_bias():
    legs = [leg("buy", "call", 765, 4.06), leg("sell", "call", 775, 1.20)]
    bullish = st.structure_metrics(legs, 765, 0.13, 2, bias=0.6)
    flat = st.structure_metrics(legs, 765, 0.13, 2, bias=0.0)
    bearish = st.structure_metrics(legs, 765, 0.13, 2, bias=-0.6)
    assert bullish["ev_model"] > flat["ev_model"] > bearish["ev_model"]
    assert bullish["prob_profit_model"] > bearish["prob_profit_model"]
    # The risk-neutral leg must be unaffected by the model's opinion.
    assert bullish["ev_risk_neutral"] == pytest.approx(bearish["ev_risk_neutral"])


def test_probabilities_are_bounded():
    legs = [leg("sell", "put", 700, 0.40), leg("buy", "put", 690, 0.20)]
    m = st.structure_metrics(legs, 765, 0.13, 2)
    assert 0.0 <= m["prob_profit"] <= 1.0
    assert 0.0 <= m["prob_profit_model"] <= 1.0


def test_fill_price_crosses_the_spread_in_the_right_direction():
    c = OptionContract("X", "SPY", "2026-09-02", 765, "call", bid=4.00, ask=4.20)
    buy = st.fill_price(c, "buy", 0.5)
    sell = st.fill_price(c, "sell", 0.5)
    assert buy > c.mid > sell
    assert st.fill_price(c, "buy", 0.0) == pytest.approx(c.mid, abs=0.01)


def test_pick_ignores_contracts_without_a_market():
    contracts = [
        OptionContract("A", "SPY", "2026-09-02", 760, "call", bid=0, ask=0, delta=0.6),
        OptionContract("B", "SPY", "2026-09-02", 765, "call", bid=4.0, ask=4.2, delta=0.5),
    ]
    picked = st._pick(contracts, 0.6)
    assert picked is not None and picked.symbol == "B", "unpriced contract was selected"


def test_unbounded_target_structures_report_no_risk_reward(market):
    """A long put's max profit assumes the stock goes to zero; that must not
    become a headline risk/reward ratio."""
    import asyncio
    from backend.engine.conviction import assess
    from backend.engine.signals import build_signals

    async def build():
        bars = await market.history("SMH", 180)
        quote = (await market.quotes(["SMH"]))["SMH"]
        exps = await market.expirations("SMH")
        chain = await market.chain("SMH", exps[2])
        sig = build_signals("SMH", bars, quote, chain)
        return assess(sig), sig, chain

    assessment, sig, chain = asyncio.run(build())
    ideas = st.build_ideas(assessment, sig, chain, 2)
    for idea in ideas:
        if idea.strategy in st.UNBOUNDED_TARGET_STRATEGIES:
            assert idea.risk_reward is None
        assert idea.reward_at_expected_move is not None
