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

MAX_CONCURRENCY = 6

# How many symbols get an option chain fetched. Chains are the expensive call,
# and a chain is only useful for a name that already scores well on price
# action, so they are fetched after ranking rather than for the whole universe.
DEFAULT_CHAIN_BUDGET = 18

# A scan returns within this many seconds no matter what the data sources do.
# Whatever finished in time is reported; the rest is listed as unavailable. An
# unbounded scan is worse than a partial one - it just spins.
SCAN_DEADLINE_SECONDS = 45.0


@dataclass
class SymbolResult:
    symbol: str
    signals: Signals
    assessment: Assessment
    ideas: list[StrategyIdea] = field(default_factory=list)
    equity: EquityIdea | None = None
    error: str | None = None
    bars: object | None = None      # carried from the scoring pass to the option pass
    quote: object | None = None


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


async def _gather_within(coros: list, budget: float, label: str = "") -> list:
    """Run everything concurrently, but never past `budget` seconds.

    Tasks that have not finished are cancelled and reported as exceptions, so a
    slow or throttled source costs coverage rather than making the whole scan
    hang.
    """
    tasks = [asyncio.ensure_future(c) for c in coros]
    if not tasks:
        return []
    done, pending = await asyncio.wait(tasks, timeout=max(budget, 1.0))
    if pending:
        log.warning("%s: %d of %d did not finish within %.0fs; cancelling them",
                    label or "scan", len(pending), len(tasks), budget)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    results = []
    for task in tasks:
        if task in pending:
            results.append(TimeoutError(f"{label} timed out"))
        else:
            try:
                results.append(task.result())
            except Exception as exc:                       # noqa: BLE001
                results.append(exc)
    return results


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
                   include_news: bool = True,
                   chain_budget: int = DEFAULT_CHAIN_BUDGET) -> ScanResult:
        """Score the universe on price data, then price options for the best of it.

        Two passes on purpose. Free data endpoints throttle by IP, and fetching
        an option chain for every symbol is what pushes a scan over the limit -
        which shows up as "no data" even though each individual request works.
        Scoring first means chains are only fetched for names that already look
        worth trading.
        """
        started = datetime.now(timezone.utc)
        symbols = get_universe(preset)
        today = started.date()
        self.market.reset_status()

        # One batched request for the whole universe rather than one per name.
        # Under the deadline like everything else: this is the first thing a
        # throttled source stalls on.
        opening_budget = SCAN_DEADLINE_SECONDS * 0.35
        try:
            quotes = await asyncio.wait_for(
                self.market.quotes(symbols + BENCHMARKS), timeout=opening_budget)
        except asyncio.TimeoutError:
            log.warning("quote fetch exceeded %.0fs; continuing without it",
                        opening_budget)
            quotes = {}

        try:
            spy_bars = await asyncio.wait_for(
                self.market.history("SPY", 180), timeout=opening_budget)
        except asyncio.TimeoutError:
            from ..providers.base import Bars
            spy_bars = Bars("SPY", [])
        spy_closes = spy_bars.closes

        sem = asyncio.Semaphore(MAX_CONCURRENCY)

        async def score_one(symbol: str) -> SymbolResult:
            async with sem:
                return await self._score_symbol(symbol, quotes.get(symbol.upper()),
                                                spy_closes)

        scored = await _gather_within(
            [score_one(s) for s in symbols], SCAN_DEADLINE_SECONDS * 0.6,
            label="scoring")

        errors: list[str] = []
        symbol_results: list[SymbolResult] = []
        for symbol, res in zip(symbols, scored):
            if isinstance(res, Exception):
                log.warning("scan failed for %s: %s", symbol, res)
                errors.append(f"{symbol}: {res}")
                continue
            if res.error:
                errors.append(f"{symbol}: {res.error}")
            symbol_results.append(res)

        # -- second pass: options only for the strongest candidates ----------
        candidates = [r for r in symbol_results
                      if not r.error and r.assessment.conviction >= max(min_conviction, 1)]
        candidates.sort(key=lambda r: r.assessment.conviction, reverse=True)
        candidates = candidates[:max(chain_budget, 0)]

        async def options_for(result: SymbolResult) -> None:
            async with sem:
                await self._attach_option_ideas(result, min_dte, max_dte, today)

        if candidates:
            remaining = max(
                5.0,
                SCAN_DEADLINE_SECONDS
                - (datetime.now(timezone.utc) - started).total_seconds())
            await _gather_within([options_for(r) for r in candidates], remaining,
                                 label="option chains")

        # -- news and catalysts ---------------------------------------------
        headlines: list[cat.ScoredHeadline] = []
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if include_news and elapsed < SCAN_DEADLINE_SECONDS:
            top_symbols = [r.symbol for r in candidates[:4]]
            try:
                raw = await asyncio.wait_for(
                    self.market.news(BENCHMARKS[:1] + top_symbols, 40),
                    timeout=max(3.0, SCAN_DEADLINE_SECONDS - elapsed))
                headlines = cat.score_news(raw)
            except asyncio.TimeoutError:
                errors.append("news: timed out")
            except Exception as exc:                       # noqa: BLE001
                log.warning("news fetch failed: %s", exc)
                errors.append(f"news: {exc}")

        catalyst_list = cat.upcoming_catalysts(today, 10)
        breadth = self._breadth(symbol_results, quotes)
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

    async def _score_symbol(self, symbol: str, quote, spy_closes: list[float]) -> SymbolResult:
        """First pass: price history and signals only. No option requests."""
        try:
            if quote is None:
                empty = Signals(symbol=symbol.upper(), price=0.0)
                return SymbolResult(symbol, empty, assess(empty),
                                    error="no quote available from the data provider")

            bars = await self.market.history(symbol, 180)
            if len(bars) < 25:
                sig = build_signals(symbol, bars, quote)
                return SymbolResult(symbol, sig, assess(sig), error="insufficient history")

            sig = build_signals(symbol, bars, quote, None, spy_closes)
            assessment = assess(sig)
            equity = rank_equity(sig, bars.closes)
            result = SymbolResult(symbol, sig, assessment, [], equity)
            result.bars = bars
            result.quote = quote
            return result
        except Exception as exc:                           # noqa: BLE001
            log.exception("scoring %s failed", symbol)
            empty = Signals(symbol=symbol.upper(), price=0.0)
            return SymbolResult(symbol, empty, assess(empty), error=str(exc))

    async def _attach_option_ideas(self, result: SymbolResult, min_dte: int,
                                   max_dte: int, today: date) -> None:
        """Second pass: fetch the chain for one symbol and build its structures."""
        symbol = result.symbol
        try:
            expirations = await self.market.expirations(symbol)
            targets = _pick_expirations(expirations, min_dte, max_dte, today)
            if not targets:
                return

            primary_chain = await self.market.chain(symbol, targets[0])
            if not primary_chain or not primary_chain.calls:
                return

            # Re-score with the chain in hand so implied vol informs the read.
            # The quote came from the batched fetch in the first pass; asking
            # for it again would be one more request per candidate.
            quote = result.quote
            bars = getattr(result, "bars", None)
            if bars is not None and quote is not None:
                result.signals = build_signals(symbol, bars, quote, primary_chain)
                result.assessment = assess(result.signals)

            ideas: list[StrategyIdea] = []
            for exp in targets[:2]:
                chain = (primary_chain if exp == targets[0]
                         else await self.market.chain(symbol, exp))
                if not chain or not chain.calls:
                    continue
                dte = (date.fromisoformat(exp) - today).days
                ideas.extend(build_ideas(result.assessment, result.signals, chain, dte,
                                         rate=settings.risk_free_rate))

            ideas.sort(key=lambda i: i.score, reverse=True)
            result.ideas = ideas[:3]
        except Exception as exc:                           # noqa: BLE001
            log.debug("option ideas for %s failed: %s", symbol, exc)
            result.error = result.error or f"options unavailable: {exc}"

    def _breadth(self, results: list[SymbolResult], quotes: dict) -> dict:
        """Advance/decline across the scanned names, plus index moves.

        Uses the quotes already fetched for the scan rather than issuing more
        requests for symbols that were just retrieved.
        """
        from .. import market_hours
        advancers = sum(1 for r in results
                        if r.signals.change_pct is not None and r.signals.change_pct > 0)
        decliners = sum(1 for r in results
                        if r.signals.change_pct is not None and r.signals.change_pct < 0)
        spy = quotes.get("SPY")
        indices = [quotes[s].to_dict() for s in BENCHMARKS if s in quotes]
        return {
            "advancers": advancers,
            "decliners": decliners,
            "spy_change_pct": round(spy.change_pct, 2)
            if spy and spy.change_pct is not None else None,
            "indices": indices,
            "market_session": market_hours.session_label(),
        }
