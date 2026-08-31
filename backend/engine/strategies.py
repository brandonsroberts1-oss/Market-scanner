"""Turn a directional read into concrete, priced option structures.

Given a scored symbol and a live chain this module selects real strikes, prices
the structure against the actual bid/ask, and reports max profit, max loss,
breakevens, probability of profit and expected value.

Two expected values are reported and they mean different things:

  ev_risk_neutral - EV under the market's own risk-neutral distribution. This
      is approximately zero minus costs for any fairly-priced structure. It is
      the honest baseline: if it is strongly negative, the structure is
      expensive to own regardless of whether the directional call is right.

  ev_model - EV under a distribution tilted by the scanner's directional bias.
      This is the app's edge claim, and it is only as good as the bias. Treat
      it as "what this trade pays if the model is right", not as a forecast.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import date

from ..analytics import blackscholes as bs
from ..providers.base import OptionChain, OptionContract
from .conviction import Assessment
from .signals import Signals

MULTIPLIER = 100.0


@dataclass
class Leg:
    action: str            # "buy" | "sell"
    kind: str              # "call" | "put"
    strike: float
    expiration: str
    symbol: str            # OCC contract symbol
    quantity: int = 1
    price: float = 0.0     # per-share fill price used for the quoted cost
    bid: float | None = None
    ask: float | None = None
    delta: float | None = None
    iv: float | None = None
    open_interest: float | None = None
    volume: float | None = None

    @property
    def sign(self) -> int:
        return 1 if self.action == "buy" else -1

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StrategyIdea:
    symbol: str
    strategy: str              # machine key, e.g. "bull_call_spread"
    label: str                 # human name
    direction: str
    expiration: str
    dte: int
    legs: list[Leg] = field(default_factory=list)

    net_cost: float = 0.0      # per spread, dollars. >0 debit, <0 credit
    max_profit: float | None = None    # None means theoretically unbounded
    max_loss: float | None = None
    breakevens: list[float] = field(default_factory=list)
    prob_profit: float | None = None
    risk_reward: float | None = None
    ev_risk_neutral: float | None = None
    ev_model: float | None = None

    conviction: int = 0
    score: float = 0.0         # ranking score combining conviction and structure
    underlying_price: float = 0.0
    expected_move: float = 0.0
    net_delta: float = 0.0
    net_theta: float = 0.0
    net_vega: float = 0.0
    rationale: str = ""
    risk_note: str = ""
    exit_plan: str = ""
    liquidity: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["legs"] = [leg.to_dict() for leg in self.legs]
        return d


# --------------------------------------------------------------------------
# Fills
# --------------------------------------------------------------------------
def fill_price(contract: OptionContract, action: str, aggressiveness: float = 0.35) -> float | None:
    """Realistic fill: cross part of the spread rather than assuming mid.

    aggressiveness 0.0 = fill at mid (optimistic), 1.0 = pay the full spread.
    The 0.35 default reflects what a marketable limit typically achieves on a
    liquid, near-the-money contract.
    """
    mid = contract.mid
    if mid is None or mid <= 0:
        return None
    half = (contract.spread or 0.0) / 2.0
    price = mid + half * aggressiveness if action == "buy" else mid - half * aggressiveness
    return max(0.01, round(price, 2))


def _pick(contracts: list[OptionContract], target_delta: float) -> OptionContract | None:
    """Nearest contract to a target delta, ignoring illiquid or unpriced ones."""
    pool = [c for c in contracts
            if c.delta is not None and c.mid and c.mid > 0.02 and (c.ask or 0) > 0]
    if not pool:
        return None
    return min(pool, key=lambda c: abs(abs(c.delta) - abs(target_delta)))


def _pick_strike(contracts: list[OptionContract], strike: float) -> OptionContract | None:
    pool = [c for c in contracts if c.mid and c.mid > 0.01]
    if not pool:
        return None
    return min(pool, key=lambda c: abs(c.strike - strike))


def _leg(contract: OptionContract, action: str, aggressiveness: float) -> Leg | None:
    price = fill_price(contract, action, aggressiveness)
    if price is None:
        return None
    return Leg(action=action, kind=contract.kind, strike=contract.strike,
               expiration=contract.expiration, symbol=contract.symbol, price=price,
               bid=contract.bid, ask=contract.ask, delta=contract.delta,
               iv=contract.implied_volatility, open_interest=contract.open_interest,
               volume=contract.volume)


# --------------------------------------------------------------------------
# Payoff and probability
# --------------------------------------------------------------------------
def payoff_at(legs: list[Leg], spot: float) -> float:
    """P&L per spread at expiry for a given underlying price, in dollars."""
    total = 0.0
    for leg in legs:
        intrinsic = max(spot - leg.strike, 0.0) if leg.kind == "call" else max(leg.strike - spot, 0.0)
        total += leg.sign * (intrinsic - leg.price) * leg.quantity * MULTIPLIER
    return total


def payoff_curve(legs: list[Leg], spot: float, points: int = 61, width: float = 0.18):
    """Payoff sampled across +/- `width` around spot, for charting."""
    lo, hi = spot * (1 - width), spot * (1 + width)
    step = (hi - lo) / max(points - 1, 1)
    return [{"price": round(lo + i * step, 2),
             "pnl": round(payoff_at(legs, lo + i * step), 2)} for i in range(points)]


def _price_grid(spot: float, vol: float, t: float, nodes: int = 241, span: float = 4.5) -> list[float]:
    """A log-spaced grid of terminal prices wide enough to cover both drifts."""
    if t <= 0 or vol <= 0 or spot <= 0:
        return [spot]
    sigma = vol * math.sqrt(t)
    mu = math.log(spot)
    # Pad the span so a drifted distribution still fits inside the same grid.
    lo, hi = mu - (span + 1.0) * sigma, mu + (span + 1.0) * sigma
    step = (hi - lo) / (nodes - 1)
    return [math.exp(lo + i * step) for i in range(nodes)]


def _lognormal_weights(prices: list[float], spot: float, vol: float, t: float,
                       drift: float = 0.0) -> list[float]:
    """Normalised lognormal probability mass over a *given* price grid.

    Taking the grid as input is what lets the risk-neutral and model-tilted
    distributions be compared: both are evaluated at the same terminal prices,
    so only the weights differ.
    """
    if t <= 0 or vol <= 0 or spot <= 0 or len(prices) < 2:
        return [1.0] + [0.0] * (len(prices) - 1)
    sigma = vol * math.sqrt(t)
    mu = math.log(spot) + (drift - 0.5 * vol * vol) * t
    logs = [math.log(p) for p in prices]
    # Weight each node by density x its share of log-space, so an uneven grid
    # would still integrate correctly.
    dens = []
    for i, x in enumerate(logs):
        lo = logs[i - 1] if i > 0 else logs[0] - (logs[1] - logs[0])
        hi = logs[i + 1] if i < len(logs) - 1 else logs[-1] + (logs[-1] - logs[-2])
        dens.append(math.exp(-0.5 * ((x - mu) / sigma) ** 2) * (hi - lo) / 2.0)
    total = sum(dens)
    if total <= 0:
        return [1.0 / len(prices)] * len(prices)
    return [d / total for d in dens]


def intrinsic_at(legs: list[Leg], spot: float) -> float:
    """Gross intrinsic value of the structure at expiry, ignoring premium paid."""
    total = 0.0
    for leg in legs:
        intrinsic = max(spot - leg.strike, 0.0) if leg.kind == "call" else max(leg.strike - spot, 0.0)
        total += leg.sign * intrinsic * leg.quantity * MULTIPLIER
    return total


def structure_metrics(legs: list[Leg], spot: float, vol: float, dte: int,
                      bias: float = 0.0, rate: float = 0.04) -> dict:
    """Probability of profit and both expected values, by numeric integration.

    The risk-neutral distribution drifts at the risk-free rate, which is what
    Black-Scholes assumes; getting this right is what makes ev_risk_neutral
    land at ~0 for a fairly-priced structure instead of showing phantom edge.
    """
    t = max(dte, 0.35) * bs.DAY
    prices = _price_grid(spot, vol, t)
    w_rn = _lognormal_weights(prices, spot, vol, t, drift=rate)

    # Translate directional bias into an annualised excess drift, capped at
    # +/-60%/yr so even a maximal signal cannot claim an absurd edge.
    model_drift = rate + max(-0.6, min(0.6, bias * 0.6))
    w_model = _lognormal_weights(prices, spot, vol, t, drift=model_drift)

    net_cost = _net_cost(legs)
    discount = math.exp(-rate * t)

    gross_rn = gross_model = pop = pop_model = 0.0
    for price, p_rn, p_m in zip(prices, w_rn, w_model):
        gross = intrinsic_at(legs, price)
        gross_rn += gross * p_rn
        gross_model += gross * p_m
        if gross - net_cost > 0:
            pop += p_rn
            pop_model += p_m

    return {"prob_profit": round(pop, 4),
            "prob_profit_model": round(pop_model, 4),
            "ev_risk_neutral": round(gross_rn * discount - net_cost, 2),
            "ev_model": round(gross_model * discount - net_cost, 2)}


def _breakevens(legs: list[Leg], spot: float) -> list[float]:
    """Find sign changes of the payoff on a fine grid, then bisect each one."""
    lo, hi = max(spot * 0.4, 0.01), spot * 1.9
    steps = 900
    step = (hi - lo) / steps
    out: list[float] = []
    prev_x = lo
    prev_y = payoff_at(legs, lo)
    for i in range(1, steps + 1):
        x = lo + i * step
        y = payoff_at(legs, x)
        if prev_y == 0.0:
            out.append(round(prev_x, 2))
        elif prev_y * y < 0:
            a, b = prev_x, x
            for _ in range(60):
                m = (a + b) / 2
                if payoff_at(legs, a) * payoff_at(legs, m) <= 0:
                    b = m
                else:
                    a = m
            out.append(round((a + b) / 2, 2))
        prev_x, prev_y = x, y
    # De-duplicate near-identical roots produced by flat segments.
    deduped: list[float] = []
    for value in sorted(out):
        if not deduped or abs(value - deduped[-1]) > max(0.01, spot * 0.001):
            deduped.append(value)
    return deduped


def _greeks_of(legs: list[Leg]) -> tuple[float, float, float]:
    """Net delta (shares equivalent), theta ($/day) and vega ($/vol point)."""
    d = t = v = 0.0
    for leg in legs:
        if leg.delta is not None:
            d += leg.sign * leg.delta * leg.quantity * MULTIPLIER
    return d, t, v


def _net_cost(legs: list[Leg]) -> float:
    return sum(leg.sign * leg.price * leg.quantity for leg in legs) * MULTIPLIER


def _liquidity_note(legs: list[Leg]) -> str:
    spreads = []
    for leg in legs:
        if leg.bid is not None and leg.ask and leg.ask > 0:
            mid = (leg.bid + leg.ask) / 2
            if mid > 0:
                spreads.append((leg.ask - leg.bid) / mid)
    oi = min((leg.open_interest or 0) for leg in legs) if legs else 0
    if not spreads:
        return "Liquidity unknown - no two-sided quotes."
    worst = max(spreads) * 100
    return f"Widest leg spread {worst:.0f}% of mid, thinnest leg OI {oi:,.0f}."


# --------------------------------------------------------------------------
# Structure builders
# --------------------------------------------------------------------------
def _finalise(idea: StrategyIdea, spot: float, vol: float, bias: float, rate: float) -> StrategyIdea:
    """Fill in the derived economics shared by every structure."""
    idea.net_cost = round(_net_cost(idea.legs), 2)
    idea.breakevens = _breakevens(idea.legs, spot)
    idea.underlying_price = spot

    metrics = structure_metrics(idea.legs, spot, vol, idea.dte, bias, rate)
    idea.prob_profit = metrics["prob_profit"]
    idea.ev_risk_neutral = metrics["ev_risk_neutral"]
    idea.ev_model = metrics["ev_model"]

    if idea.max_profit is not None and idea.max_loss and idea.max_loss > 0:
        idea.risk_reward = round(idea.max_profit / idea.max_loss, 2)

    idea.net_delta = round(_greeks_of(idea.legs)[0], 1)
    idea.net_theta = round(sum(
        leg.sign * (bs.greeks(spot, leg.strike, max(idea.dte, 0.35) * bs.DAY, rate,
                              leg.iv or vol, leg.kind).theta) * leg.quantity * MULTIPLIER
        for leg in idea.legs), 2)
    idea.net_vega = round(sum(
        leg.sign * (bs.greeks(spot, leg.strike, max(idea.dte, 0.35) * bs.DAY, rate,
                              leg.iv or vol, leg.kind).vega) * leg.quantity * MULTIPLIER
        for leg in idea.legs), 2)
    idea.liquidity = _liquidity_note(idea.legs)
    return idea


def _vertical(chain_side: list[OptionContract], long_delta: float, short_delta: float,
              kind: str, aggressiveness: float) -> tuple[Leg, Leg] | None:
    long_c = _pick(chain_side, long_delta)
    short_c = _pick(chain_side, short_delta)
    if not long_c or not short_c or long_c.strike == short_c.strike:
        return None
    long_leg, short_leg = _leg(long_c, "buy", aggressiveness), _leg(short_c, "sell", aggressiveness)
    if not long_leg or not short_leg:
        return None
    return long_leg, short_leg


def build_ideas(assessment: Assessment, sig: Signals, chain: OptionChain, dte: int,
                rate: float = 0.04, aggressiveness: float = 0.35) -> list[StrategyIdea]:
    """Produce every structure that fits this symbol's read, priced and scored.

    The selection logic is: direction decides call-side vs put-side, the IV
    regime decides debit vs credit, and conviction decides how much of the
    move the structure needs in order to pay.
    """
    spot = chain.underlying_price or sig.price
    if not spot:
        return []
    vol = sig.atm_iv or sig.realized_vol or 0.25
    bias = assessment.bias
    conv = assessment.conviction
    ideas: list[StrategyIdea] = []
    exp = chain.expiration
    em = bs.expected_move(spot, vol, max(dte, 1))
    em_pct = em / spot * 100 if spot else 0.0

    bullish = bias > 0.12
    bearish = bias < -0.12
    iv = assessment.iv_regime

    def new(strategy: str, label: str, direction: str, legs: list[Leg]) -> StrategyIdea:
        return StrategyIdea(symbol=sig.symbol, strategy=strategy, label=label,
                            direction=direction, expiration=exp, dte=dte, legs=legs,
                            conviction=conv, expected_move=round(em, 2))

    # -- directional: long single option --------------------------------------
    # Only worth it when the move is expected to be large relative to the
    # premium: a long option bleeds theta hard inside three days.
    if (bullish or bearish) and conv >= 45 and iv in ("cheap", "fair", "unknown"):
        side = chain.calls if bullish else chain.puts
        # ~45-delta: enough gamma to pay on a one-ATR move without buying
        # a lottery ticket.
        contract = _pick(side, 0.45)
        leg = _leg(contract, "buy", aggressiveness) if contract else None
        if leg:
            kind = "call" if bullish else "put"
            idea = new(f"long_{kind}", f"Long {kind.title()}",
                       "bullish" if bullish else "bearish", [leg])
            idea.max_loss = round(leg.price * MULTIPLIER, 2)
            idea.max_profit = None    # unbounded (calls) / bounded by strike (puts)
            if kind == "put":
                idea.max_profit = round((leg.strike - leg.price) * MULTIPLIER, 2)
            idea.rationale = (
                f"{assessment.direction.title()} read with conviction {conv}. Implied vol is "
                f"{iv}, so paying premium is defensible. Needs roughly a "
                f"{abs(leg.strike + (leg.price if kind == 'call' else -leg.price) - spot) / spot * 100:.1f}% "
                f"move to break even against a {em_pct:.1f}% one-sigma expected move."
            )
            idea.exit_plan = ("Take profit at +50-75% of premium; cut at -50%. "
                              "Close before the final trading hour on expiry day to avoid pin risk.")
            _finalise(idea, spot, vol, bias, rate)
            # Set after finalise so the quoted decay is the real computed theta.
            idea.risk_note = (
                f"Long premium decays every calendar day including weekends: about "
                f"${abs(idea.net_theta):.0f}/day against a ${idea.max_loss:.0f} position, "
                f"so a flat tape costs ~{abs(idea.net_theta) / max(idea.max_loss, 1) * 100:.0f}% "
                f"of the premium per day."
            )
            ideas.append(idea)

    # -- directional: debit vertical -----------------------------------------
    # Caps the payoff but halves the premium and the theta bleed. The workhorse.
    if bullish or bearish:
        side = chain.calls if bullish else chain.puts
        pair = _vertical(side, 0.50, 0.28, "call" if bullish else "put", aggressiveness)
        if pair:
            long_leg, short_leg = pair
            width = abs(long_leg.strike - short_leg.strike)
            debit = (long_leg.price - short_leg.price)
            if debit > 0.02 and width > 0:
                name = "bull_call_spread" if bullish else "bear_put_spread"
                idea = new(name, "Bull Call Spread" if bullish else "Bear Put Spread",
                           "bullish" if bullish else "bearish", [long_leg, short_leg])
                idea.max_loss = round(debit * MULTIPLIER, 2)
                idea.max_profit = round((width - debit) * MULTIPLIER, 2)
                idea.rationale = (
                    f"Directional with defined risk. Pays in full if {sig.symbol} closes "
                    f"{'above' if bullish else 'below'} {short_leg.strike:g} by {exp}, which is "
                    f"{abs(short_leg.strike - spot) / max(em, 1e-9):.2f}x the expected move away. "
                    f"Costs {debit / width * 100:.0f}% of the {width:g}-wide spread."
                )
                idea.risk_note = ("Max loss is the debit, taken if the underlying finishes at or "
                                  f"{'below' if bullish else 'above'} {long_leg.strike:g}.")
                idea.exit_plan = ("Target 50-70% of max profit rather than holding to expiry - "
                                  "the last of the value is the slowest and riskiest to collect.")
                ideas.append(_finalise(idea, spot, vol, bias, rate))

    # -- directional: credit vertical ----------------------------------------
    # Sells the move instead of buying it. Wants rich IV and only needs the
    # underlying to *not* go against you.
    if (bullish or bearish) and iv in ("rich", "fair") and conv >= 30:
        side = chain.puts if bullish else chain.calls
        short_c = _pick(side, 0.28)
        long_c = _pick(side, 0.14)
        if short_c and long_c and short_c.strike != long_c.strike:
            short_leg, long_leg = _leg(short_c, "sell", aggressiveness), _leg(long_c, "buy", aggressiveness)
            if short_leg and long_leg:
                width = abs(short_leg.strike - long_leg.strike)
                credit = short_leg.price - long_leg.price
                if credit > 0.02 and width > credit:
                    name = "bull_put_spread" if bullish else "bear_call_spread"
                    idea = new(name, "Bull Put Credit Spread" if bullish else "Bear Call Credit Spread",
                               "bullish" if bullish else "bearish", [short_leg, long_leg])
                    idea.max_profit = round(credit * MULTIPLIER, 2)
                    idea.max_loss = round((width - credit) * MULTIPLIER, 2)
                    idea.rationale = (
                        f"Sells premium into {iv} implied vol. Keeps the full credit as long as "
                        f"{sig.symbol} stays {'above' if bullish else 'below'} {short_leg.strike:g} "
                        f"({abs(short_leg.strike - spot) / spot * 100:.1f}% away, "
                        f"{abs(short_leg.strike - spot) / max(em, 1e-9):.2f}x expected move). "
                        f"Time decay works for you."
                    )
                    idea.risk_note = (
                        f"Risk/reward is inverted: risking ${idea.max_loss:.0f} to make "
                        f"${idea.max_profit:.0f}. A single loss undoes several wins, so position "
                        f"size is what makes or breaks this structure."
                    )
                    idea.exit_plan = ("Buy back at 50-60% of max profit. Close or roll if the short "
                                      "strike is tested - assignment risk rises fast near expiry.")
                    ideas.append(_finalise(idea, spot, vol, bias, rate))

    # -- neutral: iron condor -------------------------------------------------
    if assessment.regime in ("range", "mean_revert") and iv in ("rich", "fair") and abs(bias) < 0.45:
        put_short, put_long = _pick(chain.puts, 0.20), _pick(chain.puts, 0.10)
        call_short, call_long = _pick(chain.calls, 0.20), _pick(chain.calls, 0.10)
        if all([put_short, put_long, call_short, call_long]) and \
           put_short.strike != put_long.strike and call_short.strike != call_long.strike:
            legs = [_leg(put_short, "sell", aggressiveness), _leg(put_long, "buy", aggressiveness),
                    _leg(call_short, "sell", aggressiveness), _leg(call_long, "buy", aggressiveness)]
            if all(legs):
                credit = -_net_cost(legs) / MULTIPLIER
                put_width = abs(put_short.strike - put_long.strike)
                call_width = abs(call_long.strike - call_short.strike)
                width = max(put_width, call_width)
                if credit > 0.02 and width > credit:
                    idea = new("iron_condor", "Iron Condor", "neutral", legs)
                    idea.max_profit = round(credit * MULTIPLIER, 2)
                    idea.max_loss = round((width - credit) * MULTIPLIER, 2)
                    idea.rationale = (
                        f"Range regime with {iv} implied vol and no strong directional read "
                        f"(bias {bias:+.2f}). Profits if {sig.symbol} finishes between "
                        f"{put_short.strike:g} and {call_short.strike:g} - a "
                        f"{(call_short.strike - put_short.strike) / spot * 100:.1f}% wide window "
                        f"against a {em_pct:.1f}% expected move."
                    )
                    idea.risk_note = ("Four legs means four spreads to cross; do not leg in. "
                                      "Loss is capped but larger than the credit on either side.")
                    idea.exit_plan = ("Close at 50% of max credit. Manage the tested side early - "
                                      "condors lose most of their damage in the last two days.")
                    ideas.append(_finalise(idea, spot, vol, bias, rate))

    # -- volatility: long strangle -------------------------------------------
    # For a coiled name about to move, when you cannot call the direction.
    if assessment.regime == "vol_expansion" and iv in ("cheap", "fair") and abs(bias) < 0.5:
        call_c, put_c = _pick(chain.calls, 0.30), _pick(chain.puts, 0.30)
        if call_c and put_c:
            legs = [_leg(call_c, "buy", aggressiveness), _leg(put_c, "buy", aggressiveness)]
            if all(legs):
                debit = _net_cost(legs) / MULTIPLIER
                idea = new("long_strangle", "Long Strangle", "neutral", legs)
                idea.max_loss = round(debit * MULTIPLIER, 2)
                idea.max_profit = None
                idea.rationale = (
                    f"Volatility is compressed (Bollinger width in the "
                    f"{sig.bb_squeeze:.0f}th percentile) with volume picking up "
                    f"({sig.relative_volume:.2f}x). Buys the move without picking a side. "
                    f"Needs a move beyond {abs(call_c.strike + debit - spot) / spot * 100:.1f}% "
                    f"vs a {em_pct:.1f}% expected move."
                    if sig.bb_squeeze is not None and sig.relative_volume else
                    "Buys an expected volatility expansion without picking a side."
                )
                idea.risk_note = ("Pays for two options and needs a move bigger than both premiums "
                                  "combined. If the range holds, this is the fastest way to lose "
                                  "a full premium in three days.")
                idea.exit_plan = ("Exit on the first sharp expansion - take 40-60%. Do not hold "
                                  "a strangle into the last day of a quiet tape.")
                ideas.append(_finalise(idea, spot, vol, bias, rate))

    for idea in ideas:
        idea.score = round(rank_score(idea, assessment), 4)
    ideas.sort(key=lambda i: i.score, reverse=True)
    return ideas


def rank_score(idea: StrategyIdea, assessment: Assessment) -> float:
    """Rank ideas by conviction, structural quality and cost of the edge.

    Conviction dominates, but a structure that pays badly for the risk it takes
    is demoted even when the directional read is strong.
    """
    score = assessment.conviction / 100.0 * 0.55

    # Reward positive model EV relative to the capital at risk.
    risk = idea.max_loss or abs(idea.net_cost) or 1.0
    if idea.ev_model is not None and risk > 0:
        score += max(-0.25, min(0.25, idea.ev_model / risk * 0.5))

    # Reward probability of profit, mildly - high POP structures are usually
    # paying for it with a bad risk/reward, which the EV term already sees.
    if idea.prob_profit is not None:
        score += (idea.prob_profit - 0.5) * 0.12

    # Penalise structures whose risk-neutral EV is deeply negative: that is the
    # market charging a lot for the exposure, regardless of the model's view.
    if idea.ev_risk_neutral is not None and risk > 0:
        score += max(-0.20, min(0.0, idea.ev_risk_neutral / risk * 0.35))

    # Liquidity: widest leg spread eats real money on entry and exit.
    spreads = [((leg.ask - leg.bid) / ((leg.ask + leg.bid) / 2))
               for leg in idea.legs
               if leg.bid is not None and leg.ask and (leg.ask + leg.bid) > 0]
    if spreads:
        score -= min(0.25, max(spreads) * 0.5)

    return max(0.0, score)
