"""Paper-trading engine.

Design goals, in priority order:

  1. Fills are priced against the live book, not against mid. Buying lifts the
     offer side, selling hits the bid side. Paper accounts that fill everything
     at mid teach you a strategy works when it does not.
  2. Short options consume buying power. A defined-risk spread reserves its max
     loss; an uncovered short reserves a Reg-T style requirement. Without this
     you can "sell" unlimited premium and the equity curve is fiction.
  3. Every order, fill and mark is persisted, so the session can be reopened
     and reviewed later.
  4. Expiry is handled explicitly: contracts settle to intrinsic value on their
     expiration date rather than silently vanishing.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from ..config import settings
from ..db import connect, dumps
from ..providers.base import OptionContract, parse_occ
from ..providers.registry import MarketData

log = logging.getLogger(__name__)

OPTION_MULTIPLIER = 100.0


class OrderRejected(Exception):
    """Raised when an order cannot be accepted (funds, margin, bad symbol)."""


@dataclass
class LegRequest:
    symbol: str                 # OCC contract symbol, or a plain ticker
    side: str                   # buy | sell
    quantity: int               # contracts (options) or shares (equity)
    asset_type: str = "option"  # option | equity


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
def create_session(name: str, starting_cash: float, notes: str = "") -> dict:
    if starting_cash <= 0:
        raise OrderRejected("Starting cash must be greater than zero.")
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (name, starting_cash, cash, status, created_at, notes) "
            "VALUES (?,?,?,'active',?,?)",
            (name or "Paper session", float(starting_cash), float(starting_cash), _now(), notes),
        )
        session_id = cur.lastrowid
        conn.execute(
            "INSERT INTO snapshots (session_id, taken_at, cash, positions_value, total_equity) "
            "VALUES (?,?,?,0,?)",
            (session_id, _now(), float(starting_cash), float(starting_cash)),
        )
    return get_session(session_id)


def get_session(session_id: int) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        raise OrderRejected(f"Session {session_id} not found.")
    return dict(row)


def list_sessions() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT s.*, "
            "  (SELECT COUNT(*) FROM orders o WHERE o.session_id=s.id AND o.status='filled') AS order_count,"
            "  (SELECT COUNT(*) FROM positions p WHERE p.session_id=s.id) AS position_count "
            "FROM sessions s ORDER BY s.created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def close_session(session_id: int) -> dict:
    with connect() as conn:
        conn.execute("UPDATE sessions SET status='closed', closed_at=? WHERE id=?",
                     (_now(), session_id))
    return get_session(session_id)


def delete_session(session_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def rename_session(session_id: int, name: str) -> dict:
    with connect() as conn:
        conn.execute("UPDATE sessions SET name=? WHERE id=?", (name, session_id))
    return get_session(session_id)


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------
class Pricer:
    """Resolves a live tradeable price for equities and option contracts."""

    def __init__(self, market: MarketData, aggressiveness: float = 0.35):
        self.market = market
        self.aggressiveness = aggressiveness

    async def option_contract(self, occ: str) -> OptionContract | None:
        info = parse_occ(occ)
        if not info:
            return None
        chain = await self.market.chain(info["underlying"], info["expiration"])
        if not chain:
            return None
        pool = chain.calls if info["kind"] == "call" else chain.puts
        for c in pool:
            if abs(c.strike - info["strike"]) < 1e-6:
                return c
        return None

    async def underlying_price(self, symbol: str) -> float | None:
        q = (await self.market.quotes([symbol])).get(symbol.upper())
        return q.last if q else None

    async def fill_price(self, symbol: str, side: str, asset_type: str) -> tuple[float, dict]:
        """Price an order against the live book, crossing part of the spread."""
        if asset_type == "equity":
            q = (await self.market.quotes([symbol])).get(symbol.upper())
            if not q:
                raise OrderRejected(f"No quote available for {symbol}.")
            mid = q.mid
            half = ((q.ask - q.bid) / 2.0) if (q.bid and q.ask and q.ask > q.bid) else mid * 0.0002
            price = mid + half * self.aggressiveness * (1 if side == "buy" else -1)
            return max(0.01, round(price, 2)), {"bid": q.bid, "ask": q.ask, "mid": round(mid, 4)}

        contract = await self.option_contract(symbol)
        if contract is None:
            raise OrderRejected(f"No option quote available for {symbol}.")
        mid = contract.mid
        if mid is None or mid <= 0:
            raise OrderRejected(f"{symbol} has no two-sided market.")
        half = (contract.spread or 0.0) / 2.0
        price = mid + half * self.aggressiveness * (1 if side == "buy" else -1)
        return max(0.01, round(price, 2)), {
            "bid": contract.bid, "ask": contract.ask, "mid": round(mid, 4),
            "iv": contract.implied_volatility, "delta": contract.delta,
        }

    async def mark(self, symbol: str, asset_type: str, quantity: float) -> float | None:
        """Mark-to-market price: the side you would have to trade to exit."""
        if asset_type == "equity":
            q = (await self.market.quotes([symbol])).get(symbol.upper())
            if not q:
                return None
            if quantity > 0:
                return q.bid or q.last
            return q.ask or q.last
        contract = await self.option_contract(symbol)
        if contract is None:
            return None
        if quantity > 0:
            return contract.bid if contract.bid is not None else contract.mid
        return contract.ask if contract.ask is not None else contract.mid


# --------------------------------------------------------------------------
# Margin
# --------------------------------------------------------------------------
def _reg_t_short_option(strike: float, underlying_price: float, kind: str,
                        premium: float) -> float:
    """Reg-T style requirement for one uncovered short contract, in dollars.

    The greater of:
      * 20% of underlying value, less any out-of-the-money amount, plus premium
      * 10% of strike (calls) or 10% of underlying (puts), plus premium
    Floored at $250/contract, which is the conventional minimum.
    """
    otm = max(strike - underlying_price, 0.0) if kind == "call" else max(underlying_price - strike, 0.0)
    a = (0.20 * underlying_price - otm + premium) * OPTION_MULTIPLIER
    b = ((0.10 * strike if kind == "call" else 0.10 * underlying_price) + premium) * OPTION_MULTIPLIER
    return max(a, b, 250.0)


def cover_margin(kind: str, short_strike: float, long_strike: float) -> float:
    """Margin per contract for a short option covered by a long of the same kind.

    A long option at *any* strike caps the loss on a short of the same type and
    expiry - the question is only how much is capped:

      * Debit spread (the long is nearer the money): the whole cost was paid in
        cash up front, so nothing further is reserved.
      * Credit spread (the short is nearer the money): the reserve is the strike
        width, which is the worst case before the credit received.
    """
    if kind == "call":
        return max(0.0, long_strike - short_strike) * OPTION_MULTIPLIER
    return max(0.0, short_strike - long_strike) * OPTION_MULTIPLIER


async def required_margin(legs: list[dict], pricer: Pricer) -> float:
    """Buying power consumed by a multi-leg order.

    Short legs paired with a long leg of the same type and expiry are treated as
    a vertical spread and require the spread width; anything left uncovered
    falls back to the Reg-T approximation.
    """
    shorts = [l for l in legs if l["side"] == "sell" and l["asset_type"] == "option"]
    longs = [l for l in legs if l["side"] == "buy" and l["asset_type"] == "option"]
    if not shorts:
        return 0.0

    remaining_longs = [dict(l) for l in longs]
    total = 0.0

    for short in shorts:
        s_info = parse_occ(short["symbol"])
        if not s_info:
            continue
        qty = abs(short["quantity"])

        # Any long of the same underlying, kind and expiry covers this short.
        # Use the cheapest cover first so a spread is never over-charged.
        candidates = []
        for cand in remaining_longs:
            c_info = parse_occ(cand["symbol"])
            if not c_info or cand["quantity"] <= 0:
                continue
            if (c_info["kind"] == s_info["kind"]
                    and c_info["expiration"] == s_info["expiration"]
                    and c_info["underlying"] == s_info["underlying"]):
                candidates.append(
                    (cover_margin(s_info["kind"], s_info["strike"], c_info["strike"]), cand)
                )
        candidates.sort(key=lambda pair: pair[0])

        for per_contract, cover in candidates:
            if qty <= 0:
                break
            covered = min(qty, cover["quantity"])
            if covered <= 0:
                continue
            total += per_contract * covered
            cover["quantity"] -= covered
            qty -= covered

        if qty > 0:
            spot = await pricer.underlying_price(s_info["underlying"]) or s_info["strike"]
            premium = short.get("fill_price") or 0.0
            total += _reg_t_short_option(s_info["strike"], spot, s_info["kind"], premium) * qty

    return round(total, 2)


def reserved_margin(session_id: int) -> float:
    """Margin currently tied up by open short option positions."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE session_id=? AND quantity < 0", (session_id,)
        ).fetchall()
    if not rows:
        return 0.0
    # Reconstruct spread coverage from the open book rather than per order, so
    # closing one leg correctly releases or increases the requirement.
    with connect() as conn:
        longs = conn.execute(
            "SELECT * FROM positions WHERE session_id=? AND quantity > 0 AND asset_type='option'",
            (session_id,),
        ).fetchall()
    long_pool = [{"strike": r["strike"], "kind": r["kind"], "expiration": r["expiration"],
                  "underlying": r["underlying"], "quantity": r["quantity"]} for r in longs]

    total = 0.0
    for r in rows:
        if r["asset_type"] != "option":
            continue
        qty = abs(r["quantity"])
        candidates = [
            (cover_margin(r["kind"], r["strike"], cand["strike"]), cand)
            for cand in long_pool
            if cand["quantity"] > 0 and cand["kind"] == r["kind"]
            and cand["expiration"] == r["expiration"] and cand["underlying"] == r["underlying"]
        ]
        candidates.sort(key=lambda pair: pair[0])
        for per_contract, cand in candidates:
            if qty <= 0:
                break
            covered = min(qty, cand["quantity"])
            if covered <= 0:
                continue
            total += per_contract * covered
            cand["quantity"] -= covered
            qty -= covered
        if qty > 0:
            # Uncovered short still on the book: charge the Reg-T floor using
            # the entry strike as a stand-in for spot (marks refresh this).
            total += _reg_t_short_option(r["strike"], r["strike"], r["kind"],
                                         abs(r["avg_price"])) * qty
    return round(total, 2)


# --------------------------------------------------------------------------
# Order execution
# --------------------------------------------------------------------------
def _commission(asset_type: str, quantity: float) -> float:
    if asset_type == "option":
        return round(settings.option_commission * abs(quantity), 2)
    return round(settings.equity_commission, 2)


def _apply_fill(conn, session_id: int, leg: dict) -> float:
    """Update the position book for one fill. Returns realized P&L.

    Handles the four transitions: opening, adding to a position, reducing it,
    and flipping through zero to the other side.
    """
    symbol = leg["symbol"]
    signed = leg["quantity"] if leg["side"] == "buy" else -leg["quantity"]
    price = leg["fill_price"]
    multiplier = OPTION_MULTIPLIER if leg["asset_type"] == "option" else 1.0

    row = conn.execute(
        "SELECT * FROM positions WHERE session_id=? AND symbol=?", (session_id, symbol)
    ).fetchone()

    if row is None:
        info = parse_occ(symbol) if leg["asset_type"] == "option" else None
        conn.execute(
            "INSERT INTO positions (session_id, symbol, asset_type, quantity, avg_price, "
            "multiplier, underlying, expiration, strike, kind, group_id, strategy, opened_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, symbol, leg["asset_type"], signed, price, multiplier,
             (info or {}).get("underlying", symbol.upper()), (info or {}).get("expiration"),
             (info or {}).get("strike"), (info or {}).get("kind"),
             leg.get("group_id"), leg.get("strategy"), _now()),
        )
        return 0.0

    old_qty = row["quantity"]
    old_avg = row["avg_price"]
    new_qty = old_qty + signed
    realized = 0.0

    if old_qty * signed > 0:
        # Adding to the same side: blend the cost basis.
        new_avg = (old_avg * abs(old_qty) + price * abs(signed)) / abs(new_qty)
    elif abs(signed) <= abs(old_qty):
        # Reducing (or exactly closing): realise P&L on the closed quantity.
        closed = abs(signed)
        realized = (price - old_avg) * closed * multiplier * (1 if old_qty > 0 else -1)
        new_avg = old_avg
    else:
        # Flipping through zero: realise the old position, rebase on the rest.
        closed = abs(old_qty)
        realized = (price - old_avg) * closed * multiplier * (1 if old_qty > 0 else -1)
        new_avg = price

    if abs(new_qty) < 1e-9:
        conn.execute("DELETE FROM positions WHERE id=?", (row["id"],))
    else:
        conn.execute("UPDATE positions SET quantity=?, avg_price=? WHERE id=?",
                     (new_qty, new_avg, row["id"]))
    return round(realized, 2)


async def submit_order(session_id: int, legs: list[LegRequest], market: MarketData,
                       strategy: str | None = None, note: str | None = None,
                       order_type: str = "market", limit_price: float | None = None,
                       aggressiveness: float = 0.35) -> dict:
    """Price, risk-check and fill a single or multi-leg order atomically.

    All legs fill or none do: a partially-filled spread is a different position
    with a different risk profile, and silently handing you one is how paper
    trading stops resembling the real thing.
    """
    session = get_session(session_id)
    if session["status"] != "active":
        raise OrderRejected("This session is closed. Start a new session to keep trading.")
    if not legs:
        raise OrderRejected("An order needs at least one leg.")

    pricer = Pricer(market, aggressiveness)
    group_id = uuid.uuid4().hex[:12]
    priced: list[dict] = []

    for leg in legs:
        if leg.quantity <= 0:
            raise OrderRejected(f"Quantity for {leg.symbol} must be positive.")
        if leg.side not in ("buy", "sell"):
            raise OrderRejected(f"Unknown side '{leg.side}'.")
        fill, book = await pricer.fill_price(leg.symbol, leg.side, leg.asset_type)
        priced.append({
            "symbol": leg.symbol.upper(), "side": leg.side, "quantity": leg.quantity,
            "asset_type": leg.asset_type, "fill_price": fill, "book": book,
            "group_id": group_id, "strategy": strategy,
        })

    multiplier_of = lambda a: OPTION_MULTIPLIER if a == "option" else 1.0
    net_debit = sum(
        (1 if l["side"] == "buy" else -1) * l["fill_price"] * l["quantity"] * multiplier_of(l["asset_type"])
        for l in priced
    )

    # A limit on a multi-leg order applies to the net debit/credit of the package.
    if order_type == "limit" and limit_price is not None:
        limit_total = limit_price * OPTION_MULTIPLIER if any(
            l["asset_type"] == "option" for l in priced) else limit_price
        if net_debit > 0 and net_debit > limit_total + 1e-6:
            raise OrderRejected(
                f"Limit not marketable: net debit ${net_debit:.2f} exceeds limit ${limit_total:.2f}."
            )
        if net_debit < 0 and abs(net_debit) < abs(limit_total) - 1e-6:
            raise OrderRejected(
                f"Limit not marketable: net credit ${abs(net_debit):.2f} below limit ${abs(limit_total):.2f}."
            )

    commission = sum(_commission(l["asset_type"], l["quantity"]) for l in priced)

    # Only *new* short exposure consumes margin; closing legs release it.
    opening_legs = await _opening_legs(session_id, priced)
    margin = await required_margin(opening_legs, pricer)

    cash_after = session["cash"] - net_debit - commission
    available = cash_after - reserved_margin(session_id) - margin
    if cash_after < 0 and net_debit > 0:
        raise OrderRejected(
            f"Insufficient cash: order costs ${net_debit + commission:,.2f} but "
            f"${session['cash']:,.2f} is available."
        )
    if available < 0:
        raise OrderRejected(
            f"Insufficient buying power: this order reserves ${margin:,.2f} of margin, "
            f"leaving ${available:,.2f}. Reduce size or close an existing short."
        )

    filled_at = _now()
    total_realized = 0.0
    order_ids: list[int] = []

    with connect() as conn:
        for leg in priced:
            realized = _apply_fill(conn, session_id, leg)
            total_realized += realized
            cur = conn.execute(
                "INSERT INTO orders (session_id, group_id, symbol, asset_type, side, quantity, "
                "order_type, limit_price, status, fill_price, commission, realized_pnl, "
                "strategy, note, created_at, filled_at) "
                "VALUES (?,?,?,?,?,?,?,?, 'filled', ?,?,?,?,?,?,?)",
                (session_id, group_id, leg["symbol"], leg["asset_type"], leg["side"],
                 leg["quantity"], order_type, limit_price, leg["fill_price"],
                 _commission(leg["asset_type"], leg["quantity"]), realized,
                 strategy, note, filled_at, filled_at),
            )
            order_ids.append(cur.lastrowid)

        conn.execute("UPDATE sessions SET cash=? WHERE id=?",
                     (round(cash_after, 2), session_id))

    return {
        "group_id": group_id, "order_ids": order_ids, "status": "filled",
        "net_debit": round(net_debit, 2), "commission": round(commission, 2),
        "realized_pnl": round(total_realized, 2), "margin_reserved": margin,
        "cash_after": round(cash_after, 2), "filled_at": filled_at,
        "legs": [{k: v for k, v in l.items() if k != "group_id"} for l in priced],
    }


async def _opening_legs(session_id: int, priced: list[dict]) -> list[dict]:
    """The portion of each leg that opens new exposure rather than closing.

    A sell that closes an existing long is not new short exposure and must not
    be charged margin for it.
    """
    with connect() as conn:
        rows = conn.execute("SELECT symbol, quantity FROM positions WHERE session_id=?",
                            (session_id,)).fetchall()
    held = {r["symbol"]: r["quantity"] for r in rows}

    out = []
    for leg in priced:
        signed = leg["quantity"] if leg["side"] == "buy" else -leg["quantity"]
        existing = held.get(leg["symbol"], 0.0)
        if existing * signed >= 0:
            opening = abs(signed)              # adding to the same side
        else:
            opening = max(0.0, abs(signed) - abs(existing))   # net of what it closes
        held[leg["symbol"]] = existing + signed
        if opening > 0:
            out.append({**leg, "quantity": opening})
    return out


async def close_position(session_id: int, symbol: str, market: MarketData,
                         quantity: float | None = None) -> dict:
    """Flatten a position (or part of it) at the live exit price."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM positions WHERE session_id=? AND symbol=?",
                           (session_id, symbol.upper())).fetchone()
    if row is None:
        raise OrderRejected(f"No open position in {symbol}.")

    qty = abs(row["quantity"]) if quantity is None else min(abs(quantity), abs(row["quantity"]))
    if qty <= 0:
        raise OrderRejected("Nothing to close.")
    side = "sell" if row["quantity"] > 0 else "buy"
    leg = LegRequest(symbol=row["symbol"], side=side, quantity=qty,
                     asset_type=row["asset_type"])
    return await submit_order(session_id, [leg], market,
                              strategy=row["strategy"], note="close")


async def close_group(session_id: int, group_id: str, market: MarketData) -> dict:
    """Flatten every leg opened under one spread order, in one package."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE session_id=? AND group_id=?", (session_id, group_id)
        ).fetchall()
    if not rows:
        raise OrderRejected("No open positions for that order group.")
    legs = [LegRequest(symbol=r["symbol"], side="sell" if r["quantity"] > 0 else "buy",
                       quantity=abs(r["quantity"]), asset_type=r["asset_type"]) for r in rows]
    return await submit_order(session_id, legs, market,
                              strategy=rows[0]["strategy"], note="close group")


# --------------------------------------------------------------------------
# Portfolio marking and history
# --------------------------------------------------------------------------
async def portfolio(session_id: int, market: MarketData, snapshot: bool = False) -> dict:
    """Mark every position to the live book and return the full account state."""
    session = get_session(session_id)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM positions WHERE session_id=? ORDER BY opened_at",
                            (session_id,)).fetchall()
        realized_total = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) AS r, COALESCE(SUM(commission),0) AS c "
            "FROM orders WHERE session_id=? AND status='filled'", (session_id,)
        ).fetchone()

    pricer = Pricer(market)
    positions: list[dict] = []
    positions_value = 0.0

    for row in rows:
        mark = await pricer.mark(row["symbol"], row["asset_type"], row["quantity"])
        if mark is None:
            mark = row["avg_price"]
            stale = True
        else:
            stale = False
        qty, mult = row["quantity"], row["multiplier"]
        value = mark * qty * mult
        cost = row["avg_price"] * qty * mult
        unrealized = value - cost
        positions_value += value

        dte = None
        if row["expiration"]:
            try:
                dte = (date.fromisoformat(row["expiration"]) - datetime.now(timezone.utc).date()).days
            except ValueError:
                dte = None

        positions.append({
            **dict(row),
            "mark": round(mark, 4), "market_value": round(value, 2),
            "cost_basis": round(cost, 2), "unrealized_pnl": round(unrealized, 2),
            "unrealized_pct": round(unrealized / abs(cost) * 100, 2) if cost else None,
            "dte": dte, "stale_mark": stale,
        })

    margin = reserved_margin(session_id)
    total_equity = session["cash"] + positions_value
    unrealized_total = sum(p["unrealized_pnl"] for p in positions)

    if snapshot:
        with connect() as conn:
            conn.execute(
                "INSERT INTO snapshots (session_id, taken_at, cash, positions_value, total_equity) "
                "VALUES (?,?,?,?,?)",
                (session_id, _now(), round(session["cash"], 2), round(positions_value, 2),
                 round(total_equity, 2)),
            )

    return {
        "session": session,
        "cash": round(session["cash"], 2),
        "positions_value": round(positions_value, 2),
        "total_equity": round(total_equity, 2),
        "starting_cash": session["starting_cash"],
        "total_pnl": round(total_equity - session["starting_cash"], 2),
        "total_pnl_pct": round((total_equity / session["starting_cash"] - 1) * 100, 2)
        if session["starting_cash"] else 0.0,
        "realized_pnl": round(realized_total["r"], 2),
        "unrealized_pnl": round(unrealized_total, 2),
        "commissions_paid": round(realized_total["c"], 2),
        "margin_reserved": margin,
        "buying_power": round(session["cash"] - margin, 2),
        "positions": positions,
    }


def order_history(session_id: int, limit: int = 200) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE session_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def equity_curve(session_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT taken_at, cash, positions_value, total_equity FROM snapshots "
            "WHERE session_id=? ORDER BY taken_at", (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def round_trips(session_id: int) -> list[dict]:
    """Completed trades grouped by contract, for win-rate and hold-time stats."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE session_id=? AND status='filled' "
            "ORDER BY created_at, id", (session_id,)
        ).fetchall()

    open_lots: dict[str, list[dict]] = {}
    trips: list[dict] = []
    for row in rows:
        symbol = row["symbol"]
        signed = row["quantity"] if row["side"] == "buy" else -row["quantity"]
        lots = open_lots.setdefault(symbol, [])
        remaining = abs(signed)

        while remaining > 0 and lots and lots[0]["qty"] * signed < 0:
            lot = lots[0]
            matched = min(remaining, abs(lot["qty"]))
            mult = OPTION_MULTIPLIER if row["asset_type"] == "option" else 1.0
            direction = 1 if lot["qty"] > 0 else -1
            pnl = (row["fill_price"] - lot["price"]) * matched * mult * direction
            trips.append({
                "symbol": symbol, "asset_type": row["asset_type"],
                "strategy": lot.get("strategy") or row["strategy"],
                "opened_at": lot["at"], "closed_at": row["filled_at"],
                "quantity": matched, "entry": lot["price"], "exit": row["fill_price"],
                "pnl": round(pnl, 2), "direction": "long" if direction > 0 else "short",
            })
            lot["qty"] -= direction * matched
            remaining -= matched
            if abs(lot["qty"]) < 1e-9:
                lots.pop(0)

        if remaining > 0:
            lots.append({"qty": (1 if signed > 0 else -1) * remaining,
                         "price": row["fill_price"], "at": row["filled_at"],
                         "strategy": row["strategy"]})

    return trips


def performance(session_id: int) -> dict:
    """Win rate, average win/loss and profit factor over closed round trips."""
    trips = round_trips(session_id)
    if not trips:
        return {"trades": 0, "win_rate": None, "avg_win": None, "avg_loss": None,
                "profit_factor": None, "best": None, "worst": None, "round_trips": []}
    wins = [t["pnl"] for t in trips if t["pnl"] > 0]
    losses = [t["pnl"] for t in trips if t["pnl"] < 0]
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    return {
        "trades": len(trips),
        "win_rate": round(len(wins) / len(trips) * 100, 1),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "best": round(max(t["pnl"] for t in trips), 2),
        "worst": round(min(t["pnl"] for t in trips), 2),
        "net_pnl": round(sum(t["pnl"] for t in trips), 2),
        "round_trips": sorted(trips, key=lambda t: t["closed_at"] or "", reverse=True)[:100],
    }


async def settle_expirations(session_id: int, market: MarketData,
                             as_of: date | None = None) -> dict:
    """Settle option positions at or past expiry to intrinsic value.

    Options do not disappear at expiry - they either expire worthless or settle
    in the money. Modelling that explicitly is what stops a paper account from
    quietly carrying dead contracts at their entry price forever.
    """
    as_of = as_of or datetime.now(timezone.utc).date()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE session_id=? AND asset_type='option' "
            "AND expiration IS NOT NULL", (session_id,)
        ).fetchall()

    expired = [r for r in rows if r["expiration"] and date.fromisoformat(r["expiration"]) <= as_of]
    if not expired:
        return {"settled": 0, "details": []}

    pricer = Pricer(market)
    details = []
    total_cash = 0.0

    for row in expired:
        spot = await pricer.underlying_price(row["underlying"])
        if spot is None:
            continue
        intrinsic = max(spot - row["strike"], 0.0) if row["kind"] == "call" \
            else max(row["strike"] - spot, 0.0)
        qty, mult = row["quantity"], row["multiplier"]
        # Settling to intrinsic: long positions receive it, shorts pay it.
        proceeds = intrinsic * qty * mult
        realized = (intrinsic - row["avg_price"]) * abs(qty) * mult * (1 if qty > 0 else -1)
        total_cash += proceeds

        with connect() as conn:
            conn.execute(
                "INSERT INTO orders (session_id, group_id, symbol, asset_type, side, quantity, "
                "order_type, status, fill_price, commission, realized_pnl, strategy, note, "
                "created_at, filled_at) VALUES (?,?,?,?,?,?, 'settlement','filled', ?,0,?,?,?,?,?)",
                (session_id, row["group_id"], row["symbol"], "option",
                 "sell" if qty > 0 else "buy", abs(qty), round(intrinsic, 4), round(realized, 2),
                 row["strategy"],
                 f"Expired {'in the money' if intrinsic > 0 else 'worthless'} "
                 f"with {row['underlying']} at {spot:.2f}", _now(), _now()),
            )
            conn.execute("DELETE FROM positions WHERE id=?", (row["id"],))

        details.append({
            "symbol": row["symbol"], "quantity": qty, "underlying_price": round(spot, 2),
            "intrinsic": round(intrinsic, 4), "cash_effect": round(proceeds, 2),
            "realized_pnl": round(realized, 2),
            "outcome": "in the money" if intrinsic > 0 else "expired worthless",
        })

    if details:
        session = get_session(session_id)
        with connect() as conn:
            conn.execute("UPDATE sessions SET cash=? WHERE id=?",
                         (round(session["cash"] + total_cash, 2), session_id))

    return {"settled": len(details), "cash_effect": round(total_cash, 2), "details": details}
