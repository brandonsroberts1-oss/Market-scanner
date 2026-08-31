"""Yahoo and Tradier payload parsing, driven through a mock HTTP transport.

These providers cannot be reached from CI, but their parsing is the code path
most users hit, so the vendor JSON shapes are reproduced here exactly as the
APIs return them - including the awkward parts: Tradier collapsing a
single-element list into a bare object, Yahoo interleaving nulls in its OHLC
arrays, and both using different names for the same field.
"""
import asyncio

import httpx
import pytest

from backend.providers.tradier import TradierProvider
from backend.providers.yahoo import YahooProvider


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Yahoo
# --------------------------------------------------------------------------
YAHOO_CHART = {
    "chart": {"result": [{
        "meta": {
            "symbol": "SPY", "regularMarketPrice": 765.89, "chartPreviousClose": 769.35,
            "regularMarketDayHigh": 767.2, "regularMarketDayLow": 764.1,
            "regularMarketVolume": 36744344, "regularMarketTime": 1788000000,
            "shortName": "SPDR S&P 500 ETF Trust",
        },
        "timestamp": [1787000000, 1787086400, 1787172800],
        "indicators": {"quote": [{
            # Yahoo interleaves nulls for halted or missing bars.
            "open": [771.76, None, 766.10],
            "high": [775.30, None, 768.00],
            "low": [768.31, None, 764.00],
            "close": [769.35, None, 765.89],
            "volume": [36744344, None, 31000000],
        }]},
    }], "error": None}
}

YAHOO_OPTIONS = {
    "optionChain": {"result": [{
        "expirationDates": [1788307200, 1788393600],
        "quote": {"regularMarketPrice": 765.89},
        "options": [{
            "calls": [
                {"contractSymbol": "SPY260902C00760000", "strike": 760.0, "lastPrice": 6.90,
                 "bid": 6.85, "ask": 6.95, "volume": 2100, "openInterest": 4300,
                 "impliedVolatility": 0.44},     # deliberately stale, must be re-solved
                {"contractSymbol": "SPY260902C00766000", "strike": 766.0, "lastPrice": 2.72,
                 "bid": 2.71, "ask": 2.73, "volume": 1420, "openInterest": 1314,
                 "impliedVolatility": 0.44},
            ],
            "puts": [
                {"contractSymbol": "SPY260902P00760000", "strike": 760.0, "lastPrice": 1.90,
                 "bid": 1.88, "ask": 1.92, "volume": 900, "openInterest": 2200,
                 "impliedVolatility": 0.44},
            ],
        }],
    }]}
}

YAHOO_SEARCH = {"news": [
    {"title": "S&P 500 slips as traders position into payrolls",
     "publisher": "Reuters", "link": "https://example.com/a",
     "providerPublishTime": 1788000000, "relatedTickers": ["SPY"]},
]}


def _yahoo_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "getcrumb" in url:
        return httpx.Response(200, text="abc123crumb")
    if "fc.yahoo.com" in url:
        return httpx.Response(200, text="ok")
    if "/v8/finance/chart/" in url:
        return httpx.Response(200, json=YAHOO_CHART)
    if "/v7/finance/options/" in url:
        assert "crumb=abc123crumb" in url, "options call must carry the crumb"
        return httpx.Response(200, json=YAHOO_OPTIONS)
    if "/v1/finance/search" in url:
        return httpx.Response(200, json=YAHOO_SEARCH)
    return httpx.Response(404)


@pytest.fixture
def yahoo():
    p = YahooProvider()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(_yahoo_handler))
    return p


def test_yahoo_parses_a_quote(yahoo):
    q = run(yahoo.quotes(["SPY"]))["SPY"]
    assert q.last == pytest.approx(765.89)
    assert q.previous_close == pytest.approx(769.35)
    assert q.change_pct == pytest.approx(-0.4497, abs=1e-3)
    assert q.name == "SPDR S&P 500 ETF Trust"


def test_yahoo_history_skips_null_bars(yahoo):
    bars = run(yahoo.history("SPY", 30))
    assert len(bars) == 2, "the null bar should be dropped, not zero-filled"
    assert bars.bars[-1].close == pytest.approx(765.89)
    assert all(b.high >= b.low for b in bars.bars)


def test_yahoo_expirations_convert_from_epoch(yahoo):
    assert run(yahoo.expirations("SPY")) == ["2026-09-02", "2026-09-03"]


def test_yahoo_resolves_iv_from_the_live_mid_not_the_stale_field(yahoo):
    """Yahoo's impliedVolatility is often hours old. The provider must re-solve
    it from the current midpoint, which is the whole point of that code path."""
    chain = run(yahoo.chain("SPY", "2026-09-02"))
    assert chain is not None
    atm = next(c for c in chain.calls if c.strike == 766.0)
    assert atm.mid == pytest.approx(2.72)
    # The vendor said 0.44; the real IV implied by a $2.72 mid is far lower.
    assert atm.implied_volatility is not None
    assert atm.implied_volatility < 0.25, f"stale vendor IV was trusted: {atm.implied_volatility}"
    # Greeks must be computed from the re-solved vol.
    assert atm.delta is not None and 0.3 < atm.delta < 0.7
    assert atm.theta is not None and atm.theta < 0


def test_yahoo_news_is_normalised(yahoo):
    items = run(yahoo.news(["SPY"], 5))
    assert items and items[0].source == "Reuters"
    assert items[0].published.startswith("2026-08-29")


def test_yahoo_survives_an_upstream_error():
    p = YahooProvider()
    p._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    assert run(p.quotes(["SPY"])) == {}
    assert len(run(p.history("SPY", 30))) == 0
    assert run(p.expirations("SPY")) == []
    assert run(p.chain("SPY", "2026-09-02")) is None


def test_yahoo_survives_malformed_json():
    p = YahooProvider()
    p._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"unexpected": 1})))
    assert run(p.quotes(["SPY"])) == {}
    assert run(p.chain("SPY", "2026-09-02")) is None


def test_yahoo_retries_once_when_the_crumb_is_rejected():
    calls = {"n": 0}

    def handler(request):
        url = str(request.url)
        if "getcrumb" in url:
            return httpx.Response(200, text="fresh-crumb")
        if "fc.yahoo.com" in url:
            return httpx.Response(200, text="ok")
        if "/v7/finance/options/" in url:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(401, text="Invalid crumb")
            return httpx.Response(200, json=YAHOO_OPTIONS)
        return httpx.Response(404)

    p = YahooProvider()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert run(p.expirations("SPY")) == ["2026-09-02", "2026-09-03"]
    assert calls["n"] == 2, "a rejected crumb should be refreshed and retried once"


# --------------------------------------------------------------------------
# Tradier
# --------------------------------------------------------------------------
TRADIER_QUOTE = {"quotes": {"quote": {
    "symbol": "SPY", "description": "SPDR S&P 500", "last": 765.89, "close": 769.35,
    "prevclose": 769.35, "open": 771.76, "high": 775.30, "low": 768.31,
    "bid": 765.88, "ask": 765.90, "volume": 36744344, "trade_date": 1788000000000,
}}}

TRADIER_CHAIN = {"options": {"option": [
    {"symbol": "SPY260902C00766000", "strike": 766.0, "option_type": "call",
     "bid": 2.71, "ask": 2.73, "last": 2.72, "volume": 1420, "open_interest": 1314,
     # Tradier publishes ANNUAL theta and per-1.00-vol vega.
     "greeks": {"delta": 0.5129, "gamma": 0.0605, "theta": -228.13, "vega": 23.97,
                "mid_iv": 0.1096, "smv_vol": 0.1100}},
    {"symbol": "SPY260902P00766000", "strike": 766.0, "option_type": "put",
     "bid": 2.80, "ask": 2.84, "last": 2.82, "volume": 1100, "open_interest": 990,
     "greeks": {"delta": -0.4871, "gamma": 0.0605, "theta": -220.0, "vega": 23.97,
                "mid_iv": 0.1105, "smv_vol": 0.1110}},
]}}

# A single expiration collapses to a bare string, not a list.
TRADIER_EXPIRATIONS_SINGLE = {"expirations": {"date": "2026-09-02"}}
TRADIER_EXPIRATIONS_MANY = {"expirations": {"date": ["2026-09-02", "2026-09-03"]}}
TRADIER_HISTORY = {"history": {"day": [
    {"date": "2026-08-27", "open": 768.50, "high": 772.36, "low": 767.16,
     "close": 771.10, "volume": 34557064},
    {"date": "2026-08-28", "open": 771.76, "high": 775.30, "low": 768.31,
     "close": 769.35, "volume": 36744344},
]}}


def _tradier_provider(handler):
    p = TradierProvider("test-token")
    p._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer test-token"})
    return p


def _tradier_handler(request):
    path = request.url.path
    if path.endswith("/markets/quotes"):
        return httpx.Response(200, json=TRADIER_QUOTE)
    if path.endswith("/markets/options/chains"):
        return httpx.Response(200, json=TRADIER_CHAIN)
    if path.endswith("/markets/options/expirations"):
        return httpx.Response(200, json=TRADIER_EXPIRATIONS_MANY)
    if path.endswith("/markets/history"):
        return httpx.Response(200, json=TRADIER_HISTORY)
    return httpx.Response(404)


def test_tradier_parses_a_single_quote_object(monkeypatch):
    """Tradier returns a bare object rather than a list for one symbol."""
    from backend import market_hours
    monkeypatch.setattr(market_hours, "session", lambda *a, **k: market_hours.REGULAR)
    q = run(_tradier_provider(_tradier_handler).quotes(["SPY"]))["SPY"]
    assert q.last == pytest.approx(765.89)
    assert q.bid == pytest.approx(765.88)
    assert q.previous_close == pytest.approx(769.35)
    assert q.name == "SPDR S&P 500"
    assert q.extended_last is None, "no extended print during the regular session"


def test_tradier_splits_regular_and_after_hours_prices(monkeypatch):
    """Tradier reports one `last` that silently includes extended-hours prints.

    Uses the real numbers from a live AAPL feed: the regular session closed at
    317.15 and an after-hours trade printed at 317.05.
    """
    from backend import market_hours
    payload = {"quotes": {"quote": {
        "symbol": "AAPL", "description": "Apple", "last": 317.05, "close": 317.15,
        "prevclose": 319.70, "bid": 317.05, "ask": 317.15,
    }}}
    provider = _tradier_provider(lambda r: httpx.Response(200, json=payload))

    monkeypatch.setattr(market_hours, "session", lambda *a, **k: market_hours.AFTER)
    q = run(provider.quotes(["AAPL"]))["AAPL"]
    assert q.last == pytest.approx(317.15), "regular close must not be overwritten"
    assert q.extended_last == pytest.approx(317.05)
    assert q.extended_change_pct == pytest.approx(-0.0315, abs=0.001)
    assert q.price == pytest.approx(317.05), "marking uses the most recent trade"
    assert q.change_pct == pytest.approx(-0.797, abs=0.01), "day change is off the close"
    assert q.market_session == market_hours.AFTER


def test_tradier_treats_a_pre_market_trade_as_extended(monkeypatch):
    from backend import market_hours
    payload = {"quotes": {"quote": {
        "symbol": "AAPL", "last": 321.00, "close": None, "prevclose": 319.70,
    }}}
    provider = _tradier_provider(lambda r: httpx.Response(200, json=payload))
    monkeypatch.setattr(market_hours, "session", lambda *a, **k: market_hours.PRE)
    q = run(provider.quotes(["AAPL"]))["AAPL"]
    assert q.last == pytest.approx(319.70), "pre-market reference is yesterday's close"
    assert q.extended_last == pytest.approx(321.00)
    assert q.extended_change_pct > 0


def test_tradier_expirations_handle_both_shapes():
    many = _tradier_provider(_tradier_handler)
    assert run(many.expirations("SPY")) == ["2026-09-02", "2026-09-03"]

    single = _tradier_provider(
        lambda r: httpx.Response(200, json=TRADIER_EXPIRATIONS_SINGLE))
    assert run(single.expirations("SPY")) == ["2026-09-02"]


def test_tradier_history_parses():
    bars = run(_tradier_provider(_tradier_handler).history("SPY", 30))
    assert len(bars) == 2
    assert bars.bars[-1].close == pytest.approx(769.35)


def test_tradier_rejects_an_unlisted_expiration():
    """Asking a vendor for a date it does not list must return nothing rather
    than a chain silently labelled with the wrong expiry."""
    provider = _tradier_provider(_tradier_handler)
    assert run(provider.chain("SPY", "2026-09-01")) is None   # not in the listed set
    assert run(provider.chain("SPY", "2026-09-02")) is not None


def test_tradier_converts_greeks_to_the_apps_units():
    """Tradier publishes annual theta and per-1.00 vega; this app works in
    per-calendar-day theta and per-vol-point vega."""
    chain = run(_tradier_provider(_tradier_handler).chain("SPY", "2026-09-02"))
    assert chain is not None
    call = chain.calls[0]
    assert call.delta == pytest.approx(0.5129)
    assert call.theta == pytest.approx(-228.13 / 365, abs=1e-4)   # ~-0.625/day
    assert call.vega == pytest.approx(23.97 * 0.01, abs=1e-4)     # ~0.2397/point
    assert call.implied_volatility == pytest.approx(0.1096)
    assert call.mid == pytest.approx(2.72)


def test_tradier_sandbox_is_flagged_as_delayed():
    live = TradierProvider("t", "https://api.tradier.com/v1")
    sandbox = TradierProvider("t", "https://sandbox.tradier.com/v1")
    assert live.realtime is True
    assert sandbox.realtime is False


def test_yahoo_rejects_an_unlisted_expiration(yahoo):
    """Yahoo returns the nearest expiry for a bad date, which would mislabel
    the entire chain, so the date is validated against the listed set first."""
    assert run(yahoo.chain("SPY", "2026-09-01")) is None
    assert run(yahoo.chain("SPY", "2026-09-02")) is not None


def test_yahoo_reads_post_market_price(monkeypatch):
    """The v7 quote endpoint is the only Yahoo path with explicit pre/post."""
    payload = {"quoteResponse": {"result": [{
        "symbol": "AAPL", "marketState": "POST", "regularMarketPrice": 317.15,
        "regularMarketPreviousClose": 319.70, "postMarketPrice": 317.05,
        "postMarketTime": 1788205652, "shortName": "Apple Inc.",
    }]}}

    def handler(request):
        url = str(request.url)
        if "getcrumb" in url:
            return httpx.Response(200, text="c")
        if "fc.yahoo.com" in url:
            return httpx.Response(200, text="ok")
        if "/v7/finance/quote" in url:
            return httpx.Response(200, json=payload)
        return httpx.Response(404)

    p = YahooProvider()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    q = run(p.quotes(["AAPL"]))["AAPL"]
    assert q.last == pytest.approx(317.15)
    assert q.extended_last == pytest.approx(317.05)
    assert q.market_session == "after-hours"
    assert q.extended_timestamp is not None


def test_yahoo_pre_market_price_is_kept_separate():
    payload = {"quoteResponse": {"result": [{
        "symbol": "NVDA", "marketState": "PRE", "regularMarketPrice": 220.88,
        "regularMarketPreviousClose": 217.55, "preMarketPrice": 223.10,
        "preMarketTime": 1788205652,
    }]}}

    def handler(request):
        url = str(request.url)
        if "getcrumb" in url:
            return httpx.Response(200, text="c")
        if "fc.yahoo.com" in url:
            return httpx.Response(200, text="ok")
        if "/v7/finance/quote" in url:
            return httpx.Response(200, json=payload)
        return httpx.Response(404)

    p = YahooProvider()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    q = run(p.quotes(["NVDA"]))["NVDA"]
    assert q.last == pytest.approx(220.88)
    assert q.extended_last == pytest.approx(223.10)
    assert q.market_session == "pre-market"
    assert q.price == pytest.approx(223.10)


def test_tradier_survives_an_auth_failure():
    p = _tradier_provider(lambda r: httpx.Response(401, text="Unauthorized"))
    assert run(p.quotes(["SPY"])) == {}
    assert run(p.chain("SPY", "2026-09-02")) is None
    assert run(p.expirations("SPY")) == []


def test_tradier_handles_null_and_nan_fields():
    payload = {"quotes": {"quote": {"symbol": "XYZ", "last": None, "close": 10.0,
                                    "bid": "", "ask": "NaN", "prevclose": 9.5}}}
    p = _tradier_provider(lambda r: httpx.Response(200, json=payload))
    q = run(p.quotes(["XYZ"]))["XYZ"]
    assert q.last == pytest.approx(10.0)      # falls back to close
    assert q.bid is None and q.ask is None
