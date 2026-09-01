"""US equity market sessions, in America/New_York.

The app needs to know which session it is in for three reasons: to label a
price correctly (a 4:30pm print is an after-hours trade, not the close), to
decide whether a quote is stale or simply the last trade of a closed market,
and to avoid claiming the market is open on Thanksgiving.

Sessions (Eastern):
    pre-market   04:00 - 09:30
    regular      09:30 - 16:00
    after-hours  16:00 - 20:00
    closed       everything else, plus weekends and holidays

Holidays are computed from the NYSE rules rather than listed, so the calendar
stays correct in future years without maintenance.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone, tzinfo

log = logging.getLogger(__name__)


def _nth_weekday_of(year: int, month: int, weekday: int, n: int) -> date:
    """The nth `weekday` (0=Mon .. 6=Sun) of a month."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


class _USEasternFallback(tzinfo):
    """US Eastern time without the IANA database.

    Windows does not ship the IANA tz database, so `zoneinfo` there depends on
    the `tzdata` package being installed. `tzdata` is in requirements.txt, but
    this fallback means a missing or broken install degrades to a slightly less
    authoritative clock instead of preventing the app from starting at all.

    Implements the US rules in force since 2007: daylight time runs from the
    second Sunday in March to the first Sunday in November. Both transitions
    happen at 02:00 local, which is outside every market session (the earliest
    is pre-market at 04:00), so the ambiguous hour cannot affect how a session
    is classified.
    """

    _STD = timedelta(hours=-5)      # EST
    _DST = timedelta(hours=1)

    @staticmethod
    def _bounds(year: int) -> tuple[datetime, datetime]:
        start = _nth_weekday_of(year, 3, 6, 2)      # 2nd Sunday in March
        end = _nth_weekday_of(year, 11, 6, 1)       # 1st Sunday in November
        return (datetime(start.year, start.month, start.day, 2),
                datetime(end.year, end.month, end.day, 2))

    def utcoffset(self, dt):
        return self._STD + self.dst(dt)

    def dst(self, dt):
        if dt is None:
            return timedelta(0)
        naive = dt.replace(tzinfo=None)
        start, end = self._bounds(naive.year)
        return self._DST if start <= naive < end else timedelta(0)

    def tzname(self, dt):
        return "EDT" if self.dst(dt) else "EST"

    def __repr__(self):
        return "US/Eastern (built-in fallback)"


def _eastern_zone() -> tzinfo:
    """Prefer the real IANA zone; fall back to the built-in rules."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception as exc:                                    # noqa: BLE001
        log.warning(
            "IANA time zone data unavailable (%s). Using built-in US Eastern "
            "rules instead. Install the 'tzdata' package for the authoritative "
            "database: pip install tzdata", exc,
        )
        return _USEasternFallback()


EASTERN = _eastern_zone()

PRE_OPEN = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
AFTER_CLOSE = time(20, 0)
EARLY_CLOSE = time(13, 0)

# Session labels used across the API and UI.
PRE = "pre-market"
REGULAR = "regular"
AFTER = "after-hours"
CLOSED = "closed"


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _observed(day: date, shift_saturday_back: bool = True) -> date:
    """NYSE observance: Sunday holidays move to Monday, Saturday to Friday.

    New Year's Day is the exception - when it lands on a Saturday the exchange
    stays open on the preceding Friday.
    """
    if day.weekday() == 5:
        return day - timedelta(days=1) if shift_saturday_back else day
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def market_holidays(year: int) -> set[date]:
    """Full-day NYSE closures for a given year."""
    days = {
        _observed(date(year, 1, 1), shift_saturday_back=False),   # New Year's Day
        _nth_weekday(year, 1, 0, 3),                              # MLK Day
        _nth_weekday(year, 2, 0, 3),                              # Washington's Birthday
        easter(year) - timedelta(days=2),                         # Good Friday
        _last_weekday(year, 5, 0),                                # Memorial Day
        _observed(date(year, 7, 4)),                              # Independence Day
        _nth_weekday(year, 9, 0, 1),                              # Labor Day
        _nth_weekday(year, 11, 3, 4),                             # Thanksgiving
        _observed(date(year, 12, 25)),                            # Christmas
    }
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))                    # Juneteenth
    # A holiday shifted onto a weekend is not a closure at all.
    return {d for d in days if d.weekday() < 5}


def early_close_days(year: int) -> set[date]:
    """Sessions that end at 13:00 ET instead of 16:00."""
    days = set()
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    days.add(thanksgiving + timedelta(days=1))                    # Black Friday

    christmas_eve = date(year, 12, 24)
    if christmas_eve.weekday() < 5:
        days.add(christmas_eve)

    july_3 = date(year, 7, 3)
    if july_3.weekday() < 5 and _observed(date(year, 7, 4)) == date(year, 7, 4):
        days.add(july_3)

    return {d for d in days if d.weekday() < 5 and d not in market_holidays(year)}


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in market_holidays(day.year)


def regular_close_time(day: date) -> time:
    return EARLY_CLOSE if day in early_close_days(day.year) else REGULAR_CLOSE


def previous_trading_day(day: date) -> date:
    cursor = day - timedelta(days=1)
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def now_eastern(moment: datetime | None = None) -> datetime:
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(EASTERN)


def session(moment: datetime | None = None) -> str:
    """Which session the given instant falls in."""
    now = now_eastern(moment)
    if not is_trading_day(now.date()):
        return CLOSED

    clock = now.time()
    close = regular_close_time(now.date())
    if PRE_OPEN <= clock < REGULAR_OPEN:
        return PRE
    if REGULAR_OPEN <= clock < close:
        return REGULAR
    if close <= clock < AFTER_CLOSE:
        return AFTER
    return CLOSED


def is_extended_session(moment: datetime | None = None) -> bool:
    return session(moment) in (PRE, AFTER)


def session_label(moment: datetime | None = None) -> str:
    """Short human label for the header chip."""
    return {
        PRE: "pre-market",
        REGULAR: "market open",
        AFTER: "after hours",
        CLOSED: "market closed",
    }[session(moment)]


def last_regular_close(moment: datetime | None = None) -> datetime:
    """When the most recent regular session ended, as an aware datetime."""
    now = now_eastern(moment)
    today = now.date()
    if is_trading_day(today) and now.time() >= regular_close_time(today):
        day = today
    else:
        day = previous_trading_day(today)
    return datetime.combine(day, regular_close_time(day), tzinfo=EASTERN)


def latest_completed_session(moment: datetime | None = None) -> date:
    """The most recent trading day whose regular session has finished.

    This is the date of the newest daily bar that can exist. During a session
    the latest complete bar is still yesterday's, so daily history fetched
    earlier today is already current and does not need refetching.
    """
    now = now_eastern(moment)
    today = now.date()
    if is_trading_day(today) and now.time() >= regular_close_time(today):
        return today
    return previous_trading_day(today)


def describe(moment: datetime | None = None) -> dict:
    """Everything the API needs to tell the user where the clock is."""
    now = now_eastern(moment)
    current = session(now)
    return {
        "session": current,
        "label": session_label(now),
        "is_open": current == REGULAR,
        "is_extended": current in (PRE, AFTER),
        "eastern_time": now.isoformat(),
        "trading_day": is_trading_day(now.date()),
        "early_close": now.date() in early_close_days(now.year),
        "last_regular_close": last_regular_close(now).isoformat(),
    }
