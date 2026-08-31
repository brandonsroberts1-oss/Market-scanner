"""Scan orchestration: fetch, measure, score, construct, rank.

The whole pass runs concurrently with a semaphore so a 60-name universe does
not open 200 simultaneous sockets at a data vendor that will rate-limit for it.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone

from ..config import settings
from ..providers.registry import MarketData
from . import catalysts as cat
from .conviction import Assessment, assess
from .equities import EquityIdea, rank_equity
from .signals import Signals, build_signals
from .strategies import StrategyIdea, build_ideas
from .universe import BENCHMARKS, get_universe

log = logging.getLogger(__name__)

MAX_CONCURRENCY = 8


@dataclass
class SymbolResult:
    symbol: str
    signals: Signals
    assessment: Assessment
    ideas: list[StrategyIdea] = field(default_factory=list)
    equity: EquityIdea | None = None
    error: str | None = None


@dataclass
class ScanResult:
    generated_at: str
    provider: str
    realtime: bool
    market_session: str
    data_status: dict
    universe: list[str]
    min_dte: int
    max_dte: int
    ideas: list[dict] = field(default_factory=list)
    equities: list[dict] = field(default_factory=list)
    headlines: list[dict] = field(default_factory=list)
    catalysts: list[dict] = field(default_factory=list)
    narrative: str = ""
    breadth: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _pick_expirations(expirations: list[str], min_dte: int, max_dte: int,
                      today: date | None = None) -> list[str]:
    """Expirations whose DTE falls in the requested window."""
    today = today or datetime.now(timezone.utc).date()
    out = []
    for exp in expirations:
        try:
            dte = (date.fromisoformat(exp) - today).days
        except ValueError:
            continue
        if min_dte <= dte <= max_dte:
            out.append((dte, exp))
    out.sort()
    return [exp for _, exp in out]


class Scanner:
    def __init__(self, market: MarketData):
        self.market = market

    async def scan(self, preset: str = "core", min_dte: int = 0, max_dte: int = 3,
                   min_conviction: int = 0, limit: int = 40,
                   include_news: bool = True) -> ScanResult:
        started = datetime.now(timezone.utc)
        symbols = get_universe(preset)
        today = started.date()
        # Staleness is reported per scan, not accumulated across the session.
        self.market.reset_status()

        # SPY history is needed by every symbol for beta and relative strength.
        spy_bars = await self.market.history("SPY", 180)
        spy_closes = spy_bars.closes

        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        async def run(symbol: str) -> SymbolResult:
            async with sem:
                return await self._scan_symbol(symbol, spy_closes, min_dte, max_dte, today)

        results = await asyncio.gather(*(run(s) for s in symbols), return_exceptions=True)

        errors: list[str] = []
        symbol_results: list[SymbolResult] = []
        for symbol, res in zip(symbols, results):
            if isinstance(res, Exception):
                log.warning("scan failed for %s: %s", symbol, res)
                errors.append(f"{symbol}: {res}")
            elif res.error:
                errors.append(f"{symbol}: {res.error}")
                symbol_results.append(res)
            else:
                symbol_results.append(res)

        # -- news and catalysts ---------------------------------------------
        headlines: list[cat.ScoredHeadline] = []
        if include_news:
            top_symbols = [r.symbol for r in sorted(
                symbol_results, key=lambda r: r.assessment.conviction, reverse=True)][:10]
            try:
                raw = await self.market.news(BENCHMARKS[:2] + top_symbols, 40)
                headlines = cat.score_news(raw)
            except Exception as exc:                       # noqa: BLE001
                log.warning("news fetch failed: %s", exc)
                errors.append(f"news: {exc}")

        catalyst_list = cat.upcoming_catalysts(today, 10)
        breadth = await self._breadth(symbol_results)
        narrative = cat.market_narrative(headlines, catalyst_list, breadth)

        # -- collect ideas ---------------------------------------------------
        ideas: list[dict] = []
        for r in symbol_results:
            sentiment = cat.symbol_sentiment(headlines, r.symbol)
            for idea in r.ideas:
                if idea.conviction < min_conviction:
                    continue
                d = idea.to_dict()
                d["regime"] = r.assessment.regime
                d["iv_regime"] = r.assessment.iv_regime
                d["bias"] = r.assessment.bias
                d["agreement"] = r.assessment.agreement
                d["quality"] = r.assessment.quality
                d["warnings"] = r.assessment.warnings
                d["factors"] = [f.to_dict() for f in r.assessment.factors]
                d["news_sentiment"] = sentiment
                d["stale"] = r.symbol.upper() in self.market.stale_symbols
                d["as_of"] = self.market.stale_symbols.get(r.symbol.upper())
                ideas.append(d)
        ideas.sort(key=lambda d: d["score"], reverse=True)

        equities = [r.equity.to_dict() for r in symbol_results if r.equity]
        equities.sort(key=lambda d: d["score"], reverse=True)

        from .. import market_hours
        return ScanResult(
            generated_at=started.isoformat(), provider=self.market.name,
            realtime=self.market.realtime,
            market_session=market_hours.session_label(),
            data_status=self.market.data_status(),
            universe=symbols, min_dte=min_dte, max_dte=max_dte,
            ideas=ideas[:limit],
            equities=[e for e in equities if e["score"] > 0][:limit],
            headlines=[h.to_dict() for h in headlines[:25]],
            catalysts=[c.to_dict() for c in catalyst_list],
            narrative=narrative, breadth=breadth, errors=errors[:20],
            elapsed_seconds=round((datetime.now(timezone.utc) - started).total_seconds(), 2),
        )

    async def _scan_symbol(self, symbol: str, spy_closes: list[float],
                           min_dte: int, max_dte: int, today: date) -> SymbolResult:
        try:
            bars, quotes = await asyncio.gather(
                self.market.history(symbol, 180), self.market.quotes([symbol])
            )
            quote = quotes.get(symbol.upper())
            if quote is None:
                empty = Signals(symbol=symbol.upper(), price=0.0)
                return SymbolResult(symbol, empty, assess(empty),
                                    error="no quote available from the data provider")
            if len(bars) < 25:
                sig = build_signals(symbol, bars, quote)
                return SymbolResult(symbol, sig, assess(sig), error="insufficient history")

            expirations = await self.market.expirations(symbol)
            targets = _pick_expirations(expirations, min_dte, max_dte, today)

            # Score the symbol using the nearest qualifying expiry's chain, so
            # the IV read matches the horizon actually being traded.
            primary_chain = None
            if targets:
                primary_chain = await self.market.chain(symbol, targets[0])

            sig = build_signals(symbol, bars, quote, primary_chain, spy_closes)
            assessment = assess(sig)
            equity = rank_equity(sig, bars.closes)

            ideas: list[StrategyIdea] = []
            # Two expiries at most: the nearest and one further out in the
            # window. More than that floods the table with near-duplicates.
            for exp in targets[:2]:
                chain = primary_chain if exp == targets[0] else await self.market.chain(symbol, exp)
                if not chain or not chain.calls:
                    continue
                dte = (date.fromisoformat(exp) - today).days
                ideas.extend(build_ideas(assessment, sig, chain, dte,
                                         rate=settings.risk_free_rate))

            ideas.sort(key=lambda i: i.score, reverse=True)
            return SymbolResult(symbol, sig, assessment, ideas[:3], equity)

        except Exception as exc:                           # noqa: BLE001
            log.exception("scan_symbol %s failed", symbol)
            empty = Signals(symbol=symbol.upper(), price=0.0)
            return SymbolResult(symbol, empty, assess(empty), error=str(exc))

    async def _breadth(self, results: list[SymbolResult]) -> dict:
        """Advance/decline across the scanned names, plus index moves."""
        quotes = await self.market.quotes(BENCHMARKS)
        from .. import market_hours
        advancers = sum(1 for r in results
                        if r.signals.change_pct is not None and r.signals.change_pct > 0)
        decliners = sum(1 for r in results
                        if r.signals.change_pct is not None and r.signals.change_pct < 0)
        spy = quotes.get("SPY")
        return {
            "advancers": advancers,
            "decliners": decliners,
            "spy_change_pct": round(spy.change_pct, 2) if spy and spy.change_pct is not None else None,
            "indices": [q.to_dict() for q in quotes.values()],
            "market_session": market_hours.session_label(),
        }
