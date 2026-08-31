"""Yahoo Finance provider - free, no API key.

Yahoo gates most JSON endpoints behind a cookie + "crumb" pair, so the client
bootstraps one on first use and refreshes it when a request comes back 401/403.

Data quality caveats, stated plainly because they change how you should trade
off this feed:
  * Equity quotes are near-real-time (a few seconds behind the tape).
  * Option chains are typically delayed ~15 minutes.
  * Yahoo's own `impliedVolatility` is often stale, so this module re-solves IV
    from the current bid/ask midpoint and computes greeks itself.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

import httpx

from ..analytics import blackscholes as bs
from .base import Bar, Bars, NewsItem, OptionChain, OptionContract, Quote

log = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


class YahooProvider:
    name = "yahoo"
    realtime = False   # equities near-real-time, options delayed

    def __init__(self, risk_free: float = 0.04, timeout: float = 15.0):
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": _UA, "Accept": "application/json"},
            follow_redirects=True,
        )
        self._crumb: str | None = None
        self._crumb_lock = asyncio.Lock()
        self.risk_free = risk_free

    # -- auth ---------------------------------------------------------------
    async def _ensure_crumb(self, force: bool = False) -> str | None:
        if self._crumb and not force:
            return self._crumb
        async with self._crumb_lock:
            if self._crumb and not force:
                return self._crumb
            try:
                await self._client.get("https://fc.yahoo.com/")
            except httpx.HTTPError:
                pass                      # the cookie may already be set
            try:
                r = await self._client.get(
                    "https://query2.finance.yahoo.com/v1/test/getcrumb",
                    headers={"Accept": "text/plain"},
                )
                if r.status_code == 200 and r.text and "<" not in r.text:
                    self._crumb = r.text.strip()
            except httpx.HTTPError as exc:
                log.debug("yahoo crumb fetch failed: %s", exc)
            return self._crumb

    async def _get_json(self, url: str, params: dict | None = None, crumb: bool = False) -> dict | None:
        params = dict(params or {})
        for attempt in (0, 1):
            if crumb:
                c = await self._ensure_crumb(force=attempt == 1)
                if c:
                    params["crumb"] = c
            try:
                r = await self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                log.debug("yahoo request failed %s: %s", url, exc)
                return None
            if r.status_code in (401, 403) and attempt == 0:
                continue                  # stale crumb - refresh and retry once
            if r.status_code != 200:
                log.debug("yahoo %s -> HTTP %s", url, r.status_code)
                return None
            try:
                return r.json()
            except ValueError:
                return None
        return None

    # -- quotes -------------------------------------------------------------
    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """One chart call per symbol, run concurrently.

        The chart endpoint is used rather than /v7/finance/quote because it
        stays available without a crumb far more often and carries the same
        fields in `meta`.
        """
        results = await asyncio.gather(
            *(self._quote_one(s) for s in symbols), return_exceptions=True
        )
        out: dict[str, Quote] = {}
        for sym, res in zip(symbols, results):
            if isinstance(res, Quote):
                out[sym.upper()] = res
            elif isinstance(res, Exception):
                log.debug("yahoo quote %s failed: %s", sym, res)
        return out

    async def _quote_one(self, symbol: str) -> Quote | None:
        data = await self._get_json(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            {"range": "5d", "interval": "1d", "includePrePost": "false"},
        )
        try:
            result = data["chart"]["result"][0]
            meta = result["meta"]
        except (TypeError, KeyError, IndexError):
            return None

        last = meta.get("regularMarketPrice")
        if last is None:
            return None
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")

        # Fill OHLC from the most recent complete daily bar.
        o = h = l = v = None
        try:
            q = result["indicators"]["quote"][0]
            for i in range(len(q["close"]) - 1, -1, -1):
                if q["close"][i] is not None:
                    o, h, l, v = q["open"][i], q["high"][i], q["low"][i], q["volume"][i]
                    break
        except (TypeError, KeyError, IndexError):
            pass

        ts = meta.get("regularMarketTime")
        return Quote(
            symbol=symbol.upper(), last=float(last),
            bid=meta.get("bid"), ask=meta.get("ask"),
            previous_close=float(prev) if prev else None,
            open=o, high=meta.get("regularMarketDayHigh", h),
            low=meta.get("regularMarketDayLow", l),
            volume=meta.get("regularMarketVolume", v),
            timestamp=(datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None),
            name=meta.get("shortName") or meta.get("longName"),
            delayed=False,
        )

    # -- history ------------------------------------------------------------
    async def history(self, symbol: str, days: int = 180, interval: str = "1d") -> Bars:
        rng = "1y" if days > 180 else ("6mo" if days > 90 else "3mo")
        if interval in ("5m", "15m", "30m", "1h"):
            rng = "1mo" if interval != "5m" else "5d"
        data = await self._get_json(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            {"range": rng, "interval": interval},
        )
        bars: list[Bar] = []
        try:
            result = data["chart"]["result"][0]
            stamps = result["timestamp"]
            q = result["indicators"]["quote"][0]
            for i, ts in enumerate(stamps):
                if q["close"][i] is None:
                    continue
                dt = datetime.fromtimestamp(ts, timezone.utc)
                label = dt.date().isoformat() if interval == "1d" else dt.isoformat()
                bars.append(Bar(
                    label, float(q["open"][i] or q["close"][i]),
                    float(q["high"][i] or q["close"][i]), float(q["low"][i] or q["close"][i]),
                    float(q["close"][i]), float(q["volume"][i] or 0),
                ))
        except (TypeError, KeyError, IndexError):
            pass
        return Bars(symbol.upper(), bars[-days:] if days else bars)

    # -- options ------------------------------------------------------------
    async def expirations(self, symbol: str) -> list[str]:
        data = await self._get_json(
            f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}", crumb=True
        )
        try:
            stamps = data["optionChain"]["result"][0]["expirationDates"]
        except (TypeError, KeyError, IndexError):
            return []
        return [datetime.fromtimestamp(s, timezone.utc).date().isoformat() for s in stamps]

    async def chain(self, symbol: str, expiration: str) -> OptionChain | None:
        epoch = int(datetime.combine(
            date.fromisoformat(expiration), datetime.min.time(), timezone.utc
        ).timestamp())
        data = await self._get_json(
            f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}",
            {"date": epoch}, crumb=True,
        )
        try:
            result = data["optionChain"]["result"][0]
            spot = float(result["quote"]["regularMarketPrice"])
            raw = result["options"][0]
        except (TypeError, KeyError, IndexError, ValueError):
            return None

        dte = max((date.fromisoformat(expiration) - datetime.now(timezone.utc).date()).days, 0)
        t = max(dte, 0.35) * bs.DAY

        def build(rows, kind) -> list[OptionContract]:
            out = []
            for row in rows or []:
                try:
                    strike = float(row["strike"])
                except (KeyError, TypeError, ValueError):
                    continue
                bid, ask = row.get("bid"), row.get("ask")
                last = row.get("lastPrice")
                c = OptionContract(
                    symbol=row.get("contractSymbol", ""), underlying=symbol.upper(),
                    expiration=expiration, strike=strike, kind=kind,
                    bid=float(bid) if bid is not None else None,
                    ask=float(ask) if ask is not None else None,
                    last=float(last) if last is not None else None,
                    volume=row.get("volume") or 0, open_interest=row.get("openInterest") or 0,
                    implied_volatility=row.get("impliedVolatility"),
                )
                # Prefer IV solved from the live midpoint; Yahoo's own IV field
                # is frequently stale or derived from a last trade hours old.
                mid = c.mid
                if mid and mid > 0:
                    solved = bs.implied_vol(mid, spot, strike, t, self.risk_free, kind)
                    if solved:
                        c.implied_volatility = solved
                if c.implied_volatility:
                    g = bs.greeks(spot, strike, t, self.risk_free, c.implied_volatility, kind)
                    c.delta, c.gamma, c.theta, c.vega = g.delta, g.gamma, g.theta, g.vega
                out.append(c)
            return out

        return OptionChain(symbol.upper(), expiration, spot,
                           build(raw.get("calls"), "call"), build(raw.get("puts"), "put"))

    # -- news ---------------------------------------------------------------
    async def news(self, symbols: list[str], limit: int = 30) -> list[NewsItem]:
        results = await asyncio.gather(
            *(self._news_one(s, limit) for s in symbols[:12]), return_exceptions=True
        )
        seen: set[str] = set()
        out: list[NewsItem] = []
        for res in results:
            if isinstance(res, Exception):
                continue
            for item in res:
                if item.headline in seen:
                    continue
                seen.add(item.headline)
                out.append(item)
        out.sort(key=lambda n: n.published or "", reverse=True)
        return out[:limit]

    async def _news_one(self, symbol: str, limit: int) -> list[NewsItem]:
        data = await self._get_json(
            "https://query1.finance.yahoo.com/v1/finance/search",
            {"q": symbol, "quotesCount": 0, "newsCount": min(limit, 10)},
        )
        out = []
        for n in (data or {}).get("news", []) or []:
            ts = n.get("providerPublishTime")
            out.append(NewsItem(
                headline=n.get("title", ""), source=n.get("publisher", "Yahoo Finance"),
                url=n.get("link"),
                published=(datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None),
                symbols=[symbol.upper()] + list(n.get("relatedTickers") or []),
            ))
        return [n for n in out if n.headline]

    async def close(self) -> None:
        await self._client.aclose()
