"""Black-Scholes-Merton pricing, greeks, implied volatility and probabilities.

Everything here works on plain floats and numpy arrays so it can be used both
for single-contract pricing in the scanner and for vectorised repricing in the
backtester.  Rates are continuously compounded, times are in years.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SQRT_2PI = math.sqrt(2.0 * math.pi)

# A day of *calendar* time as a fraction of a year.  Options decay on calendar
# days, not trading days, which matters a lot for the 0-3 DTE trades this app
# is built around.
DAY = 1.0 / 365.0


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function (full double precision)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(s: float, k: float, t: float, r: float, q: float, vol: float):
    if t <= 0 or vol <= 0 or s <= 0 or k <= 0:
        return None, None
    vsqrt = vol * math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * vol * vol) * t) / vsqrt
    return d1, d1 - vsqrt


def price(s: float, k: float, t: float, r: float, vol: float, kind: str, q: float = 0.0) -> float:
    """Black-Scholes price of a European option.

    Falls back to intrinsic value at/after expiry or with zero vol, which keeps
    the backtester well behaved when it prices a position on its expiry bar.
    """
    call = kind.lower().startswith("c")
    d1, d2 = _d1_d2(s, k, t, r, q, vol)
    if d1 is None:
        intrinsic = (s - k) if call else (k - s)
        return max(intrinsic, 0.0)
    if call:
        return s * math.exp(-q * t) * norm_cdf(d1) - k * math.exp(-r * t) * norm_cdf(d2)
    return k * math.exp(-r * t) * norm_cdf(-d2) - s * math.exp(-q * t) * norm_cdf(-d1)


@dataclass
class Greeks:
    delta: float
    gamma: float
    theta: float   # per calendar day
    vega: float    # per 1 vol point (0.01)
    rho: float     # per 1% rate move


def greeks(s: float, k: float, t: float, r: float, vol: float, kind: str, q: float = 0.0) -> Greeks:
    call = kind.lower().startswith("c")
    d1, d2 = _d1_d2(s, k, t, r, q, vol)
    if d1 is None:
        # Expired / degenerate: delta is a step function, everything else zero.
        if call:
            delta = 1.0 if s > k else 0.0
        else:
            delta = -1.0 if s < k else 0.0
        return Greeks(delta, 0.0, 0.0, 0.0, 0.0)

    disc_q = math.exp(-q * t)
    disc_r = math.exp(-r * t)
    sqrt_t = math.sqrt(t)
    pdf_d1 = norm_pdf(d1)

    delta = disc_q * (norm_cdf(d1) if call else norm_cdf(d1) - 1.0)
    gamma = disc_q * pdf_d1 / (s * vol * sqrt_t)
    vega = s * disc_q * pdf_d1 * sqrt_t * 0.01

    term1 = -(s * disc_q * pdf_d1 * vol) / (2.0 * sqrt_t)
    if call:
        theta = term1 - r * k * disc_r * norm_cdf(d2) + q * s * disc_q * norm_cdf(d1)
        rho = k * t * disc_r * norm_cdf(d2) * 0.01
    else:
        theta = term1 + r * k * disc_r * norm_cdf(-d2) - q * s * disc_q * norm_cdf(-d1)
        rho = -k * t * disc_r * norm_cdf(-d2) * 0.01

    return Greeks(delta, gamma, theta * DAY, vega, rho)


def implied_vol(
    target: float,
    s: float,
    k: float,
    t: float,
    r: float,
    kind: str,
    q: float = 0.0,
    lo: float = 1e-4,
    hi: float = 5.0,
) -> float | None:
    """Recover implied volatility from an option price by bisection.

    Bisection rather than Newton: vega collapses on deep ITM/OTM contracts and
    Newton diverges exactly where short-dated scanning spends most of its time.
    Returns None when the price is outside the no-arbitrage band.
    """
    if t <= 0 or s <= 0 or k <= 0 or target <= 0:
        return None

    call = kind.lower().startswith("c")
    intrinsic = max((s * math.exp(-q * t) - k * math.exp(-r * t)) if call
                    else (k * math.exp(-r * t) - s * math.exp(-q * t)), 0.0)
    upper_bound = s * math.exp(-q * t) if call else k * math.exp(-r * t)
    if target < intrinsic - 1e-9 or target > upper_bound + 1e-9:
        return None

    f_lo = price(s, k, t, r, lo, kind, q) - target
    f_hi = price(s, k, t, r, hi, kind, q) - target
    if f_lo > 0 or f_hi < 0:
        return None

    for _ in range(100):
        mid = 0.5 * (lo + hi)
        f_mid = price(s, k, t, r, mid, kind, q) - target
        if abs(f_mid) < 1e-8:
            return mid
        if f_mid < 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def prob_itm(s: float, k: float, t: float, r: float, vol: float, kind: str, q: float = 0.0) -> float:
    """Risk-neutral probability the option finishes in the money (N(d2))."""
    d1, d2 = _d1_d2(s, k, t, r, q, vol)
    if d2 is None:
        if kind.lower().startswith("c"):
            return 1.0 if s > k else 0.0
        return 1.0 if s < k else 0.0
    return norm_cdf(d2) if kind.lower().startswith("c") else norm_cdf(-d2)


def prob_touch(s: float, k: float, t: float, vol: float, r: float = 0.0, q: float = 0.0) -> float:
    """Probability the underlying touches barrier `k` at any point before T.

    Standard first-passage result for GBM with drift under the risk-neutral
    measure.  Useful for stop placement and for judging whether a short strike
    is likely to be tested even if it expires OTM.
    """
    if t <= 0 or vol <= 0 or s <= 0 or k <= 0:
        return 0.0
    if (k > s and s >= k) or (k < s and s <= k):
        return 1.0

    mu = r - q - 0.5 * vol * vol
    log_ratio = math.log(k / s)
    vsqrt = vol * math.sqrt(t)

    a = (-abs(log_ratio) + mu * t) / vsqrt if log_ratio > 0 else (-abs(log_ratio) - mu * t) / vsqrt
    b = (-abs(log_ratio) - mu * t) / vsqrt if log_ratio > 0 else (-abs(log_ratio) + mu * t) / vsqrt
    exponent = (2.0 * mu * abs(log_ratio)) / (vol * vol) * (1.0 if log_ratio > 0 else -1.0)
    # Clamp: the exponential blows up for tiny vol / far barriers.
    exponent = max(min(exponent, 50.0), -50.0)

    p = norm_cdf(a) + math.exp(exponent) * norm_cdf(b)
    return max(0.0, min(1.0, p))


def expected_move(s: float, vol: float, days: float) -> float:
    """One standard-deviation move over `days` calendar days, in dollars."""
    if s <= 0 or vol <= 0 or days <= 0:
        return 0.0
    return s * vol * math.sqrt(days * DAY)
