"""Configuration loaded from environment variables (and an optional .env)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader - avoids a dependency for six lines of parsing."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class Settings:
    provider_name: str = os.environ.get("MARKET_DATA_PROVIDER", "auto").lower()
    tradier_token: str = os.environ.get("TRADIER_TOKEN", "").strip()
    tradier_base_url: str = os.environ.get("TRADIER_BASE_URL", "https://api.tradier.com/v1")

    host: str = os.environ.get("HOST", "127.0.0.1")
    port: int = _int("PORT", 8000)

    db_path: str = os.environ.get("DB_PATH", str(ROOT / "data" / "market_scanner.db"))

    risk_free_rate: float = _float("RISK_FREE_RATE", 0.04)

    # Cache TTLs in seconds. Quotes are short so paper fills price off a fresh
    # book; chains are longer because they are the expensive call.
    quote_ttl: float = _float("QUOTE_TTL", 5.0)
    chain_ttl: float = _float("CHAIN_TTL", 45.0)
    history_ttl: float = _float("HISTORY_TTL", 600.0)
    news_ttl: float = _float("NEWS_TTL", 300.0)

    extra_universe: list[str] = [
        s.strip().upper() for s in os.environ.get("EXTRA_UNIVERSE", "").split(",") if s.strip()
    ]

    # Paper-trading defaults
    default_cash: float = _float("DEFAULT_CASH", 25_000.0)
    equity_commission: float = _float("EQUITY_COMMISSION", 0.0)
    option_commission: float = _float("OPTION_COMMISSION", 0.65)   # per contract


settings = Settings()
