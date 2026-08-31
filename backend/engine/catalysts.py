"""Headlines and scheduled catalysts - what may move the market today or this week.

Two distinct things are surfaced:

  * Headlines - the live newsfeed for the scanned names and the broad market,
    scored for tone with a finance-specific keyword lexicon.
  * Scheduled catalysts - recurring macro events (CPI, FOMC, payrolls, PCE,
    jobless claims, OPEX) computed from the calendar. These are known in
    advance and are the main reason a quiet week turns loud.

The macro schedule uses each release's published rule (e.g. "first Friday" for
payrolls) rather than a hard-coded list of dates, so it stays correct without
maintenance. Exact times shift occasionally, so treat them as day-accurate.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, asdict
from datetime import date, timedelta

from ..providers.base import NewsItem

# Finance-tuned tone lexicon. Deliberately small and readable: a big opaque
# model here would be harder to trust than a list you can audit.
BULLISH_TERMS = {
    "beat": 2.0, "beats": 2.0, "surge": 2.5, "surges": 2.5, "soar": 2.5, "soars": 2.5,
    "rally": 2.0, "rallies": 2.0, "jump": 1.5, "jumps": 1.5, "upgrade": 2.5,
    "upgraded": 2.5, "outperform": 2.0, "raises guidance": 3.0, "record high": 2.0,
    "tops estimates": 2.5, "strong demand": 2.0, "buyback": 1.5, "approval": 1.5,
    "expands": 1.0, "wins": 1.5, "profit": 1.0, "growth": 1.0, "bullish": 2.0,
    "optimism": 1.5, "breakout": 1.5, "beats estimates": 2.5, "raised": 1.5,
}
BEARISH_TERMS = {
    "miss": -2.0, "misses": -2.0, "plunge": -2.5, "plunges": -2.5, "sink": -2.0,
    "sinks": -2.0, "slump": -2.0, "tumble": -2.5, "tumbles": -2.5, "downgrade": -2.5,
    "downgraded": -2.5, "underperform": -2.0, "cuts guidance": -3.0, "warns": -2.5,
    "warning": -2.0, "probe": -1.5, "investigation": -2.0, "lawsuit": -1.5,
    "recall": -2.0, "layoffs": -1.5, "bankruptcy": -3.0, "fraud": -3.0,
    "falls": -1.5, "drops": -1.5, "slides": -1.5, "bearish": -2.0, "selloff": -2.0,
    "weak demand": -2.0, "halts": -1.5, "delays": -1.5, "loss": -1.0, "slashes": -2.5,
}
HIGH_IMPACT_TERMS = {
    "fomc", "fed", "cpi", "inflation", "payrolls", "jobs report", "gdp", "pce",
    "rate cut", "rate hike", "powell", "tariff", "earnings", "guidance",
    "merger", "acquisition", "sec", "antitrust", "shutdown", "downgrade", "upgrade",
}


@dataclass
class ScoredHeadline:
    headline: str
    source: str
    url: str | None
    published: str | None
    symbols: list[str]
    sentiment: float        # -1..1
    tone: str               # bullish | bearish | neutral
    impact: str             # high | medium | low

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Catalyst:
    date: str
    time_et: str
    title: str
    category: str           # macro | market_structure
    importance: str         # high | medium
    note: str

    def to_dict(self) -> dict:
        return asdict(self)


def score_headline(text: str) -> tuple[float, str]:
    """Return (sentiment -1..1, tone). Multi-word phrases are checked first."""
    lowered = " " + re.sub(r"[^a-z0-9 %$.-]+", " ", (text or "").lower()) + " "
    raw = 0.0
    hits = 0
    for lexicon in (BULLISH_TERMS, BEARISH_TERMS):
        for term, weight in lexicon.items():
            if " " in term:
                if term in lowered:
                    raw += weight
                    hits += 1
            elif re.search(rf"\b{re.escape(term)}\b", lowered):
                raw += weight
                hits += 1
    if hits == 0:
        return 0.0, "neutral"
    # Normalise by a 3-term saturation point so one strong word does not peg it.
    score = max(-1.0, min(1.0, raw / 6.0))
    tone = "bullish" if score > 0.12 else "bearish" if score < -0.12 else "neutral"
    return round(score, 3), tone


def classify_impact(text: str) -> str:
    lowered = (text or "").lower()
    hits = sum(1 for term in HIGH_IMPACT_TERMS if term in lowered)
    if hits >= 2:
        return "high"
    return "medium" if hits == 1 else "low"


def score_news(items: list[NewsItem]) -> list[ScoredHeadline]:
    out: list[ScoredHeadline] = []
    for n in items:
        text = f"{n.headline} {n.summary or ''}"
        sentiment, tone = score_headline(text)
        out.append(ScoredHeadline(
            headline=n.headline, source=n.source, url=n.url, published=n.published,
            symbols=n.symbols[:6], sentiment=sentiment, tone=tone,
            impact=classify_impact(text),
        ))
    return out


def symbol_sentiment(headlines: list[ScoredHeadline], symbol: str) -> float | None:
    """Average tone of headlines mentioning a symbol, recency-weighted by order."""
    matched = [h for h in headlines if symbol.upper() in [s.upper() for s in h.symbols]]
    if not matched:
        return None
    weights = [1.0 / (1 + i * 0.35) for i in range(len(matched))]
    total = sum(weights)
    return round(sum(h.sentiment * w for h, w in zip(matched, weights)) / total, 3)


# --------------------------------------------------------------------------
# Scheduled macro calendar
# --------------------------------------------------------------------------
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth `weekday` (0=Mon) of a month, e.g. 3rd Friday for OPEX."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _business_day_on_or_after(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


# FOMC meetings are set a year ahead and do not follow a simple rule, so the
# announced 2026 dates are listed; anything outside the list is simply omitted
# rather than guessed at.
FOMC_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
             "2026-07-29", "2026-09-16", "2026-11-04", "2026-12-16"]


def upcoming_catalysts(start: date | None = None, days: int = 10) -> list[Catalyst]:
    """Scheduled macro events in the next `days` days."""
    start = start or date.today()
    end = start + timedelta(days=days)
    out: list[Catalyst] = []

    def add(d: date, time_et: str, title: str, importance: str, note: str,
            category: str = "macro") -> None:
        if start <= d <= end:
            out.append(Catalyst(d.isoformat(), time_et, title, category, importance, note))

    cursor = date(start.year, start.month, 1)
    for _ in range(3):                        # cover this month plus the next two
        y, m = cursor.year, cursor.month

        # Non-farm payrolls: first Friday.
        add(_nth_weekday(y, m, 4, 1), "08:30", "Non-farm payrolls", "high",
            "The month's biggest scheduled vol event for index options. "
            "Expect a gap and a wide opening range.")

        # CPI: usually around the 10th-13th business day window.
        add(_business_day_on_or_after(date(y, m, 12)), "08:30", "CPI inflation report", "high",
            "Rate-path repricing. Index IV is typically bid into it and crushed after.")

        # PPI generally follows CPI by a business day.
        cpi_day = _business_day_on_or_after(date(y, m, 12))
        add(_business_day_on_or_after(cpi_day + timedelta(days=1)), "08:30",
            "PPI producer prices", "medium",
            "Secondary inflation read; matters most when it disagrees with CPI.")

        # Core PCE: the Fed's preferred gauge, near month end.
        last_day = calendar.monthrange(y, m)[1]
        pce = date(y, m, last_day)
        while pce.weekday() >= 5:
            pce -= timedelta(days=1)
        add(pce, "08:30", "Core PCE price index", "medium",
            "The Fed's preferred inflation gauge.")

        # Monthly options expiration: third Friday.
        add(_nth_weekday(y, m, 4, 3), "16:00", "Monthly options expiration (OPEX)", "medium",
            "Large open interest rolls off. Pinning near big strikes is common into the close.",
            "market_structure")

        cursor = date(y + (m == 12), (m % 12) + 1, 1)

    for iso in FOMC_2026:
        d = date.fromisoformat(iso)
        add(d, "14:00", "FOMC rate decision", "high",
            "Statement at 14:00 ET, press conference 14:30. Avoid holding short-dated "
            "premium through the announcement unless that is the trade.")

    # Weekly jobless claims: every Thursday.
    d = start
    while d <= end:
        if d.weekday() == 3:
            add(d, "08:30", "Initial jobless claims", "medium",
                "Weekly labour read; moves rates more than equities unless it surprises badly.")
        d += timedelta(days=1)

    out.sort(key=lambda c: (c.date, c.time_et))
    return out


def market_narrative(headlines: list[ScoredHeadline], catalysts: list[Catalyst],
                     breadth: dict | None = None) -> str:
    """A plain-language summary of what is driving the tape.

    Built from measured inputs only - headline tone, scheduled events and index
    breadth - so it never asserts anything the data does not support.
    """
    parts: list[str] = []

    if breadth:
        advancers = breadth.get("advancers", 0)
        decliners = breadth.get("decliners", 0)
        spy = breadth.get("spy_change_pct")
        if spy is not None:
            direction = "higher" if spy > 0.1 else "lower" if spy < -0.1 else "flat"
            parts.append(f"S&P 500 proxy is {direction} ({spy:+.2f}%) with {advancers} of "
                         f"{advancers + decliners} tracked names advancing.")

    scored = [h for h in headlines if h.sentiment]
    if scored:
        avg = sum(h.sentiment for h in scored) / len(scored)
        tone = "constructive" if avg > 0.1 else "cautious" if avg < -0.1 else "mixed"
        high = [h for h in headlines if h.impact == "high"]
        parts.append(f"Headline tone across {len(scored)} scored stories is {tone} "
                     f"(avg {avg:+.2f})" + (f", with {len(high)} high-impact items." if high else "."))

    today = date.today().isoformat()
    todays = [c for c in catalysts if c.date == today]
    soon = [c for c in catalysts if c.date > today and c.importance == "high"][:2]
    if todays:
        parts.append("Today: " + "; ".join(f"{c.title} at {c.time_et} ET" for c in todays) + ".")
    if soon:
        parts.append("Ahead: " + "; ".join(f"{c.title} on {c.date}" for c in soon) + ".")
    if not todays and not soon:
        parts.append("No high-impact scheduled macro events in the immediate window.")

    return " ".join(parts)
