"""Per-symbol signal extraction.

`build_signals` turns a price history plus a live quote into a flat bundle of
normalised measurements.  Nothing here decides anything - it only measures.
The judgement lives in conviction.py, which keeps the scoring model auditable
and easy to re-weight.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict

import numpy as np

from ..analytics import indicators as ind
from ..providers.base import Bars, OptionChain, Quote


def _clean(x) -> float | None:
    """numpy NaN/inf -> None so the value survives JSON serialisation."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


@dataclass
class Signals:
    symbol: str
    price: float
    change_pct: float | None = None

    # trend
    ema9: float | None = None
    ema21: float | None = None
    ema50: float | None = None
    trend_stack: float | None = None      # -1..1, EMA alignment
    slope: float | None = None            # normalised 10-bar regression slope
    slope_r2: float | None = None         # trend cleanliness 0..1
    adx: float | None = None

    # momentum
    rsi: float | None = None
    macd_hist: float | None = None
    roc1: float | None = None
    roc3: float | None = None
    roc10: float | None = None
    percent_b: float | None = None
    donchian_pos: float | None = None

    # volatility
    atr: float | None = None
    atr_pct: float | None = None
    realized_vol: float | None = None
    parkinson_vol: float | None = None
    bb_width: float | None = None
    bb_squeeze: float | None = None       # percentile of current band width

    # options-derived
    atm_iv: float | None = None
    iv_rank: float | None = None          # IV vs its own recent range, 0..100
    iv_rv_ratio: float | None = None      # IV / realized vol - the vol premium
    expected_move_pct: float | None = None
    put_call_oi_ratio: float | None = None
    put_call_vol_ratio: float | None = None

    # participation / liquidity
    relative_volume: float | None = None
    avg_dollar_volume: float | None = None
    gap_pct: float | None = None
    option_spread_pct: float | None = None
    option_oi: float | None = None

    # context
    beta_spy: float | None = None
    rel_strength_spy: float | None = None  # 10-day excess return vs SPY
    bars_available: int = 0

    # True when the price history and the live quote agree. They can come from
    # different providers, and a series that is split-unadjusted or stale
    # against a current quote produces meaningless gaps and momentum.
    data_consistent: bool = True
    consistency_note: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def build_signals(
    symbol: str,
    bars: Bars,
    quote: Quote | None,
    chain: OptionChain | None = None,
    spy_closes: list[float] | None = None,
    iv_history: list[float] | None = None,
) -> Signals:
    closes = bars.closes
    highs, lows, volumes = bars.highs, bars.lows, bars.volumes
    price = quote.last if quote else (closes[-1] if closes else 0.0)

    s = Signals(symbol=symbol.upper(), price=price, bars_available=len(closes))
    if len(closes) < 25:
        # Not enough history to say anything responsible about this name.
        return s

    s.change_pct = _clean(quote.change_pct) if quote else None

    # Cross-source sanity check before anything is derived from both.
    if quote and closes:
        reference = quote.previous_close or quote.last
        if reference and closes[-1] > 0:
            drift = abs(closes[-1] / reference - 1.0)
            if drift > 0.15:
                s.data_consistent = False
                s.consistency_note = (
                    f"Price history ends at {closes[-1]:.2f} but the quote implies "
                    f"{reference:.2f} ({drift * 100:.0f}% apart). The two came from "
                    f"different sources and disagree; signals derived from both are "
                    f"unreliable."
                )

    # -- trend --------------------------------------------------------------
    ema9, ema21, ema50 = ind.ema(closes, 9), ind.ema(closes, 21), ind.ema(closes, 50)
    s.ema9, s.ema21, s.ema50 = _clean(ema9[-1]), _clean(ema21[-1]), _clean(ema50[-1])
    if s.ema9 and s.ema21:
        stack = 0.0
        stack += 0.5 if price > s.ema9 else -0.5
        stack += 0.5 if s.ema9 > s.ema21 else -0.5
        if s.ema50:
            stack += 0.5 if s.ema21 > s.ema50 else -0.5
            stack /= 1.5
        s.trend_stack = round(stack, 3)

    s.slope, s.slope_r2 = (_clean(v) for v in ind.slope_r2(closes, 10))
    s.adx = _clean(ind.adx(highs, lows, closes)[-1])

    # -- momentum -----------------------------------------------------------
    s.rsi = _clean(ind.rsi(closes)[-1])
    _, _, hist = ind.macd(closes)
    # Scale MACD by price so it is comparable across a $15 ETF and a $700 name.
    s.macd_hist = _clean(hist[-1] / price * 100) if price else None
    s.roc1, s.roc3, s.roc10 = (_clean(ind.roc(closes, n)) for n in (1, 3, 10))

    upper, mid, lower, pb, bw = ind.bollinger(closes)
    s.percent_b = _clean(pb[-1])
    s.bb_width = _clean(bw[-1])
    bw_hist = [x for x in bw[-120:] if math.isfinite(x)]
    if bw_hist and s.bb_width is not None:
        s.bb_squeeze = _clean(ind.percentile_rank(s.bb_width, bw_hist))
    s.donchian_pos = _clean(ind.donchian(highs, lows, 20)[2])

    # -- volatility ---------------------------------------------------------
    atr_series = ind.atr(highs, lows, closes)
    s.atr = _clean(atr_series[-1])
    s.atr_pct = _clean(s.atr / price * 100) if s.atr and price else None
    s.realized_vol = _clean(ind.realized_vol(closes, 20))
    s.parkinson_vol = _clean(ind.parkinson_vol(highs, lows, 20))

    # -- participation ------------------------------------------------------
    s.relative_volume = _clean(ind.relative_volume(volumes, 20))
    if len(volumes) >= 20:
        s.avg_dollar_volume = _clean(float(np.mean(volumes[-20:])) * price)
    # Only meaningful when the quote and the bar series are the same scale.
    if len(closes) >= 2 and quote and quote.open and s.data_consistent:
        s.gap_pct = _clean((quote.open / closes[-2] - 1.0) * 100)

    # -- relative strength vs SPY ------------------------------------------
    if spy_closes and len(spy_closes) >= 11 and len(closes) >= 11:
        spy_roc = ind.roc(spy_closes, 10)
        own_roc = s.roc10
        if own_roc is not None and math.isfinite(spy_roc):
            s.rel_strength_spy = _clean((own_roc - spy_roc) * 100)
        n = min(len(closes), len(spy_closes), 60)
        if n >= 30:
            a = np.diff(np.log(np.asarray(closes[-n:], dtype=float)))
            b = np.diff(np.log(np.asarray(spy_closes[-n:], dtype=float)))
            var = float(np.var(b, ddof=1))
            if var > 0:
                s.beta_spy = _clean(float(np.cov(a, b, ddof=1)[0, 1]) / var)

    # -- options ------------------------------------------------------------
    if chain and chain.calls:
        _apply_chain_signals(s, chain, price, iv_history)

    return s


def _apply_chain_signals(s: Signals, chain: OptionChain, price: float,
                         iv_history: list[float] | None) -> None:
    """Fold ATM implied vol, skew and open-interest flow into the bundle."""
    def nearest(contracts):
        pool = [c for c in contracts if c.implied_volatility and c.implied_volatility > 0]
        return min(pool, key=lambda c: abs(c.strike - price)) if pool else None

    atm_call, atm_put = nearest(chain.calls), nearest(chain.puts)
    ivs = [c.implied_volatility for c in (atm_call, atm_put) if c]
    if ivs:
        s.atm_iv = _clean(sum(ivs) / len(ivs))

    if s.atm_iv and s.realized_vol and s.realized_vol > 0:
        s.iv_rv_ratio = _clean(s.atm_iv / s.realized_vol)
    if s.atm_iv and iv_history:
        s.iv_rank = _clean(ind.percentile_rank(s.atm_iv, iv_history))

    # Expected move to expiry implied by ATM vol.
    from datetime import date
    try:
        dte = max((date.fromisoformat(chain.expiration) - date.today()).days, 0)
    except ValueError:
        dte = 1
    if s.atm_iv and price:
        from ..analytics.blackscholes import expected_move
        em = expected_move(price, s.atm_iv, max(dte, 1))
        s.expected_move_pct = _clean(em / price * 100)

    # Liquidity of the strikes actually worth trading (within ~8% of spot).
    near = [c for c in chain.all() if abs(c.strike / price - 1.0) < 0.08] if price else []
    spreads = [c.spread_pct for c in near if c.spread_pct is not None and c.mid and c.mid > 0.05]
    if spreads:
        s.option_spread_pct = _clean(float(np.median(spreads)))
    ois = [c.open_interest or 0 for c in near]
    if ois:
        s.option_oi = _clean(float(np.sum(ois)))

    call_oi = sum(c.open_interest or 0 for c in chain.calls)
    put_oi = sum(c.open_interest or 0 for c in chain.puts)
    if call_oi > 0:
        s.put_call_oi_ratio = _clean(put_oi / call_oi)
    call_vol = sum(c.volume or 0 for c in chain.calls)
    put_vol = sum(c.volume or 0 for c in chain.puts)
    if call_vol > 0:
        s.put_call_vol_ratio = _clean(put_vol / call_vol)
