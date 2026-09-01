"""Yahoo Finance provider - free, no API key.

Yahoo gates most JSON endpoints behind a cookie + "crumb" pair, so the client
bootstraps one on first use and refreshes it when a request comes back 401/403.

Data quality caveats, stated plainly because they change how you should trade
off this feed:
  * Equity quotes are near-real-time (a few seconds behind the tape).
  * Option chains are typically delayed ~15 minutes.
  * Yahoo's own `impliedVolatility` is often stale, so this module re-solves IV
    from the current bid/ask midpoint and computes greeks itself.

Extended hours are supported: the batched quote endpoint carries explicit
pre- and post-market prices with their own timestamps, which are kept separate
from the regular-session price rather than overwriting it.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

import httpx

from .. import market_hours
from ..analytics import blackscholes as bs
from .ratelimit import RateLimiter, retry_after_seconds
from .base import Bar, Bars, NewsItem, OptionChain, OptionContract, Quote

log = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


class YahooProvider:
    name = "yahoo"
    realtime = False   # equities near-real-time, options delayed

    # Yahoo throttles unauthenticated use by IP. These values keep a full
    # universe scan under the threshold; going faster is what produced
    # "no data" while a single manual request still worked.
    def __init__(self, risk_free: float = 0.04, timeout: float = 15.0,
                 max_concurrent: int = 3, min_interval: float = 0.22):
        self.limiter = RateLimiter("Yahoo", max_concurrent, min_interval)
        self.last_error: str | None = None
        self._exp_cache: dict[str, tuple[float, list[str]]] = {}
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

    async def _get_json(self, url: str, params: dict | None = None,
                        crumb: bool = False) -> dict | None:
        params = dict(params or {})
        for attempt in range(3):
            if crumb:
                c = await self._ensure_crumb(force=attempt == 1)
                if c:
                    params["crumb"] = c
            try:
                async with self.limiter:
                    r = await self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.debug("yahoo request failed %s: %s", url, exc)
                return None

            if r.status_code == 429:
                # Throttled. Pause the whole source, then retry once or twice
                # rather than reporting the symbol as having no data.
                delay = retry_after_seconds(r.headers)
                self.last_error = f"HTTP 429 (rate limited, waiting {delay:.0f}s)"
                self.limiter.penalise(delay)
                if attempt < 2:
                    await asyncio.sleep(min(delay, 30.0))
                    continue
                return None

            if r.status_code in (401, 403) and attempt == 0:
                continue                  # stale crumb - refresh and retry once
            if r.status_code != 200:
                self.last_error = f"HTTP {r.status_code}"
                log.debug("yahoo %s -> HTTP %s", url, r.status_code)
                return None
            try:
                self.last_error = None
                return r.json()
            except ValueError:
                self.last_error = "response was not JSON"
                return None
        return None

    # -- quotes -------------------------------------------------------------
    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Batched quote lookup, including pre- and post-market prices.

        The v7 quote endpoint returns every symbol in one request and is the
        only Yahoo endpoint that exposes `preMarketPrice` / `postMarketPrice`
        directly. If it is unavailable the chart endpoint is used per symbol,
        which still yields extended-hours prices from the pre/post bars.
        """
        if not symbols:
            return {}

        out: dict[str, Quote] = {}
        # One request per 50 symbols instead of one per symbol: a 90-name scan
        # becomes 2 requests rather than 90.
        for i in range(0, len(symbols), 50):
            batch = symbols[i:i + 50]
            data = await self._get_json(
                "https://query1.finance.yahoo.com/v7/finance/quote",
                {"symbols": ",".join(batch)}, crumb=True,
            )
            rows = ((data or {}).get("quoteResponse") or {}).get("result") or []
            for row in rows:
                quote = self._quote_from_v7(row)
                if quote:
                    out[quote.symbol] = quote

        missing = [s for s in symbols if s.upper() not in out]
        if missing:
            results = await asyncio.gather(
                *(self._quote_from_chart(s) for s in missing), return_exceptions=True
            )
            for sym, res in zip(missing, results):
                if isinstance(res, Quote):
                    out[sym.upper()] = res
                elif isinstance(res, Exception):
                    log.debug("yahoo quote %s failed: %s", sym, res)
        return out

    @staticmethod
    def _iso(epoch) -> str | None:
        if not epoch:
            return None
        try:
            return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            return None

    def _quote_from_v7(self, row: dict) -> Quote | None:
        symbol = row.get("symbol")
        last = row.get("regularMarketPrice")
        if not symbol or last is None:
            return None

        # Yahoo's marketState uses its own vocabulary; normalise it to the
        # session names the rest of the app speaks.
        state = str(row.get("marketState", "")).upper()
        session = {
            "PRE": market_hours.PRE, "PREPRE": market_hours.CLOSED,
            "REGULAR": market_hours.REGULAR,
            "POST": market_hours.AFTER, "POSTPOST": market_hours.CLOSED,
            "CLOSED": market_hours.CLOSED,
        }.get(state) or market_hours.session()

        extended = extended_time = None
        if session == market_hours.PRE and row.get("preMarketPrice") is not None:
            extended = float(row["preMarketPrice"])
            extended_time = self._iso(row.get("preMarketTime"))
        elif session == market_hours.AFTER and row.get("postMarketPrice") is not None:
            extended = float(row["postMarketPrice"])
            extended_time = self._iso(row.get("postMarketTime"))
        elif row.get("postMarketPrice") is not None and session == market_hours.CLOSED:
            # Market fully closed: the last post-market print is still the most
            # recent trade, so show it rather than pretending it did not happen.
            extended = float(row["postMarketPrice"])
            extended_time = self._iso(row.get("postMarketTime"))

        return Quote(
            symbol=symbol.upper(), last=float(last),
            bid=row.get("bid") or None, ask=row.get("ask") or None,
            previous_close=row.get("regularMarketPreviousClose"),
            open=row.get("regularMarketOpen"), high=row.get("regularMarketDayHigh"),
            low=row.get("regularMarketDayLow"), volume=row.get("regularMarketVolume"),
            timestamp=self._iso(row.get("regularMarketTime")),
            name=row.get("shortName") or row.get("longName"),
            delayed=False, extended_last=extended, extended_timestamp=extended_time,
            market_session=session,
        )

    async def _quote_from_chart(self, symbol: str) -> Quote | None:
        """Fallback quote path, still extended-hours aware.

        The chart endpoint has no pre/post fields, but with includePrePost it
        returns bars outside the regular trading period; the latest of those is
        the extended-hours price.
        """
        data = await self._get_json(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            {"range": "5d", "interval": "1m", "includePrePost": "true"},
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

        extended = extended_time = None
        period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
        regular_end = period.get("end")
        try:
            stamps = result["timestamp"]
            closes = result["indicators"]["quote"][0]["close"]
            if regular_end:
                for i in range(len(stamps) - 1, -1, -1):
                    if closes[i] is None:
                        continue
                    if stamps[i] > regular_end:
                        extended = float(closes[i])
                        extended_time = self._iso(stamps[i])
                    break
        except (TypeError, KeyError, IndexError):
            pass

        ts = meta.get("regularMarketTime")
        return Quote(
            symbol=symbol.upper(), last=float(last),
            bid=meta.get("bid"), ask=meta.get("ask"),
            previous_close=float(prev) if prev else None,
            open=meta.get("regularMarketOpen"), high=meta.get("regularMarketDayHigh"),
            low=meta.get("regularMarketDayLow"), volume=meta.get("regularMarketVolume"),
            timestamp=self._iso(ts),
            name=meta.get("shortName") or meta.get("longName"),
            delayed=False, extended_last=extended, extended_timestamp=extended_time,
            market_session=market_hours.session(),
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
        """Cached briefly in-process: chain() validates against this list, so an
        uncached lookup would double every option request."""
        import time
        hit = self._exp_cache.get(symbol.upper())
        if hit and time.monotonic() - hit[0] < 600:
            return hit[1]
        result = await self._expirations_uncached(symbol)
        if result:
            self._exp_cache[symbol.upper()] = (time.monotonic(), result)
        return result

    async def _expirations_uncached(self, symbol: str) -> list[str]:
        data = await self._get_json(
            f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}", crumb=True
        )
        try:
            stamps = data["optionChain"]["result"][0]["expirationDates"]
        except (TypeError, KeyError, IndexError):
            return []
        return [datetime.fromtimestamp(s, timezone.utc).date().isoformat() for s in stamps]

    async def chain(self, symbol: str, expiration: str) -> OptionChain | None:
        # Only ever quote an expiration the vendor lists. Asking Yahoo for a
        # date that is not a real expiry returns the nearest one instead, which
        # would silently mislabel the whole chain.
        listed = await self.expirations(symbol)
        if listed and expiration not in listed:
            log.debug("%s is not a listed expiration for %s", expiration, symbol)
            return None

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
