"""Black-Scholes correctness: known values, parity, and greek behaviour."""
import math

import pytest

from backend.analytics import blackscholes as bs


def test_price_matches_known_value():
    # Textbook case: S=100, K=100, T=1, r=5%, vol=20% -> call 10.4506
    assert bs.price(100, 100, 1.0, 0.05, 0.20, "call") == pytest.approx(10.4506, abs=1e-3)
    assert bs.price(100, 100, 1.0, 0.05, 0.20, "put") == pytest.approx(5.5735, abs=1e-3)


def test_put_call_parity():
    s, k, t, r, vol = 765.0, 770.0, 30 / 365, 0.04, 0.18
    call = bs.price(s, k, t, r, vol, "call")
    put = bs.price(s, k, t, r, vol, "put")
    assert call - put == pytest.approx(s - k * math.exp(-r * t), abs=1e-6)


def test_expired_option_is_worth_intrinsic():
    assert bs.price(110, 100, 0.0, 0.04, 0.2, "call") == pytest.approx(10.0)
    assert bs.price(90, 100, 0.0, 0.04, 0.2, "call") == pytest.approx(0.0)
    assert bs.price(90, 100, 0.0, 0.04, 0.2, "put") == pytest.approx(10.0)


def test_implied_vol_round_trips():
    for vol in (0.08, 0.25, 0.6, 1.2):
        for kind in ("call", "put"):
            price = bs.price(500, 505, 5 / 365, 0.04, vol, kind)
            assert bs.implied_vol(price, 500, 505, 5 / 365, 0.04, kind) == pytest.approx(vol, abs=1e-4)


def test_implied_vol_rejects_arbitrage_violating_prices():
    # Below intrinsic and above the underlying are both unsolvable.
    assert bs.implied_vol(0.5, 100, 90, 0.25, 0.0, "call") is None
    assert bs.implied_vol(150, 100, 90, 0.25, 0.04, "call") is None


def test_greeks_match_live_broker_quote():
    """Reproduces a real SPY 766 call quote captured from a live chain."""
    g = bs.greeks(765.89, 766.0, 2 / 365, 0.04, 0.109598, "call")
    assert g.delta == pytest.approx(0.513, abs=0.02)
    assert g.gamma == pytest.approx(0.0605, abs=0.006)
    assert g.vega == pytest.approx(0.2397, abs=0.02)
    assert g.theta < 0                      # long options decay


def test_call_and_put_delta_bounds():
    for k in (80, 100, 130):
        c = bs.greeks(100, k, 0.5, 0.04, 0.3, "call").delta
        p = bs.greeks(100, k, 0.5, 0.04, 0.3, "put").delta
        assert 0 <= c <= 1
        assert -1 <= p <= 0
        # delta_call - delta_put = e^{-qT}, which is 1 with no dividend.
        assert c - p == pytest.approx(1.0, abs=1e-6)


def test_short_option_theta_is_positive_for_the_seller():
    long_theta = bs.greeks(100, 100, 3 / 365, 0.04, 0.3, "call").theta
    assert long_theta < 0
    assert -long_theta > 0


def test_prob_itm_is_a_probability():
    for k in (50, 100, 200):
        p = bs.prob_itm(100, k, 0.25, 0.04, 0.3, "call")
        assert 0.0 <= p <= 1.0
    # Deep ITM call is nearly certain, deep OTM nearly impossible.
    assert bs.prob_itm(100, 10, 0.02, 0.04, 0.2, "call") > 0.99
    assert bs.prob_itm(100, 500, 0.02, 0.04, 0.2, "call") < 0.01


def test_prob_touch_exceeds_prob_finish():
    """Touching a barrier is always at least as likely as finishing beyond it."""
    s, k, t, vol = 100.0, 110.0, 0.25, 0.35
    touch = bs.prob_touch(s, k, t, vol)
    finish = bs.prob_itm(s, k, t, 0.0, vol, "call")
    assert touch >= finish
    assert 0.0 <= touch <= 1.0


def test_expected_move_scales_with_sqrt_time():
    one = bs.expected_move(100, 0.2, 1)
    four = bs.expected_move(100, 0.2, 4)
    assert four == pytest.approx(one * 2, rel=1e-9)
