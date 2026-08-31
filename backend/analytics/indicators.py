"""Technical indicators over numpy arrays of daily or intraday bars.

Every function takes plain sequences and returns either a full series (same
length as the input, front-padded with NaN) or a single latest value.  Nothing
here mutates its inputs.
"""
from __future__ import annotations

import numpy as np


def _arr(x) -> np.ndarray:
    return np.asarray(x, dtype=float)


def sma(values, period: int) -> np.ndarray:
    v = _arr(values)
    out = np.full(v.shape, np.nan)
    if len(v) < period or period <= 0:
        return out
    csum = np.cumsum(np.insert(v, 0, 0.0))
    out[period - 1:] = (csum[period:] - csum[:-period]) / period
    return out


def ema(values, period: int) -> np.ndarray:
    v = _arr(values)
    out = np.full(v.shape, np.nan)
    if len(v) < period or period <= 0:
        return out
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = v[:period].mean()
    for i in range(period, len(v)):
        out[i] = alpha * v[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(values, period: int = 14) -> np.ndarray:
    """Wilder's RSI."""
    v = _arr(values)
    out = np.full(v.shape, np.nan)
    if len(v) <= period:
        return out
    delta = np.diff(v)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    avg_gain = gain[:period].mean()
    avg_loss = loss[:period].mean()
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        out[i + 1] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def true_range(high, low, close) -> np.ndarray:
    h, l, c = _arr(high), _arr(low), _arr(close)
    prev_close = np.roll(c, 1)
    prev_close[0] = c[0]
    return np.maximum(h - l, np.maximum(np.abs(h - prev_close), np.abs(l - prev_close)))


def atr(high, low, close, period: int = 14) -> np.ndarray:
    """Wilder-smoothed Average True Range."""
    tr = true_range(high, low, close)
    out = np.full(tr.shape, np.nan)
    if len(tr) < period:
        return out
    out[period - 1] = tr[:period].mean()
    for i in range(period, len(tr)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def macd(values, fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd_line, signal_line, histogram)."""
    v = _arr(values)
    line = ema(v, fast) - ema(v, slow)
    valid = ~np.isnan(line)
    sig = np.full(v.shape, np.nan)
    if valid.sum() >= signal:
        sig[valid] = ema(line[valid], signal)
    return line, sig, line - sig


def bollinger(values, period: int = 20, num_std: float = 2.0):
    """Returns (upper, middle, lower, percent_b, bandwidth)."""
    v = _arr(values)
    mid = sma(v, period)
    std = np.full(v.shape, np.nan)
    for i in range(period - 1, len(v)):
        std[i] = v[i - period + 1:i + 1].std(ddof=0)
    upper, lower = mid + num_std * std, mid - num_std * std
    width = np.where(upper - lower == 0, np.nan, upper - lower)
    return upper, mid, lower, (v - lower) / width, (upper - lower) / mid


def adx(high, low, close, period: int = 14) -> np.ndarray:
    """Average Directional Index - trend *strength* regardless of direction."""
    h, l, c = _arr(high), _arr(low), _arr(close)
    n = len(c)
    out = np.full(n, np.nan)
    if n < 2 * period + 1:
        return out

    up_move, down_move = h[1:] - h[:-1], l[:-1] - l[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = true_range(h, l, c)[1:]

    def wilder(x):
        sm = np.full(len(x), np.nan)
        sm[period - 1] = x[:period].sum()
        for i in range(period, len(x)):
            sm[i] = sm[i - 1] - sm[i - 1] / period + x[i]
        return sm

    tr_s, pdm_s, mdm_s = wilder(tr), wilder(plus_dm), wilder(minus_dm)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100.0 * pdm_s / tr_s
        mdi = 100.0 * mdm_s / tr_s
        dx = 100.0 * np.abs(pdi - mdi) / (pdi + mdi)

    dx_valid = np.where(np.isfinite(dx), dx, np.nan)
    first = period - 1 + period
    if first < len(dx_valid) and np.isfinite(dx_valid[period - 1:first]).all():
        acc = np.nanmean(dx_valid[period - 1:first])
        out[first] = acc
        for i in range(first, len(dx_valid)):
            if np.isfinite(dx_valid[i]):
                acc = (acc * (period - 1) + dx_valid[i]) / period
            out[i + 1] = acc
    return out


def realized_vol(closes, period: int = 20, annualize: int = 252) -> float:
    """Close-to-close annualised volatility over the last `period` returns."""
    c = _arr(closes)
    if len(c) < period + 1:
        return float("nan")
    rets = np.diff(np.log(c[-(period + 1):]))
    return float(rets.std(ddof=1) * np.sqrt(annualize))


def parkinson_vol(high, low, period: int = 20, annualize: int = 252) -> float:
    """Parkinson high-low range volatility - less noisy than close-to-close."""
    h, l = _arr(high)[-period:], _arr(low)[-period:]
    if len(h) < period or np.any(h <= 0) or np.any(l <= 0):
        return float("nan")
    hl = np.log(h / l) ** 2
    return float(np.sqrt(hl.mean() / (4.0 * np.log(2.0))) * np.sqrt(annualize))


def relative_volume(volumes, lookback: int = 20) -> float:
    """Latest volume divided by its trailing average (1.0 == typical day)."""
    v = _arr(volumes)
    if len(v) < lookback + 1:
        return float("nan")
    base = v[-(lookback + 1):-1].mean()
    return float(v[-1] / base) if base > 0 else float("nan")


def roc(values, period: int) -> float:
    """Rate of change over `period` bars, as a fraction."""
    v = _arr(values)
    if len(v) < period + 1 or v[-(period + 1)] == 0:
        return float("nan")
    return float(v[-1] / v[-(period + 1)] - 1.0)


def slope_r2(values, period: int = 10):
    """OLS slope (normalised by price) and R^2 of the last `period` closes.

    R^2 separates a clean, tradeable trend from a choppy drift with the same
    net displacement - which is the difference between a directional debit
    spread working and getting chopped up.
    """
    v = _arr(values)[-period:]
    if len(v) < period or np.mean(v) == 0:
        return float("nan"), float("nan")
    x = np.arange(period, dtype=float)
    slope, intercept = np.polyfit(x, v, 1)
    pred = slope * x + intercept
    ss_res = float(((v - pred) ** 2).sum())
    ss_tot = float(((v - v.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return float(slope / np.mean(v)), r2


def donchian(high, low, period: int = 20):
    """Returns (upper, lower, position_in_channel 0..1) for the latest bar."""
    h, l = _arr(high)[-period:], _arr(low)[-period:]
    if len(h) < period:
        return float("nan"), float("nan"), float("nan")
    hi, lo = float(h.max()), float(l.min())
    last = float(_arr(low)[-1] + (_arr(high)[-1] - _arr(low)[-1]) / 2)
    pos = (last - lo) / (hi - lo) if hi > lo else float("nan")
    return hi, lo, pos


def percentile_rank(value: float, history) -> float:
    """Where `value` sits within `history`, as 0..100."""
    h = _arr([x for x in history if np.isfinite(x)])
    if len(h) == 0 or not np.isfinite(value):
        return float("nan")
    return float((h < value).sum() / len(h) * 100.0)


def max_drawdown(equity) -> float:
    """Largest peak-to-trough decline of an equity curve, as a fraction."""
    e = _arr(equity)
    if len(e) == 0:
        return 0.0
    peak = np.maximum.accumulate(e)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(peak > 0, (e - peak) / peak, 0.0)
    return float(np.nanmin(dd)) if len(dd) else 0.0


def sharpe(returns, periods_per_year: int = 252, risk_free: float = 0.0) -> float:
    r = _arr(returns)
    if len(r) < 2:
        return float("nan")
    excess = r - risk_free / periods_per_year
    sd = float(excess.std(ddof=1))
    # Guard against a standard deviation that is only non-zero through floating
    # point noise: dividing by 1e-18 yields a Sharpe of 1e16, which would be
    # reported as a real statistic.
    scale = max(abs(float(excess.mean())), 1.0)
    if sd <= scale * 1e-12:
        return float("nan")
    return float(excess.mean() / sd * np.sqrt(periods_per_year))
