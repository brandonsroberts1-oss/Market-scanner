"""Market sessions, the NYSE calendar, and the timezone fallback.

The timezone tests exist because of a real failure: Windows does not ship the
IANA database, so `ZoneInfo("America/New_York")` raised at import and the app
would not start at all. These assert both that the dependency is declared and
that a missing database degrades rather than crashes.
"""
import builtins
import importlib
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend import market_hours as mh

ROOT = Path(__file__).resolve().parent.parent


# ---------------- timezone availability ----------------
def test_tzdata_is_declared_for_windows():
    """Windows has no system tz database; zoneinfo needs the PyPI package."""
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "tzdata" in requirements, "tzdata must be a dependency for Windows"
    assert "win32" in requirements, "tzdata should be marked for the win32 platform"


def test_eastern_zone_resolves():
    now = datetime.now(timezone.utc).astimezone(mh.EASTERN)
    assert now.tzname() in ("EST", "EDT")


def test_app_starts_without_the_iana_database(monkeypatch):
    """Reproduces the Windows failure: zoneinfo raising for America/New_York.

    The module must still import and produce a working Eastern clock.
    """
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "zoneinfo":
            def boom(key):
                raise Exception(f"No time zone found with key {key}")
            return types.SimpleNamespace(ZoneInfo=boom)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "backend.market_hours", raising=False)
    reloaded = importlib.import_module("backend.market_hours")
    monkeypatch.setattr(builtins, "__import__", real_import)

    try:
        assert isinstance(reloaded.EASTERN, reloaded._USEasternFallback)
        moment = datetime(2026, 8, 31, 21, 47, tzinfo=timezone.utc)
        assert reloaded.session(moment) == reloaded.AFTER
    finally:
        # Leave the real module in place for everything else.
        sys.modules.pop("backend.market_hours", None)
        importlib.import_module("backend.market_hours")


@pytest.mark.parametrize("utc_iso,expected", [
    ("2026-01-15T20:00:00+00:00", "EST"),
    ("2026-03-07T18:00:00+00:00", "EST"),   # before the March transition
    ("2026-03-09T18:00:00+00:00", "EDT"),   # after it
    ("2026-08-31T21:47:00+00:00", "EDT"),
    ("2026-10-30T18:00:00+00:00", "EDT"),   # before the November transition
    ("2026-11-03T18:00:00+00:00", "EST"),   # after it
])
def test_fallback_matches_real_dst_transitions(utc_iso, expected):
    fallback = mh._USEasternFallback()
    moment = datetime.fromisoformat(utc_iso)
    assert moment.astimezone(fallback).tzname() == expected


def test_fallback_agrees_with_the_real_zone_on_session_boundaries():
    """The fallback must classify sessions identically to the IANA database."""
    fallback = mh._USEasternFallback()
    cursor = datetime(2026, 1, 1, tzinfo=timezone.utc)
    checked = 0
    while cursor < datetime(2027, 1, 1, tzinfo=timezone.utc):
        real = cursor.astimezone(mh.EASTERN)
        fake = cursor.astimezone(fallback)
        # 02:00 is the DST switch; it is outside every market session anyway.
        if real.hour != 2 and fake.hour != 2:
            assert real.utcoffset() == fake.utcoffset(), f"offset differs at {cursor}"
            checked += 1
        cursor += timedelta(hours=7)
    assert checked > 1000


# ---------------- sessions ----------------
@pytest.mark.parametrize("hour,minute,expected", [
    (3, 30, mh.CLOSED), (4, 0, mh.PRE), (9, 29, mh.PRE),
    (9, 30, mh.REGULAR), (15, 59, mh.REGULAR),
    (16, 0, mh.AFTER), (19, 59, mh.AFTER), (20, 0, mh.CLOSED),
])
def test_session_boundaries(hour, minute, expected):
    moment = datetime(2026, 8, 31, hour, minute, tzinfo=mh.EASTERN)
    assert mh.session(moment) == expected


def test_weekends_are_closed():
    assert mh.session(datetime(2026, 8, 29, 12, 0, tzinfo=mh.EASTERN)) == mh.CLOSED
    assert mh.session(datetime(2026, 8, 30, 12, 0, tzinfo=mh.EASTERN)) == mh.CLOSED


def test_extended_session_detection():
    assert mh.is_extended_session(datetime(2026, 8, 31, 17, 0, tzinfo=mh.EASTERN))
    assert mh.is_extended_session(datetime(2026, 8, 31, 7, 0, tzinfo=mh.EASTERN))
    assert not mh.is_extended_session(datetime(2026, 8, 31, 12, 0, tzinfo=mh.EASTERN))


# ---------------- NYSE calendar ----------------
def test_2026_holidays_match_the_published_calendar():
    expected = {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
    }
    assert mh.market_holidays(2026) == expected


def test_holidays_are_never_on_a_weekend():
    for year in (2024, 2025, 2026, 2027, 2028):
        assert all(d.weekday() < 5 for d in mh.market_holidays(year))


def test_good_friday_tracks_easter():
    for year in (2025, 2026, 2027):
        good_friday = mh.easter(year) - timedelta(days=2)
        assert good_friday.weekday() == 4
        assert good_friday in mh.market_holidays(year)


def test_christmas_is_closed_and_black_friday_closes_early():
    assert not mh.is_trading_day(date(2026, 12, 25))
    assert mh.regular_close_time(date(2026, 11, 27)) == mh.EARLY_CLOSE
    assert mh.regular_close_time(date(2026, 8, 31)) == mh.REGULAR_CLOSE


def test_early_close_shortens_the_regular_session():
    black_friday = date(2026, 11, 27)
    after = datetime(black_friday.year, black_friday.month, black_friday.day,
                     14, 0, tzinfo=mh.EASTERN)
    assert mh.session(after) == mh.AFTER, "14:00 is after a 13:00 early close"


def test_previous_trading_day_skips_weekends_and_holidays():
    assert mh.previous_trading_day(date(2026, 8, 31)) == date(2026, 8, 28)   # Mon -> Fri
    assert mh.previous_trading_day(date(2026, 9, 8)) == date(2026, 9, 4)     # after Labor Day


def test_describe_reports_a_complete_picture():
    info = mh.describe()
    assert set(info) >= {"session", "label", "is_open", "is_extended",
                         "eastern_time", "trading_day", "last_regular_close"}
    assert info["session"] in (mh.PRE, mh.REGULAR, mh.AFTER, mh.CLOSED)
    assert info["is_open"] == (info["session"] == mh.REGULAR)
