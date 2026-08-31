"""FastAPI application: REST API plus the single-page front end."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .backtest.engine import run_backtest
from .config import settings
from .db import connect, dumps, init_db, loads
from .engine import catalysts as cat
from .engine.scanner import Scanner
from .engine.strategies import payoff_curve, Leg
from .engine.universe import PRESETS, get_universe
from . import market_hours
from .paper import engine as paper
from .providers import store
from .providers.registry import MarketData, ProviderUnavailable, build_provider

# Per-symbol provider failures log at DEBUG: a vendor hiccup would otherwise
# print one scary line per symbol, when the circuit breaker already prints a
# single summary that says what is actually happening.
_VERBOSE = os.environ.get("MARKET_SCANNER_VERBOSE", "").strip() not in ("", "0")
logging.basicConfig(level=logging.DEBUG if _VERBOSE else logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("market_scanner")

STATIC_DIR = Path(__file__).parent / "static"

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    try:
        state["market"] = MarketData(build_provider())
    except ProviderUnavailable as exc:
        log.error("Cannot start: %s", exc)
        raise
    state["scanner"] = Scanner(state["market"])
    log.info("Market Scanner ready - provider=%s realtime=%s, %s",
             state["market"].name, state["market"].realtime,
             market_hours.session_label())
    try:
        yield
    finally:
        await state["market"].close()


app = FastAPI(title="Market Scanner", version="1.0.0", lifespan=lifespan)


def market() -> MarketData:
    return state["market"]


@app.exception_handler(paper.OrderRejected)
async def order_rejected_handler(request, exc: paper.OrderRejected):
    status = 404 if isinstance(exc, paper.SessionNotFound) else 400
    return JSONResponse(status_code=status, content={"detail": str(exc)})


# --------------------------------------------------------------------------
# Meta
# --------------------------------------------------------------------------
@app.get("/api/status")
async def status():
    md = market()
    return {
        "provider": md.name,
        "realtime": md.realtime,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "market": market_hours.describe(),
        "data": md.data_status(),
        "cache": store.stats(),
        "presets": list(PRESETS.keys()),
        "default_cash": settings.default_cash,
        "option_commission": settings.option_commission,
        "data_note": _data_note(md),
    }


def _data_note(md: MarketData) -> str:
    """One sentence on how much to trust what is on screen."""
    stale = md.data_status()
    if stale["stale_count"]:
        return (f"The {md.name} provider is not responding for "
                f"{stale['stale_count']} symbol(s), so the app is showing the most "
                f"recent real prices it already had (last updated "
                f"{stale['stale_age']}). Those rows are marked stale. Nothing here "
                f"is simulated.")
    if md.name == "yahoo":
        return ("Yahoo Finance: equity quotes are near-real-time and include pre- and "
                "post-market prices; option chains are typically delayed about 15 "
                "minutes. Implied vol is re-solved from the live midpoint rather than "
                "taken from the vendor's stale field.")
    if md.name == "tradier" and not md.realtime:
        return "Tradier sandbox: quotes are delayed. A brokerage token returns real-time data."
    return "Tradier real-time: live NBBO quotes with exchange-published greeks."


# --------------------------------------------------------------------------
# Market data passthrough
# --------------------------------------------------------------------------
@app.get("/api/quote/{symbol}")
async def quote(symbol: str):
    q = (await market().quotes([symbol])).get(symbol.upper())
    if not q:
        raise HTTPException(404, f"No quote for {symbol}")
    return q.to_dict()

@app.get("/api/history/{symbol}")
async def history(symbol: str, days: int = Query(180, ge=20, le=800)):
    bars = await market().history(symbol, days)
    if not len(bars):
        raise HTTPException(404, f"No history for {symbol}")
    return {"symbol": bars.symbol, "bars": [b.__dict__ for b in bars.bars]}


@app.get("/api/expirations/{symbol}")
async def expirations(symbol: str):
    exps = await market().expirations(symbol)
    today = datetime.now(timezone.utc).date()
    out = []
    for e in exps:
        try:
            out.append({"expiration": e, "dte": (date.fromisoformat(e) - today).days})
        except ValueError:
            continue
    return {"symbol": symbol.upper(), "expirations": out}


@app.get("/api/chain/{symbol}")
async def chain(symbol: str, expiration: str):
    c = await market().chain(symbol, expiration)
    if not c:
        raise HTTPException(404, f"No chain for {symbol} {expiration}")
    return {
        "underlying": c.underlying, "expiration": c.expiration,
        "underlying_price": c.underlying_price,
        "calls": [x.to_dict() for x in c.calls], "puts": [x.to_dict() for x in c.puts],
    }


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------
@app.get("/api/scan")
async def scan(
    preset: str = Query("core"),
    min_dte: int = Query(0, ge=0, le=60),
    max_dte: int = Query(3, ge=0, le=60),
    min_conviction: int = Query(0, ge=0, le=100),
    limit: int = Query(40, ge=1, le=200),
    include_news: bool = Query(True),
    save: bool = Query(False),
):
    if max_dte < min_dte:
        raise HTTPException(400, "max_dte must be greater than or equal to min_dte")
    result = await state["scanner"].scan(preset, min_dte, max_dte, min_conviction,
                                         limit, include_news)
    payload = result.to_dict()
    if save:
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO saved_scans (created_at, label, params, result) VALUES (?,?,?,?)",
                (result.generated_at, f"{preset} {min_dte}-{max_dte}DTE",
                 dumps({"preset": preset, "min_dte": min_dte, "max_dte": max_dte,
                        "min_conviction": min_conviction}), dumps(payload)),
            )
            payload["saved_scan_id"] = cur.lastrowid
    return payload


@app.get("/api/scan/saved")
async def saved_scans(limit: int = Query(50, ge=1, le=200)):
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, label, params FROM saved_scans "
            "ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{**dict(r), "params": loads(r["params"])} for r in rows]


@app.get("/api/scan/saved/{scan_id}")
async def saved_scan(scan_id: int):
    with connect() as conn:
        row = conn.execute("SELECT * FROM saved_scans WHERE id=?", (scan_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Saved scan not found")
    return {"id": row["id"], "created_at": row["created_at"], "label": row["label"],
            "params": loads(row["params"]), "result": loads(row["result"])}


@app.delete("/api/scan/saved/{scan_id}")
async def delete_saved_scan(scan_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM saved_scans WHERE id=?", (scan_id,))
    return {"deleted": scan_id}


@app.get("/api/market/brief")
async def market_brief():
    """Headlines, scheduled catalysts and a plain-language read of the tape."""
    md = market()
    from .engine.universe import BENCHMARKS
    quotes = await md.quotes(BENCHMARKS)
    try:
        raw = await md.news(BENCHMARKS[:3] + ["AAPL", "NVDA", "MSFT"], 40)
    except Exception as exc:                                # noqa: BLE001
        log.warning("news failed: %s", exc)
        raw = []
    headlines = cat.score_news(raw)
    today = datetime.now(timezone.utc).date()
    catalyst_list = cat.upcoming_catalysts(today, 10)
    advancers = sum(1 for q in quotes.values() if (q.change_pct or 0) > 0)
    breadth = {
        "advancers": advancers, "decliners": len(quotes) - advancers,
        "spy_change_pct": round(quotes["SPY"].change_pct, 2)
        if "SPY" in quotes and quotes["SPY"].change_pct is not None else None,
    }
    return {
        "indices": [q.to_dict() for q in quotes.values()],
        "headlines": [h.to_dict() for h in headlines[:25]],
        "catalysts": [c.to_dict() for c in catalyst_list],
        "narrative": cat.market_narrative(headlines, catalyst_list, breadth),
        "breadth": breadth,
        "market": market_hours.describe(),
        "data": md.data_status(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


class PayoffRequest(BaseModel):
    legs: list[dict]
    underlying_price: float


@app.post("/api/payoff")
async def payoff(req: PayoffRequest):
    """Expiry payoff curve for an arbitrary set of legs, for the UI chart."""
    legs = [Leg(action=l["action"], kind=l["kind"], strike=float(l["strike"]),
                expiration=l.get("expiration", ""), symbol=l.get("symbol", ""),
                quantity=int(l.get("quantity", 1)), price=float(l.get("price", 0)))
            for l in req.legs]
    return {"curve": payoff_curve(legs, req.underlying_price)}


# --------------------------------------------------------------------------
# Equities
# --------------------------------------------------------------------------
@app.get("/api/equities")
async def equities(preset: str = Query("core"), limit: int = Query(40, ge=1, le=200)):
    result = await state["scanner"].scan(preset, 0, 3, 0, limit, include_news=False)
    return {"equities": result.equities, "generated_at": result.generated_at,
            "provider": result.provider}


# --------------------------------------------------------------------------
# Paper trading
# --------------------------------------------------------------------------
class NewSessionRequest(BaseModel):
    name: str = Field(default="Paper session", max_length=120)
    starting_cash: float = Field(default=25_000.0, gt=0, le=100_000_000)
    notes: str = Field(default="", max_length=2000)


class LegModel(BaseModel):
    symbol: str
    side: str
    quantity: int = Field(gt=0, le=10_000)
    asset_type: str = "option"


class OrderRequest(BaseModel):
    legs: list[LegModel] = Field(min_length=1, max_length=8)
    strategy: str | None = None
    note: str | None = None
    order_type: str = "market"
    limit_price: float | None = None


@app.get("/api/paper/sessions")
async def list_paper_sessions():
    return paper.list_sessions()


@app.post("/api/paper/sessions")
async def new_paper_session(req: NewSessionRequest):
    return paper.create_session(req.name, req.starting_cash, req.notes)


@app.get("/api/paper/sessions/{session_id}")
async def paper_session(session_id: int, snapshot: bool = Query(False)):
    return await paper.portfolio(session_id, market(), snapshot=snapshot)


@app.patch("/api/paper/sessions/{session_id}")
async def patch_paper_session(session_id: int, name: str = Body(..., embed=True)):
    return paper.rename_session(session_id, name)


@app.post("/api/paper/sessions/{session_id}/close")
async def close_paper_session(session_id: int):
    return paper.close_session(session_id)


@app.delete("/api/paper/sessions/{session_id}")
async def delete_paper_session(session_id: int):
    paper.delete_session(session_id)
    return {"deleted": session_id}


@app.post("/api/paper/sessions/{session_id}/orders")
async def place_order(session_id: int, req: OrderRequest):
    legs = [paper.LegRequest(l.symbol, l.side, l.quantity, l.asset_type) for l in req.legs]
    return await paper.submit_order(session_id, legs, market(), strategy=req.strategy,
                                    note=req.note, order_type=req.order_type,
                                    limit_price=req.limit_price)


@app.get("/api/paper/sessions/{session_id}/orders")
async def orders(session_id: int, limit: int = Query(200, ge=1, le=1000)):
    return paper.order_history(session_id, limit)


@app.post("/api/paper/sessions/{session_id}/close-position")
async def close_position(session_id: int, symbol: str = Body(..., embed=True),
                         quantity: float | None = Body(None, embed=True)):
    return await paper.close_position(session_id, symbol, market(), quantity)


@app.post("/api/paper/sessions/{session_id}/close-group")
async def close_group(session_id: int, group_id: str = Body(..., embed=True)):
    return await paper.close_group(session_id, group_id, market())


@app.post("/api/paper/sessions/{session_id}/settle")
async def settle(session_id: int):
    return await paper.settle_expirations(session_id, market())


@app.get("/api/paper/sessions/{session_id}/performance")
async def performance(session_id: int):
    return paper.performance(session_id)


@app.get("/api/paper/sessions/{session_id}/curve")
async def curve(session_id: int):
    return paper.equity_curve(session_id)


# --------------------------------------------------------------------------
# Backtesting
# --------------------------------------------------------------------------
class BacktestRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=25)
    lookback_days: int = Field(default=400, ge=120, le=800)
    hold_days: int = Field(default=3, ge=1, le=15)
    min_conviction: int = Field(default=55, ge=0, le=100)
    dte: int = Field(default=3, ge=1, le=45)
    contracts: int = Field(default=1, ge=1, le=100)
    starting_cash: float = Field(default=25_000.0, gt=0, le=100_000_000)
    profit_target_pct: float = Field(default=60.0, gt=0, le=1000)
    stop_loss_pct: float = Field(default=50.0, gt=0, le=100)
    iv_premium: float = Field(default=1.15, ge=0.5, le=3.0)
    spread_pct: float = Field(default=0.04, ge=0.0, le=0.5)
    mode: str = "options"
    allowed_strategies: list[str] | None = None
    risk_per_trade_pct: float = Field(default=5.0, gt=0, le=100)
    label: str | None = None
    save: bool = True


@app.post("/api/backtest")
async def backtest(req: BacktestRequest):
    if req.mode not in ("options", "equity"):
        raise HTTPException(400, "mode must be 'options' or 'equity'")
    symbols = [s.strip().upper() for s in req.symbols if s.strip()]
    result = await run_backtest(
        market(), symbols, lookback_days=req.lookback_days, hold_days=req.hold_days,
        min_conviction=req.min_conviction, dte=req.dte, contracts=req.contracts,
        starting_cash=req.starting_cash, profit_target_pct=req.profit_target_pct,
        stop_loss_pct=req.stop_loss_pct, iv_premium=req.iv_premium,
        spread_pct=req.spread_pct, mode=req.mode,
        allowed_strategies=req.allowed_strategies,
        risk_per_trade_pct=req.risk_per_trade_pct,
    )
    payload = result.to_dict()
    if req.save:
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO backtests (created_at, label, params, result) VALUES (?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(),
                 req.label or f"{req.mode} {'/'.join(symbols[:4])}",
                 dumps(result.params), dumps(payload)),
            )
            payload["backtest_id"] = cur.lastrowid
    return payload


@app.get("/api/backtest/saved")
async def saved_backtests(limit: int = Query(50, ge=1, le=200)):
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, label, params FROM backtests "
            "ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{**dict(r), "params": loads(r["params"])} for r in rows]


@app.get("/api/backtest/saved/{backtest_id}")
async def saved_backtest(backtest_id: int):
    with connect() as conn:
        row = conn.execute("SELECT * FROM backtests WHERE id=?", (backtest_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Backtest not found")
    return {"id": row["id"], "created_at": row["created_at"], "label": row["label"],
            "params": loads(row["params"]), "result": loads(row["result"])}


@app.delete("/api/backtest/saved/{backtest_id}")
async def delete_backtest(backtest_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM backtests WHERE id=?", (backtest_id,))
    return {"deleted": backtest_id}


# --------------------------------------------------------------------------
# Front end
# --------------------------------------------------------------------------
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(500, "Front end not built")
    return FileResponse(index_file)
