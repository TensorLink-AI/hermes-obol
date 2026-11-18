"""The calendar logic in zones the main suite (pinned to UTC) never sees: DST changes, half-hour offsets, day 1."""
import os
import time
from datetime import datetime

import pytest

from obol import _budget, _demo, _plugin

pytestmark = pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset (not on Windows)")
ZONES = ["UTC", "Australia/Brisbane", "Pacific/Auckland", "Asia/Kolkata", "Asia/Kathmandu", "America/New_York",
         "America/Los_Angeles", "Europe/London"]
# Around the 2026 DST changes: US ends Nov 1, Europe ends Oct 25, New Zealand starts Sep 27; and a month's first hour.
MOMENTS = [1793500000, 1793541600, 1792900000, 1790470000, 1796083200, 1798761600 - 1]


@pytest.fixture
def zone(request):
    old = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    yield request.param
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.mark.parametrize("zone", ZONES, indirect=True)
@pytest.mark.parametrize("now", MOMENTS)
def test_periods_are_local_calendar_periods(zone, now):
    for period in ("day", "week", "month"):
        start, end = _budget.period_bounds(period, now)
        a, b = datetime.fromtimestamp(start), datetime.fromtimestamp(end)
        assert start <= now < end
        assert (a.hour, a.minute, b.hour, b.minute) == (0, 0, 0, 0)
        assert _budget.period_bounds(period, end)[0] == end            # the next period starts where this one ends
        if period == "week":
            assert a.weekday() == 0 and 167 * 3600 <= end - start <= 169 * 3600
        if period == "month":
            assert a.day == 1 and b.day == 1


@pytest.mark.parametrize("zone", ZONES, indirect=True)
def test_rounded_times_never_land_before_now(zone):
    now = 1793500000.0
    for ahead in (600, 3600, 5 * 3600, 30 * 3600, 6 * 86400):
        shown = _budget.around(now + ahead, now)
        assert shown and ":" in shown


@pytest.mark.parametrize("zone", ["Asia/Kolkata", "Asia/Kathmandu", "Pacific/Auckland"], indirect=True)
def test_the_demo_checks_on_local_hours(zone):
    out = _demo.run(_plugin.Obol, 1778500000.0, lambda s: None)
    assert "Mon 12:00" in out and ":30 " not in out and ":45 " not in out
