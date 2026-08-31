"""Ranking of outright equities and funds.

Separate from the options scanner because the question is different: options
ask "what moves in the next three days", equities ask "what is worth holding
while the trend persists".  The score therefore leans on trend persistence and
risk-adjusted return rather than on gamma and expiry mechanics.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

import numpy as np

from ..analytics import indicators as ind
from .signals import Signals

ETF_HINTS = {
    "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK", "XLV", "XLI", "XLU", "XLP",
    "XLY", "XLB", "XLRE", "XLC", "SMH", "SOXL", "ARKK", "GLD", "SLV", "TLT",
    "HYG", "USO", "UNG", "EEM", "FXI", "VOO", "VTI", "IVV",
}


@dataclass
class EquityIdea:
    symbol: str
    kind: str                    # "fund" | "stock"
    price: float
    change_pct: float | None
    score: int                   # 0..100
    direction: str               # long | short | avoid
    trend_grade: str             # A..D
    horizon: str
    entry: float
    stop: float
    target: float
    risk_reward: float
    atr_pct: float | None
    rsi: float | None
    rel_strength: float | None
    sharpe_60d: float | None
    max_drawdown_60d: float | None
    avg_dollar_volume: float | None
    rationale: str = ""
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _grade(value: float) -> str:
    if value >= 0.75:
        return "A"
    if value >= 0.55:
        return "B"
    if value >= 0.35:
        return "C"
    return "D"


def rank_equity(sig: Signals, closes: list[float]) -> EquityIdea | None:
    """Score one symbol as an outright long or short swing candidate."""
    if sig.bars_available < 40 or not sig.price:
        return None

    reasons: list[str] = []

    # Trend persistence: EMA alignment, ADX and regression fit.
    trend = (sig.trend_stack or 0.0)
    adx = sig.adx or 0.0
    r2 = sig.slope_r2 or 0.0
    persistence = max(0.0, min(1.0, (min(adx, 40) / 40) * 0.5 + r2 * 0.5))

    # Risk-adjusted return over the last 60 sessions.
    rets = np.diff(np.log(np.asarray(closes[-61:], dtype=float))) if len(closes) >= 61 else np.array([])
    sharpe_60 = ind.sharpe(rets) if len(rets) > 5 else float("nan")
    dd_60 = ind.max_drawdown(closes[-61:]) if len(closes) >= 61 else 0.0

    direction = "long" if trend > 0.1 else "short" if trend < -0.1 else "avoid"

    # Base score: trend quality, momentum and relative strength, all signed to
    # the intended direction so a short candidate is scored on its downtrend.
    sign = 1.0 if direction == "long" else -1.0 if direction == "short" else 0.0
    momentum = ((sig.rsi or 50) - 50) / 50 * sign
    rel = max(-1.0, min(1.0, (sig.rel_strength_spy or 0.0) / 5.0)) * sign
    sharpe_term = 0.0
    if math.isfinite(sharpe_60):
        sharpe_term = max(-1.0, min(1.0, sharpe_60 / 2.0)) * sign

    raw = (persistence * 0.34 + max(0.0, momentum) * 0.20
           + max(-0.5, rel) * 0.22 + max(-0.5, sharpe_term) * 0.24)
    raw = max(0.0, min(1.0, raw * abs(trend if trend else 0.4) * 1.15))

    # Liquidity gate - a great chart on a thin name is not a position.
    adv = sig.avg_dollar_volume or 0.0
    if adv and adv < 10_000_000:
        raw *= 0.55
        reasons.append("Thin dollar volume; slippage will matter on size.")
    elif adv and adv < 50_000_000:
        raw *= 0.85

    if abs(dd_60) > 0.30:
        raw *= 0.8
        reasons.append(f"Drew down {abs(dd_60) * 100:.0f}% in the last 60 sessions.")

    score = int(round(max(0.0, min(1.0, raw)) * 100))

    # Levels: ATR-based, which adapts the stop to the name's own volatility
    # instead of applying a fixed percentage to everything.
    atr = sig.atr or (sig.price * 0.02)
    entry = sig.price
    if direction == "short":
        stop, target = entry + 1.5 * atr, entry - 3.0 * atr
    else:
        stop, target = entry - 1.5 * atr, entry + 3.0 * atr
    rr = abs(target - entry) / max(abs(entry - stop), 1e-9)

    if adx >= 25:
        reasons.append(f"ADX {adx:.0f} confirms a directional regime, not chop.")
    if r2 >= 0.5:
        reasons.append(f"Trend fit R^2 {r2:.2f} - the advance is orderly.")
    if sig.rel_strength_spy is not None and abs(sig.rel_strength_spy) > 1.5:
        reasons.append(f"{sig.rel_strength_spy:+.1f}pp 10-day relative strength vs SPY.")
    if math.isfinite(sharpe_60) and abs(sharpe_60) > 1.0:
        reasons.append(f"60-day Sharpe {sharpe_60:.2f}.")
    if sig.rsi is not None and sig.rsi > 75:
        reasons.append("RSI above 75 - extended; prefer a pullback entry.")
    if sig.rsi is not None and sig.rsi < 25:
        reasons.append("RSI below 25 - washed out; a bounce is as likely as continuation.")

    kind = "fund" if sig.symbol in ETF_HINTS else "stock"
    horizon = "2-10 sessions" if adx >= 25 else "1-5 sessions"

    rationale = (
        f"{'Uptrend' if direction == 'long' else 'Downtrend' if direction == 'short' else 'No clear trend'}"
        f" with grade {_grade(persistence)} persistence. "
        f"ATR is {sig.atr_pct:.1f}% of price, so the {abs(entry - stop) / entry * 100:.1f}% stop is "
        f"1.5 ATR - wide enough to survive normal noise."
        if sig.atr_pct else "Trend read from EMA alignment and regression fit."
    )

    return EquityIdea(
        symbol=sig.symbol, kind=kind, price=round(sig.price, 2),
        change_pct=round(sig.change_pct, 2) if sig.change_pct is not None else None,
        score=score, direction=direction, trend_grade=_grade(persistence), horizon=horizon,
        entry=round(entry, 2), stop=round(stop, 2), target=round(target, 2),
        risk_reward=round(rr, 2), atr_pct=round(sig.atr_pct, 2) if sig.atr_pct else None,
        rsi=round(sig.rsi, 1) if sig.rsi is not None else None,
        rel_strength=round(sig.rel_strength_spy, 2) if sig.rel_strength_spy is not None else None,
        sharpe_60d=round(sharpe_60, 2) if math.isfinite(sharpe_60) else None,
        max_drawdown_60d=round(dd_60 * 100, 2),
        avg_dollar_volume=round(adv) if adv else None,
        rationale=rationale, reasons=reasons,
    )
