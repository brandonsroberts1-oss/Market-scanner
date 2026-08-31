"""Backtesting for the strategies the scanner produces.

Read this before trusting a number it prints.

Equity backtests are exact: they replay real historical bars, and the fills are
real prices plus a slippage assumption.

Option backtests are MODEL-BASED, and they have to be. Historical option chains
are a paid dataset that no free provider offers, so this engine reprices each
structure with Black-Scholes using an implied-vol estimate derived from the
underlying's own trailing realised volatility times a configurable premium.
That is a reasonable approximation of how liquid, near-the-money, short-dated
contracts actually behaved - and it is still an approximation. Specifically it
does not model:

  * the volatility smile across strikes (one vol is used per expiry),
  * IV crush around earnings and scheduled macro events,
  * bid/ask widening in a fast tape,
  * assignment, pin risk, and early exercise.

The engine is deliberately conservative elsewhere to compensate: signals are
computed only from bars strictly before the entry, entries fill at the *next*
bar's open, and every trade pays both commission and half-spread slippage.

Treat the output as a sanity check on whether a rule has any edge at all, not
as a forecast of returns.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone

import numpy as np

from ..analytics import blackscholes as bs
from ..analytics import indicators as ind
from ..config import settings
from ..engine.conviction import assess
from ..engine.signals import build_signals
from ..providers.base import Bar, Bars, Quote
from ..providers.registry import MarketData

log = logging.getLogger(__name__)

MULTIPLIER = 100.0


@dataclass
class BacktestTrade:
    symbol: str
    strategy: str
    direction: str
    entry_date: str
    exit_date: str
    entry_price: float          # underlying at entry
    exit_price: float           # underlying at exit
    conviction: int
    contracts: int
    entry_cost: float           # net debit (+) or credit (-) in dollars
    exit_value: float
    commission: float
    pnl: float
    pnl_pct: float
    exit_reason: str
    max_loss: float | None = None
    iv_entry: float | None = None
    underlying_move_pct: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BacktestResult:
    params: dict
    trades: list[dict] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    method: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _bars_slice(bars: Bars, end_index: int) -> Bars:
    """History strictly up to and including `end_index` - never beyond."""
    return Bars(bars.symbol, bars.bars[: end_index + 1])


def _modeled_iv(closes: list[float], iv_premium: float) -> float | None:
    """Implied-vol estimate for the option model: realised vol times a premium.

    Short-dated implied vol trades above subsequent realised vol most of the
    time (that is the variance risk premium), so a premium above 1.0 is the
    realistic default rather than a thumb on the scale - it makes options more
    expensive to buy in the backtest, not less.
    """
    rv = ind.realized_vol(closes, 20)
    if not math.isfinite(rv) or rv <= 0:
        return None
    return max(0.05, rv * iv_premium)


def _strike_step(spot: float) -> float:
    if spot < 25:
        return 0.5
    if spot < 100:
        return 1.0
    if spot < 250:
        return 2.5
    return 5.0


def _round_strike(price: float, spot: float) -> float:
    step = _strike_step(spot)
    return round(price / step) * step


def _strike_for_delta(spot: float, vol: float, t: float, rate: float,
                      kind: str, target_delta: float) -> float:
    """The listed strike whose delta is closest to the target.

    Real chains are quoted on a discrete grid, and for very short expiries that
    grid is coarse relative to the expected move - at a $765 underlying with
    2 DTE and 13% vol, consecutive $5 strikes are two-thirds of a standard
    deviation apart, so a 0.15-delta contract may simply not be listed. Solving
    for the continuous strike and rounding the *price* can therefore land on
    the wrong side of the target; this compares the neighbouring listed strikes
    by delta and takes the better one, which is what a trader reading the chain
    would do.
    """
    lo, hi = spot * 0.5, spot * 1.6
    for _ in range(60):
        mid = (lo + hi) / 2
        d = abs(bs.greeks(spot, mid, t, rate, vol, kind).delta)
        if kind == "call":
            # Call delta falls as the strike rises.
            if d > target_delta:
                lo = mid
            else:
                hi = mid
        else:
            # Put delta (absolute) rises as the strike rises.
            if d > target_delta:
                hi = mid
            else:
                lo = mid

    exact = (lo + hi) / 2
    step = _strike_step(spot)
    base = _round_strike(exact, spot)
    candidates = [base - step, base, base + step]
    valid = [k for k in candidates if k > 0]
    return min(valid, key=lambda k: abs(
        abs(bs.greeks(spot, k, t, rate, vol, kind).delta) - target_delta))


@dataclass
class ModelLeg:
    action: str
    kind: str
    strike: float
    quantity: int = 1


def _price_structure(legs: list[ModelLeg], spot: float, vol: float, t: float,
                     rate: float, spread_pct: float, side: str) -> float:
    """Net dollars to open ('open') or close ('close') the structure.

    Slippage is applied per leg in the direction that hurts: you buy above the
    theoretical value and sell below it, both on entry and on exit.
    """
    total = 0.0
    for leg in legs:
        theo = bs.price(spot, leg.strike, t, rate, vol, leg.kind)
        theo = max(theo, 0.0)
        half = theo * spread_pct / 2.0
        buying = (leg.action == "buy") if side == "open" else (leg.action == "sell")
        px = theo + half if buying else max(0.0, theo - half)
        sign = 1 if leg.action == "buy" else -1
        if side == "close":
            sign = -sign
        total += sign * px * leg.quantity * MULTIPLIER
    return total


def _intrinsic(legs: list[ModelLeg], spot: float) -> float:
    total = 0.0
    for leg in legs:
        val = max(spot - leg.strike, 0.0) if leg.kind == "call" else max(leg.strike - spot, 0.0)
        total += (1 if leg.action == "buy" else -1) * val * leg.quantity * MULTIPLIER
    return total


def _build_structure(strategy: str, spot: float, vol: float, t: float, rate: float
                     ) -> list[ModelLeg] | None:
    """Reconstruct the scanner's structures with modelled strikes."""
    if strategy == "long_call":
        return [ModelLeg("buy", "call", _strike_for_delta(spot, vol, t, rate, "call", 0.45))]
    if strategy == "long_put":
        return [ModelLeg("buy", "put", _strike_for_delta(spot, vol, t, rate, "put", 0.45))]
    if strategy == "bull_call_spread":
        lo = _strike_for_delta(spot, vol, t, rate, "call", 0.50)
        hi = _strike_for_delta(spot, vol, t, rate, "call", 0.28)
        if hi <= lo:
            hi = lo + _strike_step(spot)
        return [ModelLeg("buy", "call", lo), ModelLeg("sell", "call", hi)]
    if strategy == "bear_put_spread":
        hi = _strike_for_delta(spot, vol, t, rate, "put", 0.50)
        lo = _strike_for_delta(spot, vol, t, rate, "put", 0.28)
        if lo >= hi:
            lo = hi - _strike_step(spot)
        return [ModelLeg("buy", "put", hi), ModelLeg("sell", "put", lo)]
    if strategy == "bull_put_spread":
        short = _strike_for_delta(spot, vol, t, rate, "put", 0.28)
        long_ = _strike_for_delta(spot, vol, t, rate, "put", 0.14)
        if long_ >= short:
            long_ = short - _strike_step(spot)
        return [ModelLeg("sell", "put", short), ModelLeg("buy", "put", long_)]
    if strategy == "bear_call_spread":
        short = _strike_for_delta(spot, vol, t, rate, "call", 0.28)
        long_ = _strike_for_delta(spot, vol, t, rate, "call", 0.14)
        if long_ <= short:
            long_ = short + _strike_step(spot)
        return [ModelLeg("sell", "call", short), ModelLeg("buy", "call", long_)]
    if strategy == "iron_condor":
        ps = _strike_for_delta(spot, vol, t, rate, "put", 0.20)
        pl = _strike_for_delta(spot, vol, t, rate, "put", 0.10)
        cs = _strike_for_delta(spot, vol, t, rate, "call", 0.20)
        cl = _strike_for_delta(spot, vol, t, rate, "call", 0.10)
        step = _strike_step(spot)
        if pl >= ps:
            pl = ps - step
        if cl <= cs:
            cl = cs + step
        return [ModelLeg("sell", "put", ps), ModelLeg("buy", "put", pl),
                ModelLeg("sell", "call", cs), ModelLeg("buy", "call", cl)]
    if strategy == "long_strangle":
        c = _strike_for_delta(spot, vol, t, rate, "call", 0.30)
        p = _strike_for_delta(spot, vol, t, rate, "put", 0.30)
        return [ModelLeg("buy", "call", c), ModelLeg("buy", "put", p)]
    return None


# Which structure the backtest trades for a given directional read. Mirrors the
# live scanner's selection logic so the test measures the same rules.
def _choose_strategy(assessment, allowed: list[str] | None) -> str | None:
    direction, iv = assessment.direction, assessment.iv_regime
    if direction == "bullish":
        options = ["bull_call_spread", "long_call"] if iv in ("cheap", "fair", "unknown") \
            else ["bull_put_spread", "bull_call_spread"]
    elif direction == "bearish":
        options = ["bear_put_spread", "long_put"] if iv in ("cheap", "fair", "unknown") \
            else ["bear_call_spread", "bear_put_spread"]
    else:
        options = ["iron_condor"] if iv in ("rich", "fair") else []
    if allowed:
        options = [o for o in options if o in allowed]
    return options[0] if options else None


# --------------------------------------------------------------------------
# Runner
#
# Sign convention used throughout: _price_structure returns a NET DEBIT.
# Positive means cash leaves the account, negative means cash arrives. So for
# any structure, pnl = -(open_debit + close_debit).
# --------------------------------------------------------------------------
async def run_backtest(
    market: MarketData,
    symbols: list[str],
    lookback_days: int = 400,
    hold_days: int = 3,
    min_conviction: int = 55,
    dte: int = 3,
    contracts: int = 1,
    starting_cash: float = 25_000.0,
    profit_target_pct: float = 60.0,
    stop_loss_pct: float = 50.0,
    iv_premium: float = 1.15,
    spread_pct: float = 0.04,
    mode: str = "options",              # "options" | "equity"
    allowed_strategies: list[str] | None = None,
    risk_per_trade_pct: float = 5.0,
) -> BacktestResult:
    """Replay the scanner's rules over history and measure what they produced."""
    rate = settings.risk_free_rate
    params = {
        "symbols": symbols, "lookback_days": lookback_days, "hold_days": hold_days,
        "min_conviction": min_conviction, "dte": dte, "contracts": contracts,
        "starting_cash": starting_cash, "profit_target_pct": profit_target_pct,
        "stop_loss_pct": stop_loss_pct, "iv_premium": iv_premium,
        "spread_pct": spread_pct, "mode": mode,
        "allowed_strategies": allowed_strategies, "risk_per_trade_pct": risk_per_trade_pct,
    }
    warnings: list[str] = []
    method = (
        "Exact replay of historical daily bars; fills at the next open plus slippage."
        if mode == "equity" else
        "Model-based: structures are repriced with Black-Scholes using realised "
        f"volatility x {iv_premium:.2f} as the implied-vol estimate. No historical "
        "option chain is used. See module docstring for what this does not capture."
    )

    spy_bars = await market.history("SPY", max(lookback_days, 260))
    spy_closes = spy_bars.closes

    # ---- Phase 1: find every entry signal, per symbol, with no lookahead ----
    candidates: list[dict] = []

    for symbol in symbols:
        bars = await market.history(symbol, lookback_days)
        if len(bars) < 90:
            warnings.append(f"{symbol}: only {len(bars)} bars available, skipped.")
            continue

        series = bars.bars
        # Leave room for indicator warm-up at the start and for the hold at the end.
        for i in range(60, len(series) - hold_days - 1):
            window = _bars_slice(bars, i)
            closes = window.closes

            # A synthetic quote built from the bar being scored - never from a
            # later bar. This is the no-lookahead boundary.
            bar = series[i]
            quote = Quote(symbol=symbol.upper(), last=bar.close,
                          previous_close=series[i - 1].close, open=bar.open,
                          high=bar.high, low=bar.low, volume=bar.volume)

            sig = build_signals(symbol, window, quote, None,
                                spy_closes[: i + 1] if len(spy_closes) > i else spy_closes)

            # Supply the modelled IV so the vol-regime logic behaves as it does
            # live, instead of every historical day reading as "unknown".
            vol = _modeled_iv(closes, iv_premium)
            if vol is None:
                continue
            sig.atm_iv = vol
            if sig.realized_vol:
                sig.iv_rv_ratio = vol / sig.realized_vol
            rv_hist = [ind.realized_vol(closes[: j + 1], 20)
                       for j in range(max(20, len(closes) - 120), len(closes))]
            rv_hist = [r * iv_premium for r in rv_hist if math.isfinite(r)]
            if rv_hist:
                sig.iv_rank = ind.percentile_rank(vol, rv_hist)
            # Liquidity is unknown historically; assume tradeable rather than
            # letting the gate silently zero out every score.
            sig.option_spread_pct = spread_pct
            sig.option_oi = 5000.0

            assessment = assess(sig)
            if assessment.conviction < min_conviction:
                continue
            if series[i + 1].open <= 0:
                continue
            if mode == "equity" and assessment.direction == "neutral":
                continue
            if mode == "options" and _choose_strategy(assessment, allowed_strategies) is None:
                continue

            candidates.append({
                "symbol": symbol, "series": series, "index": i,
                "entry_date": series[i + 1].date, "assessment": assessment,
                "sig": sig, "vol": vol,
            })

    # ---- Phase 2: replay chronologically against a single account ----------
    # Sizing off the *running* equity (not the starting balance) and refusing
    # trades the account cannot fund is what keeps the curve honest; sizing off
    # the opening balance lets a losing run compound past -100%.
    candidates.sort(key=lambda c: (c["entry_date"], c["symbol"]))

    equity = starting_cash
    trades: list[BacktestTrade] = []
    busy_until: dict[str, str] = {}       # symbol -> exit date of its open trade

    for cand in candidates:
        symbol = cand["symbol"]
        if equity <= 0:
            warnings.append("Account was exhausted; remaining signals were not traded.")
            break
        # One position per symbol at a time.
        if busy_until.get(symbol, "") >= cand["entry_date"]:
            continue

        if mode == "equity":
            trade = _equity_trade(symbol, cand["series"], cand["index"], cand["assessment"],
                                  cand["sig"], hold_days, profit_target_pct, stop_loss_pct,
                                  spread_pct, equity, risk_per_trade_pct)
        else:
            strategy = _choose_strategy(cand["assessment"], allowed_strategies)
            if strategy is None:
                continue
            trade = _option_trade(symbol, cand["series"], cand["index"], cand["assessment"],
                                  strategy, cand["vol"], dte, hold_days, rate, spread_pct,
                                  contracts, profit_target_pct, stop_loss_pct,
                                  equity, risk_per_trade_pct)

        if not trade:
            continue
        equity += trade.pnl
        trades.append(trade)
        busy_until[symbol] = trade.exit_date

    trades.sort(key=lambda t: t.entry_date)
    curve, stats = _summarise(trades, starting_cash)
    if mode == "options":
        warnings.append(
            "Option P&L is modelled, not replayed from historical chains. "
            "Real fills would differ, especially around earnings and macro events."
        )

    return BacktestResult(params=params, trades=[t.to_dict() for t in trades],
                          equity_curve=curve, stats=stats, warnings=warnings, method=method)


def _structure_risk(legs: list[ModelLeg], open_debit_per_contract: float,
                    contracts: int) -> float:
    """Capital at risk: the debit for a long structure, width less credit for a short."""
    if open_debit_per_contract > 0:
        return max(open_debit_per_contract * contracts, 1.0)
    widths = [abs(a.strike - b.strike)
              for a in legs for b in legs
              if a.kind == b.kind and a.action != b.action]
    width = max(widths) if widths else 0.0
    return max(width * MULTIPLIER * contracts + open_debit_per_contract * contracts, 1.0)


def _option_trade(symbol: str, series: list[Bar], i: int, assessment, strategy: str,
                  vol: float, dte: int, hold_days: int, rate: float, spread_pct: float,
                  contracts: int, profit_target_pct: float, stop_loss_pct: float,
                  equity: float, risk_per_trade_pct: float) -> BacktestTrade | None:
    entry_bar = series[i + 1]
    entry_spot = entry_bar.open
    t_entry = max(dte, 1) * bs.DAY

    legs = _build_structure(strategy, entry_spot, vol, t_entry, rate)
    if not legs:
        return None
    for leg in legs:
        leg.quantity = contracts

    open_debit = _price_structure(legs, entry_spot, vol, t_entry, rate, spread_pct, "open")
    if abs(open_debit) < 1.0:
        return None

    # Scale size down to the per-trade risk budget, and refuse the trade
    # outright if even one contract is more than the account can risk.
    budget = max(equity, 0.0) * risk_per_trade_pct / 100.0
    risk_one = _structure_risk(legs, open_debit / contracts, 1)
    if risk_one <= 0 or budget < risk_one:
        return None
    contracts = max(1, min(contracts, int(budget / risk_one)))
    for leg in legs:
        leg.quantity = contracts
    open_debit = _price_structure(legs, entry_spot, vol, t_entry, rate, spread_pct, "open")
    if open_debit > 0 and open_debit > equity:
        return None

    risk = _structure_risk(legs, open_debit / contracts, contracts)

    commission = settings.option_commission * len(legs) * contracts * 2   # round trip

    exit_reason = "time"
    exit_index = min(i + 1 + hold_days, len(series) - 1)

    for step in range(1, hold_days + 1):
        idx = i + 1 + step
        if idx >= len(series):
            break
        spot = series[idx].close
        remaining = max(dte - step, 0)
        if remaining <= 0:
            value_debit = -_intrinsic(legs, spot)
        else:
            value_debit = _price_structure(legs, spot, vol, remaining * bs.DAY, rate,
                                           spread_pct, "close")
        pnl = -(open_debit + value_debit)
        if pnl >= risk * profit_target_pct / 100.0:
            exit_reason, exit_index = "target", idx
            break
        if pnl <= -risk * stop_loss_pct / 100.0:
            exit_reason, exit_index = "stop", idx
            break
        exit_index = idx

    exit_bar = series[exit_index]
    exit_spot = exit_bar.close
    days_held = exit_index - (i + 1)
    remaining = max(dte - days_held, 0)
    if remaining <= 0:
        close_debit = -_intrinsic(legs, exit_spot)
        if exit_reason == "time":
            exit_reason = "expiry"
    else:
        close_debit = _price_structure(legs, exit_spot, vol, remaining * bs.DAY, rate,
                                       spread_pct, "close")

    pnl = -(open_debit + close_debit) - commission

    return BacktestTrade(
        symbol=symbol.upper(), strategy=strategy, direction=assessment.direction,
        entry_date=entry_bar.date, exit_date=exit_bar.date,
        entry_price=round(entry_spot, 2), exit_price=round(exit_spot, 2),
        conviction=assessment.conviction, contracts=contracts,
        entry_cost=round(open_debit, 2), exit_value=round(-close_debit, 2),
        commission=round(commission, 2), pnl=round(pnl, 2),
        pnl_pct=round(pnl / risk * 100, 2), exit_reason=exit_reason,
        max_loss=round(risk, 2), iv_entry=round(vol, 4),
        underlying_move_pct=round((exit_spot / entry_spot - 1) * 100, 2),
    )


def _equity_trade(symbol: str, series: list[Bar], i: int, assessment, sig, hold_days: int,
                  profit_target_pct: float, stop_loss_pct: float, spread_pct: float,
                  equity: float, risk_per_trade_pct: float) -> BacktestTrade | None:
    if assessment.direction == "neutral":
        return None
    entry_bar = series[i + 1]
    long_side = assessment.direction == "bullish"
    slip = entry_bar.open * min(spread_pct, 0.01) / 2.0
    entry = entry_bar.open + (slip if long_side else -slip)
    if entry <= 0:
        return None

    # Size by risk: an ATR-based stop and a fixed fraction of capital at risk.
    atr = sig.atr or entry * 0.02
    stop_distance = 1.5 * atr
    risk_budget = max(equity, 0.0) * risk_per_trade_pct / 100.0
    shares = int(risk_budget / max(stop_distance, 0.01))
    # Never allocate more than the account holds.
    shares = min(shares, int(max(equity, 0.0) / entry))
    if shares < 1:
        return None

    target = entry + (3.0 * atr if long_side else -3.0 * atr)
    stop = entry - (stop_distance if long_side else -stop_distance)

    exit_reason, exit_index, exit_price = "time", min(i + 1 + hold_days, len(series) - 1), None
    for step in range(1, hold_days + 1):
        idx = i + 1 + step
        if idx >= len(series):
            break
        bar = series[idx]
        # Stop is checked before target: the conservative assumption when a
        # single daily bar spans both levels.
        if long_side and bar.low <= stop:
            exit_reason, exit_index, exit_price = "stop", idx, stop
            break
        if not long_side and bar.high >= stop:
            exit_reason, exit_index, exit_price = "stop", idx, stop
            break
        if long_side and bar.high >= target:
            exit_reason, exit_index, exit_price = "target", idx, target
            break
        if not long_side and bar.low <= target:
            exit_reason, exit_index, exit_price = "target", idx, target
            break
        exit_index = idx

    exit_bar = series[exit_index]
    if exit_price is None:
        exit_price = exit_bar.close - (slip if long_side else -slip)

    direction = 1 if long_side else -1
    gross = (exit_price - entry) * shares * direction
    commission = settings.equity_commission * 2
    pnl = gross - commission
    capital = entry * shares

    return BacktestTrade(
        symbol=symbol.upper(), strategy="equity_long" if long_side else "equity_short",
        direction=assessment.direction, entry_date=entry_bar.date, exit_date=exit_bar.date,
        entry_price=round(entry, 2), exit_price=round(exit_price, 2),
        conviction=assessment.conviction, contracts=shares,
        entry_cost=round(capital, 2), exit_value=round(exit_price * shares, 2),
        commission=round(commission, 2), pnl=round(pnl, 2),
        pnl_pct=round(pnl / max(capital, 1) * 100, 2), exit_reason=exit_reason,
        max_loss=round(stop_distance * shares, 2),
        underlying_move_pct=round((exit_bar.close / entry - 1) * 100, 2),
    )


def _summarise(trades: list[BacktestTrade], starting_cash: float) -> tuple[list[dict], dict]:
    if not trades:
        return [], {"trades": 0, "note": "No trades met the entry criteria over this window."}

    equity = starting_cash
    curve = [{"date": trades[0].entry_date, "equity": round(equity, 2)}]
    for t in trades:
        equity += t.pnl
        curve.append({"date": t.exit_date, "equity": round(equity, 2)})

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    equities = [c["equity"] for c in curve]
    rets = np.diff(np.asarray(equities)) / np.asarray(equities[:-1])

    by_strategy: dict[str, dict] = {}
    for t in trades:
        entry = by_strategy.setdefault(t.strategy, {"trades": 0, "pnl": 0.0, "wins": 0})
        entry["trades"] += 1
        entry["pnl"] = round(entry["pnl"] + t.pnl, 2)
        entry["wins"] += 1 if t.pnl > 0 else 0
    for entry in by_strategy.values():
        entry["win_rate"] = round(entry["wins"] / entry["trades"] * 100, 1)

    exits: dict[str, int] = {}
    for t in trades:
        exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1

    return curve, {
        "trades": len(trades),
        "net_pnl": round(sum(pnls), 2),
        "return_pct": round((equity / starting_cash - 1) * 100, 2),
        "ending_equity": round(equity, 2),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "expectancy": round(sum(pnls) / len(pnls), 2),
        "best": round(max(pnls), 2),
        "worst": round(min(pnls), 2),
        "max_drawdown_pct": round(ind.max_drawdown(equities) * 100, 2),
        "sharpe": round(ind.sharpe(rets, periods_per_year=52), 2)
        if len(rets) > 2 and math.isfinite(ind.sharpe(rets, periods_per_year=52)) else None,
        "avg_hold_days": round(sum(
            (date.fromisoformat(t.exit_date[:10]) - date.fromisoformat(t.entry_date[:10])).days
            for t in trades) / len(trades), 1),
        "total_commission": round(sum(t.commission for t in trades), 2),
        "by_strategy": by_strategy,
        "exit_reasons": exits,
    }
