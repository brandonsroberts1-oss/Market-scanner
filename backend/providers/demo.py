"""Deterministic offline market simulator.

This provider needs no network and no API key.  It generates a reproducible
price history per symbol (seeded from the symbol name), then prices a full
option chain off that history with Black-Scholes plus a realistic volatility
smile, bid/ask spreads and open-interest profile.

It exists for three reasons: the app stays usable when a data vendor is down,
the test suite is deterministic, and you can explore the whole UI without
signing up for anything.  Prices are SIMULATED - never trade off them.
"""
from __future__ import annotations

import hashlib
import math
import random
from datetime import date, datetime, timedelta, timezone

from ..analytics import blackscholes as bs
from .base import Bar, Bars, NewsItem, OptionChain, OptionContract, Quote, occ_symbol

# Rough starting price / annual vol / drift per well-known symbol so the demo
# world resembles the real one.  Anything not listed gets deterministic values
# derived from a hash of the ticker.
_PROFILES: dict[str, tuple[float, float, float]] = {
    "SPY": (765.0, 0.13, 0.09), "QQQ": (714.0, 0.17, 0.12), "IWM": (293.0, 0.20, 0.05),
    "DIA": (486.0, 0.12, 0.07), "AAPL": (316.0, 0.24, 0.10), "MSFT": (512.0, 0.23, 0.11),
    "NVDA": (220.0, 0.45, 0.25), "AMZN": (243.0, 0.28, 0.12), "GOOGL": (208.0, 0.26, 0.11),
    "META": (735.0, 0.32, 0.14), "TSLA": (362.0, 0.52, 0.08), "AMD": (168.0, 0.44, 0.10),
    "NFLX": (1180.0, 0.33, 0.12), "JPM": (301.0, 0.21, 0.08), "XLF": (54.0, 0.16, 0.07),
    "XLE": (94.0, 0.24, 0.04), "XLK": (275.0, 0.21, 0.13), "SMH": (312.0, 0.35, 0.18),
    "GLD": (338.0, 0.14, 0.09), "TLT": (89.0, 0.13, 0.02), "COIN": (312.0, 0.62, 0.15),
    "PLTR": (172.0, 0.55, 0.20), "MSTR": (285.0, 0.75, 0.10), "ARKK": (78.0, 0.38, 0.09),
    "VIX": (15.0, 0.90, 0.00),
}


def _seed_for(symbol: str) -> int:
    return int(hashlib.sha256(symbol.upper().encode()).hexdigest()[:8], 16)


def _profile(symbol: str) -> tuple[float, float, float]:
    if symbol.upper() in _PROFILES:
        return _PROFILES[symbol.upper()]
    rng = random.Random(_seed_for(symbol))
    return (rng.uniform(25.0, 400.0), rng.uniform(0.20, 0.55), rng.uniform(-0.05, 0.20))


class DemoProvider:
    """Simulated provider - deterministic given (symbol, as_of date)."""

    name = "demo"
    realtime = False

    def __init__(self, as_of: date | None = None):
        # Pinning `as_of` makes every derived number stable for a given day,
        # so tests and the UI agree.
        self.as_of = as_of or datetime.now(timezone.utc).date()

    # -- price path ---------------------------------------------------------
    def _path(self, symbol: str, days: int) -> list[Bar]:
        s0, vol, drift = _profile(symbol)
        rng = random.Random(_seed_for(symbol) ^ (self.as_of.toordinal() // 7))
        dt = 1.0 / 252.0

        # Walk backwards from today so the final bar is always "now".
        n = days + 1
        shocks = [rng.gauss(0.0, 1.0) for _ in range(n)]
        # Mild volatility clustering makes the indicator output realistic.
        vols = []
        v = vol
        for i in range(n):
            v = max(0.6 * vol, min(2.2 * vol, v * (1.0 + 0.12 * rng.gauss(0, 1))))
            vols.append(v)

        prices = [s0]
        for i in range(n - 1, 0, -1):
            step = math.exp((drift - 0.5 * vols[i] ** 2) * dt + vols[i] * math.sqrt(dt) * shocks[i])
            prices.append(prices[-1] / step)
        prices.reverse()  # oldest -> newest, ending at s0

        # Build the weekday calendar backwards from `as_of` so the final bar
        # always lands on the current session.
        days_back: list[date] = []
        d = self.as_of
        while len(days_back) < n:
            if d.weekday() < 5:
                days_back.append(d)
            d -= timedelta(days=1)
        days_back.reverse()

        bars: list[Bar] = []
        base_vol_shares = 2_000_000 + (_seed_for(symbol) % 40) * 1_500_000
        for idx, day in enumerate(days_back):
            close = prices[idx]
            prev = prices[idx - 1] if idx > 0 else close
            intraday = abs(rng.gauss(0, 1)) * vols[idx] * close * math.sqrt(dt)
            open_ = prev * (1.0 + rng.gauss(0, 0.15) * vols[idx] * math.sqrt(dt))
            high = max(open_, close) + intraday * 0.6
            low = min(open_, close) - intraday * 0.6
            volume = base_vol_shares * max(0.25, rng.lognormvariate(0.0, 0.35))
            bars.append(Bar(day.isoformat(), round(open_, 2), round(high, 2),
                            round(low, 2), round(close, 2), round(volume)))
        return bars

    # -- provider API -------------------------------------------------------
    async def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for sym in symbols:
            bars = self._path(sym, 30)
            if not bars:
                continue
            last_bar = bars[-1]
            prev = bars[-2].close if len(bars) > 1 else last_bar.open
            spread = max(0.01, last_bar.close * 0.0002)
            out[sym.upper()] = Quote(
                symbol=sym.upper(), last=last_bar.close,
                bid=round(last_bar.close - spread / 2, 2),
                ask=round(last_bar.close + spread / 2, 2),
                previous_close=prev, open=last_bar.open, high=last_bar.high,
                low=last_bar.low, volume=last_bar.volume,
                timestamp=datetime.now(timezone.utc).isoformat(),
                name=f"{sym.upper()} (simulated)", delayed=True,
            )
        return out

    async def history(self, symbol: str, days: int = 180, interval: str = "1d") -> Bars:
        return Bars(symbol=symbol.upper(), bars=self._path(symbol, days))

    async def expirations(self, symbol: str) -> list[str]:
        """Weekday expirations for the next two weeks, then weeklies."""
        out, d = [], self.as_of
        for _ in range(14):
            if d.weekday() < 5:
                out.append(d.isoformat())
            d += timedelta(days=1)
        d = self.as_of + timedelta(days=21)
        for _ in range(6):
            while d.weekday() != 4:
                d += timedelta(days=1)
            out.append(d.isoformat())
            d += timedelta(days=7)
        return sorted(set(out))

    async def chain(self, symbol: str, expiration: str) -> OptionChain | None:
        # Only quote expirations this provider actually lists, the way a real
        # vendor does. Fabricating a chain for an unlisted date would hide bugs
        # in callers that pass a stale or malformed expiry.
        if expiration not in await self.expirations(symbol):
            return None
        q = (await self.quotes([symbol])).get(symbol.upper())
        if q is None:
            return None
        spot = q.last
        try:
            exp = date.fromisoformat(expiration)
        except ValueError:
            return None
        dte = max((exp - self.as_of).days, 0)
        t = max(dte, 0.35) * bs.DAY          # intraday floor so 0DTE still prices
        _, base_vol, _ = _profile(symbol)
        rng = random.Random(_seed_for(symbol) ^ exp.toordinal())

        # Term structure: short-dated options carry a vol premium.
        atm_vol = base_vol * (1.0 + 0.35 * math.exp(-dte / 9.0)) * rng.uniform(0.95, 1.12)
        step = self._strike_step(spot)
        atm = round(spot / step) * step
        strikes = [round(atm + i * step, 2) for i in range(-14, 15) if atm + i * step > 0]

        calls, puts = [], []
        for k in strikes:
            moneyness = math.log(k / spot)
            # Equity skew: downside puts bid up, upside calls cheaper.
            smile = atm_vol * (1.0 + 1.6 * moneyness ** 2 - 0.55 * moneyness)
            vol = max(0.03, smile)
            for kind, bucket in (("call", calls), ("put", puts)):
                theo = bs.price(spot, k, t, 0.04, vol, kind)
                g = bs.greeks(spot, k, t, 0.04, vol, kind)
                # Wider markets further from the money and closer to expiry.
                rel = abs(moneyness) * 6.0
                width = max(0.01, theo * (0.012 + 0.05 * rel) + 0.01)
                bid = max(0.0, round(theo - width / 2, 2))
                ask = round(max(theo + width / 2, bid + 0.01), 2)
                oi = int(max(0, 9000 * math.exp(-((moneyness * 9) ** 2)) * rng.uniform(0.4, 1.6)))
                volume = int(oi * rng.uniform(0.05, 0.7))
                bucket.append(OptionContract(
                    symbol=occ_symbol(symbol, expiration, kind, k),
                    underlying=symbol.upper(), expiration=expiration, strike=k, kind=kind,
                    bid=bid, ask=ask, last=round(theo, 2), volume=volume, open_interest=oi,
                    implied_volatility=round(vol, 4), delta=round(g.delta, 4),
                    gamma=round(g.gamma, 5), theta=round(g.theta, 4), vega=round(g.vega, 4),
                ))
        return OptionChain(symbol.upper(), expiration, spot, calls, puts)

    @staticmethod
    def _strike_step(spot: float) -> float:
        if spot < 25:
            return 0.5
        if spot < 100:
            return 1.0
        if spot < 250:
            return 2.5
        return 5.0

    async def news(self, symbols: list[str], limit: int = 30) -> list[NewsItem]:
        templates = [
            ("{sym} extends gains as sector rotation favours the group", "positive"),
            ("Analysts lift {sym} price target citing margin expansion", "positive"),
            ("{sym} slips after cautious guidance from a peer", "negative"),
            ("Options desks flag unusual {sym} call activity into expiry", "neutral"),
            ("{sym} volume runs above average as the range tightens", "neutral"),
        ]
        out = []
        now = datetime.now(timezone.utc)
        for i, sym in enumerate(symbols[:limit]):
            rng = random.Random(_seed_for(sym) ^ self.as_of.toordinal())
            head, _ = rng.choice(templates)
            out.append(NewsItem(
                headline=head.format(sym=sym.upper()), source="Simulated Newsfeed",
                url=None, published=(now - timedelta(minutes=17 * (i + 1))).isoformat(),
                symbols=[sym.upper()],
                summary="Synthetic headline generated by the offline demo provider.",
            ))
        return out[:limit]

    async def close(self) -> None:
        return None
