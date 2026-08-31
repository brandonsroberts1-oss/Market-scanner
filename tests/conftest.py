"""Shared fixtures. Every test runs against the deterministic demo provider and
a throwaway database, so the suite needs no network and no API key."""
import os
import tempfile
from datetime import date
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="market-scanner-tests-")
os.environ["DB_PATH"] = str(Path(_TMP) / "test.db")

from backend.providers.registry import MarketData        # noqa: E402
from tests.simulated_provider import SimulatedProvider   # noqa: E402

AS_OF = date(2026, 8, 31)


@pytest.fixture
def provider():
    """The simulator is a TEST FIXTURE only - it is not reachable from the app."""
    return SimulatedProvider(as_of=AS_OF)


@pytest.fixture
def market(provider):
    # use_store=False keeps each test isolated from the last-known-good cache.
    return MarketData(provider, use_store=False)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point the app at an empty database for the duration of one test."""
    from backend import config, db
    path = tmp_path / "session.db"
    monkeypatch.setattr(config.settings, "db_path", str(path))
    db._initialised = False
    db.init_db(force=True)
    yield path
    db._initialised = False
