"""The scan universe: liquid names whose options are actually tradeable.

Short-dated options only work where there is real depth.  These lists are
restricted to underlyings with active weekly (mostly daily) expirations and
tight markets, grouped so the UI can offer sensible presets.
"""
from __future__ import annotations

from ..config import settings

INDEX_ETFS = ["SPY", "QQQ", "IWM", "DIA"]

SECTOR_ETFS = ["XLF", "XLE", "XLK", "XLV", "XLI", "XLU", "XLP", "XLY", "XLB", "XLRE", "XLC"]

THEME_FUNDS = ["SMH", "SOXL", "ARKK", "GLD", "SLV", "TLT", "HYG", "USO", "UNG", "EEM", "FXI"]

MEGA_CAP = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "BRK.B", "LLY"]

HIGH_BETA = ["AMD", "NFLX", "COIN", "PLTR", "MSTR", "MARA", "RIOT", "SMCI", "MU", "INTC",
             "CRWD", "SNOW", "SHOP", "UBER", "ABNB", "RIVN", "SOFI", "DKNG", "RBLX", "AFRM"]

LARGE_CAP = ["JPM", "BAC", "GS", "WFC", "V", "MA", "UNH", "JNJ", "PFE", "MRK", "XOM", "CVX",
             "COP", "WMT", "COST", "HD", "MCD", "NKE", "DIS", "BA", "CAT", "GE", "T", "VZ",
             "ORCL", "CRM", "ADBE", "QCOM", "TXN", "IBM", "NOW", "PANW", "ANET", "DELL"]

PRESETS: dict[str, list[str]] = {
    "core": INDEX_ETFS + MEGA_CAP + ["AMD", "NFLX", "SMH"],
    "etfs": INDEX_ETFS + SECTOR_ETFS + THEME_FUNDS,
    "megacap": MEGA_CAP,
    "highbeta": HIGH_BETA,
    "wide": INDEX_ETFS + MEGA_CAP + HIGH_BETA + LARGE_CAP + SECTOR_ETFS + THEME_FUNDS,
}

# Benchmarks always fetched for market context, whatever universe is scanned.
BENCHMARKS = ["SPY", "QQQ", "IWM", "DIA"]


def get_universe(preset: str = "core", extra: list[str] | None = None) -> list[str]:
    """Resolve a preset name (or comma-separated ticker list) to symbols."""
    preset = (preset or "core").strip()
    if preset.lower() in PRESETS:
        symbols = list(PRESETS[preset.lower()])
    else:
        # Treat anything unrecognised as an explicit ticker list.
        symbols = [s.strip().upper() for s in preset.replace(" ", ",").split(",") if s.strip()]
        if not symbols:
            symbols = list(PRESETS["core"])

    symbols += settings.extra_universe
    for s in extra or []:
        symbols.append(s.strip().upper())

    return list(dict.fromkeys(s for s in symbols if s))
