"""Stooq - free daily price history as CSV, no key and no handshake.

Stooq serves plain CSV over a stable URL with no authentication of any kind,
which makes it a dependable source of daily bars when Yahoo is blocking or
throttling. It has no option chain and no intraday or extended-hours data, so
it covers history and a last close, nothing more.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone

import httpx

from .. import market_hours
from .ratelimit import RateLimiter, SourcePaused
from .base import Bar, Bars, NewsItem, OptionChain, Quote

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _stooq_symbol(symbol: str) -> str:
    """US tickers carry a .us suffix; BRK.B style names use a dash."""
    return symbol.lower().replace(".", "-") + ".us"


class StooqProvider:
    name = "stooq"
    realtime = False

    def __init__(self, timeout: float = 20.0):
        self.limiter = RateLimiter("Stooq", 2, 0.25)
        self._client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=True,
            headers={"User-Agent": UA, "Accept": "text/csv,*/*"},
        )
        self.last_error: str | None = None

    async def _csv(self, url: str) -> list[dict] | None:
        try:
            async with self.limiter:
                response = await self._client.get(url)
        except SourcePaused as exc:
            self.last_error = str(exc)
            return None
        except httpx.HTTPError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        if response.status_code != 200:
            self.last_error = f"HTTP {response.status_code}"
            return None
        text = response.text.strip()
        # Stooq answers an unknown symbol with a one-line body, not an error.
        if not text or text.lower().startswith("no data") or "\n" not in text:
            self.last_error = "no data for that symbol"
            return None
        self.last_error = None
        return list(csv.DictReader(io.StringIO(text)))

    async def history(self, symbol: str, days: int = 180, interval: str = "1d") -> Bars:
        rows = await self._csv(
            f"https://stooq.com/q/d/l/?s={_stooq_symbol(symbol)}&i=d")
        if not rows:
            return Bars(symbol.upper(), [])

        bars: list[Bar] = []
        for row in rows:
            try:
                bars.append(Bar(
                    row["Date"], float(row["Open"]), float(row["High"]),
                    float(row["Low"]), float(row["Close"]),
                    float(row.get("Volume") or 0),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return Bars(symbol.upper(), bars[-days:] if days else bars)

    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Last close per symbol, derived from the daily series.

        Stooq has no live quote endpoint worth relying on, so this is the most
        recent settled close - correct outside market hours, and behind during
        the session. It exists as a last resort when nothing else responds.
        """
        out: dict[str, Quote] = {}
        for symbol in symbols:
            bars = await self.history(symbol, days=5)
            if len(bars) < 2:
                continue
            last, prev = bars.bars[-1], bars.bars[-2]
            out[symbol.upper()] = Quote(
                symbol=symbol.upper(), last=last.close, previous_close=prev.close,
                open=last.open, high=last.high, low=last.low, volume=last.volume,
                timestamp=datetime.combine(
                    datetime.fromisoformat(last.date).date(),
                    datetime.min.time(), timezone.utc).isoformat(),
                name=symbol.upper(), delayed=True,
                market_session=market_hours.session(),
            )
        return out

    async def expirations(self, symbol: str) -> list[str]:
        return []

    async def chain(self, symbol: str, expiration: str) -> OptionChain | None:
        return None

    async def news(self, symbols: list[str], limit: int = 30) -> list[NewsItem]:
        return []

    async def close(self) -> None:
        await self._client.aclose()
