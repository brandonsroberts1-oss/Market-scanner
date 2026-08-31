"""Indicator correctness against hand-checkable inputs."""
import math

import numpy as np
import pytest

from backend.analytics import indicators as ind

# A real SPY daily close series (June-August 2026), used so the tests exercise
# the same shape of data the app sees in production.
SPY_CLOSES = [758.54, 759.57, 754.24, 757.09, 737.55, 739.22, 737.05, 725.43, 737.76,
              741.75, 754.83, 750.33, 740.96, 746.74, 744.39, 733.58, 733.24, 734.30,
              728.99, 741.00, 746.77, 745.76, 744.78, 751.28, 747.71, 745.40, 751.71,
              754.95, 749.17, 751.83, 754.81, 750.72, 743.29, 742.09, 748.28, 747.41,
              738.18, 738.93, 739.09, 740.86, 729.46, 741.69, 747.03, 757.67, 771.33,
              769.79, 768.56, 773.26, 773.03, 770.56, 772.49, 777.88, 776.34, 772.67,
              767.45, 769.06, 762.60, 765.72, 763.47, 765.91, 766.08, 771.10, 769.35]


def test_sma_matches_manual_mean():
    values = [1, 2, 3, 4, 5, 6]
    out = ind.sma(values, 3)
    assert math.isnan(out[0]) and math.isnan(out[1])
    assert out[2] == pytest.approx(2.0)
    assert out[-1] == pytest.approx(5.0)


def test_ema_converges_and_tracks_last_value():
    flat = [10.0] * 50
    assert ind.ema(flat, 9)[-1] == pytest.approx(10.0)


def test_rsi_bounds_and_extremes():
    rsi = ind.rsi(SPY_CLOSES)
    finite = [v for v in rsi if math.isfinite(v)]
    assert finite, "RSI produced no values"
    assert all(0 <= v <= 100 for v in finite)
    # A monotonically rising series pins RSI at 100.
    assert ind.rsi(list(range(1, 40)))[-1] == pytest.approx(100.0)


def test_atr_is_positive_and_reflects_range():
    highs = [c + 5 for c in SPY_CLOSES]
    lows = [c - 5 for c in SPY_CLOSES]
    atr = ind.atr(highs, lows, SPY_CLOSES)[-1]
    assert atr >= 10.0        # at least the constant 10-wide daily range


def test_macd_histogram_is_line_minus_signal():
    line, signal, hist = ind.macd(SPY_CLOSES)
    idx = -1
    assert hist[idx] == pytest.approx(line[idx] - signal[idx], abs=1e-9)


def test_bollinger_percent_b_locates_price_in_band():
    upper, mid, lower, pb, width = ind.bollinger(SPY_CLOSES)
    assert lower[-1] < mid[-1] < upper[-1]
    assert 0.0 <= pb[-1] <= 1.0


def test_adx_is_bounded():
    highs = [c + 3 for c in SPY_CLOSES]
    lows = [c - 3 for c in SPY_CLOSES]
    adx = ind.adx(highs, lows, SPY_CLOSES)
    finite = [v for v in adx if math.isfinite(v)]
    assert finite
    assert all(0 <= v <= 100 for v in finite)


def test_realized_vol_matches_manual_calculation():
    rets = np.diff(np.log(np.asarray(SPY_CLOSES[-21:])))
    expected = rets.std(ddof=1) * math.sqrt(252)
    assert ind.realized_vol(SPY_CLOSES, 20) == pytest.approx(expected, rel=1e-9)


def test_realized_vol_is_plausible_for_spy():
    """SPY's realised vol should land near the ~11% ATM IV its chain quoted."""
    assert 0.05 < ind.realized_vol(SPY_CLOSES, 20) < 0.25


def test_relative_volume_detects_a_surge():
    volumes = [1_000_000] * 20 + [3_000_000]
    assert ind.relative_volume(volumes, 20) == pytest.approx(3.0)


def test_slope_r2_separates_clean_trend_from_chop():
    clean = list(range(100, 120))
    choppy = [110 + (5 if i % 2 else -5) for i in range(20)]
    _, r2_clean = ind.slope_r2(clean, 10)
    _, r2_chop = ind.slope_r2(choppy, 10)
    assert r2_clean > 0.95
    assert r2_chop < 0.5


def test_percentile_rank_ignores_non_finite_history():
    assert ind.percentile_rank(5, [1, 2, 3, 4, float("nan")]) == pytest.approx(100.0)
    assert ind.percentile_rank(0, [1, 2, 3, 4]) == pytest.approx(0.0)


def test_max_drawdown_is_negative_and_correct():
    assert ind.max_drawdown([100, 120, 60, 80]) == pytest.approx(-0.5)
    assert ind.max_drawdown([100, 110, 120]) == pytest.approx(0.0)


def test_sharpe_of_constant_returns_is_undefined():
    assert math.isnan(ind.sharpe([0.01] * 10))


def test_indicators_tolerate_short_series():
    """Every indicator must return NaN padding rather than raising on thin data."""
    short = [100.0, 101.0, 99.0]
    assert all(math.isnan(v) for v in ind.rsi(short))
    assert all(math.isnan(v) for v in ind.sma(short, 20))
    assert math.isnan(ind.realized_vol(short, 20))
    assert math.isnan(ind.relative_volume([1, 2], 20))
