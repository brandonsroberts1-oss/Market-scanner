"""Full pipeline against realistic vendor payloads.

Every other test exercises a piece. This one drives the whole app the way it
runs for a user - real provider classes, real HTTP client code, real scanner -
with only the network replaced by recorded-shape responses.

It exists because unit tests all passed while the running app showed nothing:
each part worked, and the assembled pipeline did not.
"""
import asyncio
import math
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from backend.engine.scanner import Scanner
from backend.providers.cboe import CboeProvider
from backend.providers.registry import MarketData
from backend.providers.stooq import StooqProvider
from backend.providers.yahoo import YahooProvider


def run(coro):
    return asyncio.run(coro)


TODAY = datetime.now(timezone.utc).date()


def _next_weekdays(n: int) -> list[date]:
    out, cursor = [], TODAY
    while len(out) < n:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


EXPIRIES = _next_weekdays(6)


def _occ(root: str, day: date, kind: str, strike: float) -> str:
    cp = "C" if kind == "call" else "P"
    return f"{root}{day:%y%m%d}{cp}{int(round(strike * 1000)):08d}"


def cboe_payload(symbol: str = "AAPL", spot: float = 317.15) -> dict:
    """A CBOE delayed-quotes response, in the shape the endpoint returns."""
    options = []
    for expiry in EXPIRIES[:3]:
        dte = max((expiry - TODAY).days, 1)
        for offset in range(-6, 7):
            strike = round(spot / 5) * 5 + offset * 5
            if strike <= 0:
                continue
            for kind in ("call", "put"):
                moneyness = (strike - spot) / spot
                intrinsic = max(spot - strike, 0) if kind == "call" else max(strike - spot, 0)
                extrinsic = max(0.35, 6.0 * math.exp(-((moneyness * 9) ** 2)) * (dte ** 0.5))
                mid = intrinsic + extrinsic
                options.append({
                    "option": _occ(symbol, expiry, kind, strike),
                    "bid": round(mid * 0.985, 2), "ask": round(mid * 1.015, 2),
                    "bid_size": 40, "ask_size": 45,
                    "iv": 0.26, "open_interest": 2400, "volume": 350,
                    "delta": 0.5 - moneyness * 4 if kind == "call" else -0.5 - moneyness * 4,
                    "gamma": 0.03, "theta": -0.22, "vega": 0.12, "rho": 0.01,
                    "theo": round(mid, 2), "last_trade_price": round(mid, 2),
                    "prev_day_close": round(mid, 2), "percent_change": 0.0,
                })
    return {"timestamp": datetime.now(timezone.utc).isoformat(), "symbol": symbol,
            "data": {"symbol": symbol, "security_type": "stock",
                     "current_price": spot, "close": spot, "prev_day_close": spot * 0.99,
                     "bid": spot - 0.02, "ask": spot + 0.02,
                     "open": spot * 0.995, "high": spot * 1.008, "low": spot * 0.99,
                     "volume": 38_000_000, "iv30": 0.27, "options": options}}


def stooq_csv(spot: float = 317.15, days: int = 200) -> str:
    """Stooq daily CSV, header included, oldest first.

    The series has to END at the quoted price: real history and a real quote
    describe the same instrument, and the app now rejects a symbol whose two
    sources disagree.
    """
    sessions = []
    cursor = TODAY - timedelta(days=int(days * 1.45))
    while cursor <= TODAY:
        if cursor.weekday() < 5:
            sessions.append(cursor)
        cursor += timedelta(days=1)

    import random
    rng = random.Random(7)
    growth = 1.0018
    # Walk backwards from the quote so the newest bar matches it.
    prices, price = [], spot
    for _ in sessions:
        prices.append(price)
        price /= growth * (1 + rng.gauss(0, 0.004))
    prices.reverse()

    lines = ["Date,Open,High,Low,Close,Volume"]
    for day, close in zip(sessions, prices):
        lines.append(f"{day.isoformat()},{close*0.996:.2f},{close*1.008:.2f},"
                     f"{close*0.992:.2f},{close:.2f},35000000")
    return "\n".join(lines)


def yahoo_quote_payload(symbols: list[str]) -> dict:
    return {"quoteResponse": {"result": [
        {"symbol": s, "marketState": "POST", "regularMarketPrice": 317.15,
         "regularMarketPreviousClose": 313.90, "postMarketPrice": 317.60,
         "postMarketTime": int(datetime.now(timezone.utc).timestamp()),
         "regularMarketOpen": 314.0, "regularMarketDayHigh": 318.2,
         "regularMarketDayLow": 313.4, "regularMarketVolume": 38_000_000,
         "shortName": f"{s} Inc."} for s in symbols]}}


def make_transport(yahoo_ok: bool = True, cboe_ok: bool = True, stooq_ok: bool = True):
    """Route each vendor's URLs to a realistic response, or a 429."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        host = request.url.host

        if "cboe.com" in host:
            if not cboe_ok:
                return httpx.Response(429, text="Too Many Requests")
            symbol = request.url.path.rsplit("/", 1)[-1].replace(".json", "").lstrip("_")
            return httpx.Response(200, json=cboe_payload(symbol))

        if "stooq.com" in host:
            if not stooq_ok:
                return httpx.Response(429, text="Too Many Requests")
            return httpx.Response(200, text=stooq_csv())

        if "yahoo" in host:
            if not yahoo_ok:
                return httpx.Response(429, headers={"retry-after": "30"}, text="slow down")
            if "getcrumb" in url:
                return httpx.Response(200, text="testcrumb")
            if "fc.yahoo.com" in host:
                return httpx.Response(200, text="ok")
            if "/v7/finance/quote" in url:
                syms = request.url.params.get("symbols", "AAPL").split(",")
                return httpx.Response(200, json=yahoo_quote_payload(syms))
            if "/v1/finance/search" in url:
                return httpx.Response(200, json={"news": []})
            return httpx.Response(200, json={"chart": {"result": [], "error": None}})

        return httpx.Response(404)
    return httpx.MockTransport(handler)


def build_market(**flags) -> MarketData:
    transport = make_transport(**flags)
    providers = [YahooProvider(), CboeProvider(), StooqProvider()]
    for provider in providers:
        provider._client = httpx.AsyncClient(
            transport=transport, follow_redirects=True,
            headers=dict(provider._client.headers))
        # Pacing is irrelevant against a mock and only slows the suite.
        provider.limiter.min_interval = 0.0
    return MarketData(providers, use_store=False)


# ---------------- the whole pipeline ----------------
def test_scan_produces_ideas_from_live_shaped_payloads(fresh_db):
    market = build_market()
    result = run(Scanner(market).scan("core", 0, 5, min_conviction=20, limit=40,
                                      include_news=False))
    assert result.ideas, f"no ideas. diagnosis: {result.diagnosis}"
    assert result.equities, "no equity ranking"
    assert result.scored > 10, f"only {result.scored} symbols scored"

    idea = result.ideas[0]
    assert idea["legs"] and all(l["price"] > 0 for l in idea["legs"])
    assert idea["max_loss"] > 0
    assert 0 <= idea["prob_profit"] <= 1
    assert idea["expiration"]
    assert not result.data_status["missing_symbols"], result.data_status


def test_the_app_works_with_yahoo_completely_unavailable(fresh_db):
    """Your situation: Yahoo rate limiting everything, CBOE and Stooq fine."""
    market = build_market(yahoo_ok=False)
    result = run(Scanner(market).scan("core", 0, 5, min_conviction=20, limit=40,
                                      include_news=False))
    assert result.ideas, f"no ideas without Yahoo. diagnosis: {result.diagnosis}"
    assert result.equities
    assert "yahoo" in result.data_status["sources_down"]
    assert not result.data_status["missing_symbols"]


def test_option_chains_still_price_without_yahoo(fresh_db):
    market = build_market(yahoo_ok=False)
    expirations = run(market.expirations("AAPL"))
    assert expirations, "CBOE should supply expirations on its own"
    chain = run(market.chain("AAPL", expirations[0]))
    assert chain and chain.calls and chain.puts
    assert chain.underlying_price > 0
    assert all(c.mid and c.mid > 0 for c in chain.calls[:5])
    assert any(c.implied_volatility for c in chain.calls)


def test_history_still_loads_without_yahoo(fresh_db):
    market = build_market(yahoo_ok=False)
    bars = run(market.history("AAPL", 180))
    assert len(bars) >= 100, f"only {len(bars)} bars from Stooq"
    assert bars.bars[-1].close > 0
    assert all(b.high >= b.low for b in bars.bars)


def test_quotes_still_load_without_yahoo(fresh_db):
    market = build_market(yahoo_ok=False)
    quotes = run(market.quotes(["AAPL", "MSFT", "NVDA"]))
    assert len(quotes) == 3, f"only got {list(quotes)}"
    assert all(q.last > 0 for q in quotes.values())
    assert all(q.source == "cboe" for q in quotes.values())


def test_a_scan_with_only_stooq_still_ranks_equities(fresh_db):
    """No option source at all: equities must still work, and say why no options."""
    market = build_market(yahoo_ok=False, cboe_ok=False)
    result = run(Scanner(market).scan("core", 0, 5, min_conviction=20,
                                      include_news=False))
    assert result.equities, "equity ranking needs only price history"
    assert not result.ideas
    assert result.diagnosis, "an empty options table must explain itself"


def test_every_source_down_reports_clearly_and_fast(fresh_db):
    import time
    market = build_market(yahoo_ok=False, cboe_ok=False, stooq_ok=False)
    started = time.monotonic()
    result = run(Scanner(market).scan("core", 0, 5, include_news=False))
    assert time.monotonic() - started < 30
    assert not result.ideas
    assert result.diagnosis
    assert result.data_status["missing_symbols"]


# ---------------- parsing against realistic payloads ----------------
def test_cboe_parses_a_realistic_payload():
    provider = CboeProvider()
    provider._client = httpx.AsyncClient(transport=make_transport())
    provider.limiter.min_interval = 0.0

    quotes = run(provider.quotes(["AAPL"]))
    assert quotes["AAPL"].last == pytest.approx(317.15)

    expirations = run(provider.expirations("AAPL"))
    assert len(expirations) >= 3
    assert all(e >= TODAY.isoformat() for e in expirations)

    chain = run(provider.chain("AAPL", expirations[0]))
    assert chain and len(chain.calls) > 5 and len(chain.puts) > 5
    call = chain.calls[0]
    assert call.bid and call.ask and call.ask >= call.bid
    assert call.implied_volatility and call.open_interest
    assert call.delta is not None


def test_stooq_parses_a_realistic_csv():
    provider = StooqProvider()
    provider._client = httpx.AsyncClient(transport=make_transport())
    provider.limiter.min_interval = 0.0

    bars = run(provider.history("AAPL", 180))
    assert len(bars) >= 100
    assert bars.bars[0].date < bars.bars[-1].date
    quotes = run(provider.quotes(["AAPL"]))
    assert quotes["AAPL"].last > 0


def test_stooq_handles_lowercase_columns():
    """Column case differs between Stooq endpoints."""
    csv = stooq_csv().replace("Date,Open,High,Low,Close,Volume",
                             "date,open,high,low,close,volume")
    provider = StooqProvider()
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=csv)))
    provider.limiter.min_interval = 0.0
    assert len(run(provider.history("AAPL", 180))) >= 100


def test_cboe_keeps_weekly_roots():
    """Weekly series carry a suffixed root; an exact match dropped the chain."""
    payload = cboe_payload("SPX", 5800.0)
    for row in payload["data"]["options"]:
        row["option"] = row["option"].replace("SPX", "SPXW", 1)

    provider = CboeProvider()
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    provider.limiter.min_interval = 0.0
    expirations = run(provider.expirations("SPX"))
    assert expirations, "weekly roots were discarded"
    assert run(provider.chain("SPX", expirations[0])) is not None
