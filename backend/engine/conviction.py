"""The conviction model.

Every factor produces a score in [-1, +1] (negative = bearish), carries a
weight, and carries a sentence explaining what it saw.  The weighted sum is the
directional bias; conviction is a separate quantity built from the *agreement*
between factors, the cleanliness of the trend, and a liquidity gate.

Keeping bias and conviction separate matters: a mildly bullish read that every
factor agrees on is a better trade than a strongly bullish read that half the
factors contradict, and a perfect signal on an untradeable option chain is not
a trade at all.

Nothing here is a prediction of profit.  It is a ranking of setups by how well
they match historically-studied conditions, and it is wrong regularly.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .signals import Signals


@dataclass
class Factor:
    name: str
    score: float          # -1..1 directional, or 0..1 for quality factors
    weight: float
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Assessment:
    symbol: str
    bias: float                 # -1..1
    direction: str              # bullish | bearish | neutral
    conviction: int             # 0..100
    agreement: float            # 0..1
    quality: float              # 0..1 liquidity/tradeability gate
    regime: str                 # trending | mean_revert | range | vol_expansion
    iv_regime: str              # cheap | fair | rich | unknown
    factors: list[Factor] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["factors"] = [f.to_dict() for f in self.factors]
        return d


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _scale(value: float | None, center: float, span: float) -> float:
    """Map a raw value to -1..1, centred on `center`, saturating at +/- span."""
    if value is None:
        return 0.0
    return _clamp((value - center) / span)


# Factor weights. They are deliberately visible in one place so the model can
# be re-tuned without hunting through the scoring code.
WEIGHTS = {
    "trend": 1.30,
    "momentum": 1.10,
    "macd": 0.75,
    "short_roc": 0.95,
    "mean_reversion": 0.85,
    "rel_strength": 0.70,
    "volume": 0.55,
    "gap": 0.50,
    "positioning": 0.35,
}


def assess(sig: Signals) -> Assessment:
    """Score one symbol into a directional bias plus a conviction level."""
    factors: list[Factor] = []
    warnings: list[str] = []

    if sig.bars_available < 25:
        return Assessment(sig.symbol, 0.0, "neutral", 0, 0.0, 0.0, "unknown", "unknown",
                          [], ["Insufficient price history to score this symbol."])

    if not sig.data_consistent:
        # Scoring contradictory inputs produces confident nonsense. Better to
        # decline and say why.
        return Assessment(sig.symbol, 0.0, "neutral", 0, 0.0, 0.0, "unknown", "unknown",
                          [], [sig.consistency_note or
                               "Price history and quote disagree; not scored."])

    # -- directional factors ------------------------------------------------
    trend = _clamp((sig.trend_stack or 0.0))
    # A trend only counts when it is actually a trend: ADX below ~18 is chop.
    adx_gate = _clamp((sig.adx or 15.0) / 25.0, 0.35, 1.0)
    trend_score = trend * adx_gate
    factors.append(Factor(
        "trend", trend_score, WEIGHTS["trend"],
        f"EMA stack {'bullish' if trend > 0.1 else 'bearish' if trend < -0.1 else 'mixed'}"
        f" (9/21/50), ADX {sig.adx:.0f}" if sig.adx else "EMA stack read",
    ))

    rsi = sig.rsi if sig.rsi is not None else 50.0
    momentum = _scale(rsi, 50.0, 22.0)
    factors.append(Factor(
        "momentum", momentum, WEIGHTS["momentum"],
        f"RSI(14) at {rsi:.0f}"
        + (" - overbought, momentum may be stretched" if rsi > 72 else
           " - oversold, momentum may be washed out" if rsi < 28 else ""),
    ))

    macd_score = _scale(sig.macd_hist, 0.0, 0.6)
    factors.append(Factor(
        "macd", macd_score, WEIGHTS["macd"],
        f"MACD histogram {sig.macd_hist:+.2f}% of price"
        if sig.macd_hist is not None else "MACD unavailable",
    ))

    # 3-day rate of change dominates for a 0-3 day holding period.
    roc3 = (sig.roc3 or 0.0) * 100
    atr_pct = sig.atr_pct or 1.5
    # Normalise the move by the stock's own daily range: a 3% move in a 1%-ATR
    # name is a far bigger event than in a 5%-ATR name.
    short_roc = _clamp(roc3 / max(atr_pct * 2.2, 0.5))
    factors.append(Factor(
        "short_roc", short_roc, WEIGHTS["short_roc"],
        f"3-day move {roc3:+.1f}% ({roc3 / atr_pct:+.1f} ATR)",
    ))

    # Mean reversion opposes the recent move when price is stretched to a band
    # edge without trend support - the classic fade setup.
    mr_score = 0.0
    pb = sig.percent_b
    if pb is not None:
        if pb > 1.0:
            mr_score = -_clamp((pb - 1.0) / 0.35)
        elif pb < 0.0:
            mr_score = _clamp((0.0 - pb) / 0.35)
    # Only fade when there is no strong trend to fight.
    mr_score *= 1.0 - min(abs(trend_score), 0.85)
    factors.append(Factor(
        "mean_reversion", mr_score, WEIGHTS["mean_reversion"],
        f"Bollinger %B at {pb:.2f}" + (" - outside the band, fade risk" if pb is not None and (pb > 1 or pb < 0) else "")
        if pb is not None else "Bollinger unavailable",
    ))

    rs = _scale(sig.rel_strength_spy, 0.0, 4.0)
    factors.append(Factor(
        "rel_strength", rs, WEIGHTS["rel_strength"],
        f"10-day relative strength vs SPY {sig.rel_strength_spy:+.1f}pp"
        if sig.rel_strength_spy is not None else "Relative strength unavailable",
    ))

    # Volume confirms direction: heavy volume behind an up move is bullish,
    # heavy volume behind a down move is bearish. Light volume confirms nothing.
    rvol = sig.relative_volume or 1.0
    vol_conf = _clamp((rvol - 1.0) / 1.2, 0.0, 1.0)
    vol_score = vol_conf * (1.0 if roc3 > 0 else -1.0 if roc3 < 0 else 0.0)
    factors.append(Factor(
        "volume", vol_score, WEIGHTS["volume"],
        f"Relative volume {rvol:.2f}x" + (" - conviction behind the move" if rvol > 1.3
                                          else " - participation is light" if rvol < 0.8 else ""),
    ))

    gap = sig.gap_pct or 0.0
    gap_score = _clamp(gap / max(atr_pct * 1.5, 0.4)) * 0.8
    factors.append(Factor(
        "gap", gap_score, WEIGHTS["gap"],
        f"Opening gap {gap:+.2f}%" if abs(gap) > 0.05 else "No meaningful opening gap",
    ))

    # Options positioning: a very high put/call ratio is contrarian-bullish.
    pos_score = 0.0
    pcr = sig.put_call_vol_ratio
    if pcr is not None:
        pos_score = _clamp((1.0 - pcr) / 0.8) * 0.6
    factors.append(Factor(
        "positioning", pos_score, WEIGHTS["positioning"],
        f"Put/call volume {pcr:.2f}" if pcr is not None else "Options flow unavailable",
    ))

    # -- aggregate ----------------------------------------------------------
    total_weight = sum(f.weight for f in factors)
    bias = sum(f.score * f.weight for f in factors) / total_weight if total_weight else 0.0
    bias = _clamp(bias * 1.45)   # the weighted mean compresses; re-expand to use the range

    # Agreement: what share of the weight points the same way as the bias.
    if abs(bias) > 1e-6:
        aligned = sum(f.weight for f in factors if f.score * bias > 0.02)
        engaged = sum(f.weight for f in factors if abs(f.score) > 0.02)
        agreement = aligned / engaged if engaged > 0 else 0.0
    else:
        agreement = 0.0

    quality, quality_notes = _tradeability(sig)
    warnings.extend(quality_notes)

    regime = _classify_regime(sig)
    iv_regime = _classify_iv(sig)

    # Conviction blends signal strength, factor agreement and trend cleanliness,
    # then gates the whole thing on whether the options are actually tradeable.
    strength = abs(bias)
    cleanliness = 0.5 + 0.5 * (sig.slope_r2 if sig.slope_r2 is not None else 0.3)
    raw = strength * (0.45 + 0.55 * agreement) * (0.75 + 0.25 * cleanliness)
    conviction = int(round(_clamp(raw, 0.0, 1.0) * quality * 100))

    if regime == "range" and abs(bias) > 0.35:
        warnings.append("Directional signal fired inside a range regime - size down or prefer defined-risk spreads.")
    if sig.rsi is not None and ((sig.rsi > 78 and bias > 0) or (sig.rsi < 22 and bias < 0)):
        warnings.append("Chasing an extended RSI reading; reversal risk is elevated.")

    direction = "bullish" if bias > 0.12 else "bearish" if bias < -0.12 else "neutral"

    return Assessment(
        symbol=sig.symbol, bias=round(bias, 4), direction=direction, conviction=conviction,
        agreement=round(agreement, 3), quality=round(quality, 3),
        regime=regime, iv_regime=iv_regime, factors=factors, warnings=warnings,
    )


def _tradeability(sig: Signals) -> tuple[float, list[str]]:
    """0..1 gate on whether this option chain can actually be traded.

    Wide markets and thin open interest are the single most common reason a
    'good' short-dated signal loses money, so they cut the score directly.
    """
    notes: list[str] = []
    quality = 1.0

    spread = sig.option_spread_pct
    if spread is None:
        quality *= 0.75
        notes.append("No option quotes available; liquidity could not be verified.")
    elif spread > 0.25:
        quality *= 0.35
        notes.append(f"Option spreads are very wide ({spread * 100:.0f}% of mid) - execution will eat the edge.")
    elif spread > 0.12:
        quality *= 0.70
        notes.append(f"Option spreads are wide ({spread * 100:.0f}% of mid).")
    elif spread > 0.06:
        quality *= 0.90

    oi = sig.option_oi
    if oi is not None:
        if oi < 500:
            quality *= 0.45
            notes.append("Near-the-money open interest is thin; expect slippage getting out.")
        elif oi < 2500:
            quality *= 0.80

    adv = sig.avg_dollar_volume
    if adv is not None and adv < 20_000_000:
        quality *= 0.70
        notes.append("Underlying trades under $20M/day - fine for stock, rough for same-day options.")

    return max(0.05, min(1.0, quality)), notes


def _classify_regime(sig: Signals) -> str:
    """Label the price regime, which decides which structures make sense."""
    adx = sig.adx or 0.0
    r2 = sig.slope_r2 or 0.0
    squeeze = sig.bb_squeeze
    rvol = sig.relative_volume or 1.0

    if squeeze is not None and squeeze < 15 and rvol > 1.25:
        return "vol_expansion"
    if adx >= 25 and r2 >= 0.45:
        return "trending"
    if adx < 18 and (sig.percent_b is not None and 0.15 < sig.percent_b < 0.85):
        return "range"
    if sig.percent_b is not None and (sig.percent_b > 1.0 or sig.percent_b < 0.0):
        return "mean_revert"
    return "range" if adx < 20 else "trending"


def _classify_iv(sig: Signals) -> str:
    """Is implied vol cheap or rich relative to what the stock actually does?

    Prefer IV rank when there is enough history; otherwise fall back to the
    IV/realised-vol ratio, which is a decent same-day proxy.
    """
    if sig.iv_rank is not None:
        if sig.iv_rank >= 65:
            return "rich"
        if sig.iv_rank <= 30:
            return "cheap"
        return "fair"
    ratio = sig.iv_rv_ratio
    if ratio is None:
        return "unknown"
    if ratio >= 1.35:
        return "rich"
    if ratio <= 0.95:
        return "cheap"
    return "fair"
