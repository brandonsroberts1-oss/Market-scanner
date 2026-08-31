"""Normalised market-data types and the provider interface.

Every provider converts its vendor payload into these dataclasses so the
scanner, paper-trading engine and backtester never see vendor-specific JSON.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Protocol


@dataclass
class Quote:
    """A price for one symbol.

    `last` is always the REGULAR-session price - the last trade during normal
    hours, or the official close once the session has ended. Pre- and
    post-market trades live in `extended_last` with their own timestamp, so a
    4:30pm print is never silently presented as the closing price.
    """

    symbol: str
    last: float
    bid: float | None = None
    ask: float | None = None
    previous_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    timestamp: str | None = None
    name: str | None = None
    delayed: bool = False

    # Extended hours
    extended_last: float | None = None
    extended_timestamp: str | None = None
    market_session: str | None = None      # pre-market | regular | after-hours | closed

    # Provenance - set when this came from the last-known-good store rather
    # than a live fetch, so the UI can say how old it is.
    stale: bool = False
    as_of: str | None = None
    source: str | None = None

    @property
    def change(self) -> float | None:
        if self.previous_close in (None, 0):
            return None
        return self.last - self.previous_close

    @property
    def change_pct(self) -> float | None:
        if self.previous_close in (None, 0):
            return None
        return (self.last / self.previous_close - 1.0) * 100.0

    @property
    def extended_change(self) -> float | None:
        """Extended-hours move, measured from the regular close - the way a
        broker quotes it."""
        if self.extended_last is None or not self.last:
            return None
        return self.extended_last - self.last

    @property
    def extended_change_pct(self) -> float | None:
        if self.extended_last is None or not self.last:
            return None
        return (self.extended_last / self.last - 1.0) * 100.0

    @property
    def price(self) -> float:
        """The most recent traded price, whichever session produced it.

        Use this for marking positions and for anything that means "what is it
        worth right now". Use `last` when you specifically mean the regular
        session.
        """
        if self.extended_last is not None and self.extended_last > 0:
            return self.extended_last
        return self.last

    @property
    def mid(self) -> float:
        """Bid/ask midpoint, falling back to the latest trade."""
        if self.bid and self.ask and self.ask >= self.bid > 0:
            return (self.bid + self.ask) / 2.0
        return self.price

    def to_dict(self) -> dict:
        d = asdict(self)
        d["change"] = self.change
        d["change_pct"] = self.change_pct
        d["extended_change"] = self.extended_change
        d["extended_change_pct"] = self.extended_change_pct
        d["price"] = self.price
        return d


@dataclass
class Bar:
    date: str          # ISO date (daily) or ISO datetime (intraday)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Bars:
    symbol: str
    bars: list[Bar] = field(default_factory=list)

    @property
    def closes(self) -> list[float]:
        return [b.close for b in self.bars]

    @property
    def highs(self) -> list[float]:
        return [b.high for b in self.bars]

    @property
    def lows(self) -> list[float]:
        return [b.low for b in self.bars]

    @property
    def volumes(self) -> list[float]:
        return [b.volume for b in self.bars]

    def __len__(self) -> int:
        return len(self.bars)


@dataclass
class OptionContract:
    symbol: str            # OCC-style contract symbol
    underlying: str
    expiration: str        # YYYY-MM-DD
    strike: float
    kind: str              # "call" | "put"
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    volume: float | None = None
    open_interest: float | None = None
    implied_volatility: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None

    @property
    def mid(self) -> float | None:
        """Midpoint price, or the last trade when only one side is quoted."""
        if self.bid is not None and self.ask is not None and self.ask >= self.bid >= 0:
            if self.ask > 0:
                return (self.bid + self.ask) / 2.0
        return self.last

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None or self.ask <= 0:
            return None
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float | None:
        """Bid/ask spread as a fraction of mid - the key liquidity filter.

        A 20%-wide spread on a 1-day option means round-tripping it costs more
        than most of the edge the signal claims to have.
        """
        s, m = self.spread, self.mid
        if s is None or not m or m <= 0:
            return None
        return s / m

    def to_dict(self) -> dict:
        d = asdict(self)
        d["mid"] = self.mid
        d["spread_pct"] = self.spread_pct
        return d


@dataclass
class OptionChain:
    underlying: str
    expiration: str
    underlying_price: float
    calls: list[OptionContract] = field(default_factory=list)
    puts: list[OptionContract] = field(default_factory=list)

    def all(self) -> list[OptionContract]:
        return self.calls + self.puts


@dataclass
class NewsItem:
    headline: str
    source: str
    url: str | None = None
    published: str | None = None
    symbols: list[str] = field(default_factory=list)
    summary: str | None = None


class MarketDataProvider(Protocol):
    """Interface every data source implements."""

    name: str
    realtime: bool

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]: ...

    async def history(self, symbol: str, days: int = 180, interval: str = "1d") -> Bars: ...

    async def expirations(self, symbol: str) -> list[str]: ...

    async def chain(self, symbol: str, expiration: str) -> OptionChain | None: ...

    async def news(self, symbols: list[str], limit: int = 30) -> list[NewsItem]: ...

    async def close(self) -> None: ...


def occ_symbol(underlying: str, expiration: str, kind: str, strike: float) -> str:
    """Build an OCC contract symbol, e.g. SPY260902C00766000."""
    d = date.fromisoformat(expiration)
    cp = "C" if kind.lower().startswith("c") else "P"
    return f"{underlying.upper()}{d:%y%m%d}{cp}{int(round(strike * 1000)):08d}"


def parse_occ(symbol: str) -> dict | None:
    """Inverse of `occ_symbol`; returns None if the symbol is not OCC-shaped."""
    import re
    m = re.fullmatch(r"([A-Z]{1,6})(\d{6})([CP])(\d{8})", symbol.upper())
    if not m:
        return None
    root, yymmdd, cp, strike = m.groups()
    try:
        exp = datetime.strptime(yymmdd, "%y%m%d").date().isoformat()
    except ValueError:
        return None
    return {
        "underlying": root,
        "expiration": exp,
        "kind": "call" if cp == "C" else "put",
        "strike": int(strike) / 1000.0,
    }
