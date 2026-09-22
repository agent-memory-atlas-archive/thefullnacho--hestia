"""Rain barrels — the one freeze nudge a season that stops a full barrel from splitting.

A reminder that never fires loses a barrel; a reminder that fires every cold morning until
April gets swiped away before the freeze that matters. So the cases below pin both edges:
it speaks when the first freeze appears, once more the morning before, and then not again.
"""
from __future__ import annotations

import datetime as dt

import pytest

import garden_watch


@pytest.fixture
def watch(tmp_path, monkeypatch):
    monkeypatch.setattr(garden_watch, "STATE_PATH", str(tmp_path / "garden_watch.json"))
    return garden_watch


def forecast(start, lows):
    d0 = dt.date.fromisoformat(start)
    return [{"date": (d0 + dt.timedelta(days=i)).isoformat(), "lo": lo, "hi": lo + 20,
             "rain": 0.0, "pop": 0} for i, lo in enumerate(lows)]


def test_no_freeze_no_nudge(watch):
    rows = forecast("2026-10-20", [40, 38, 35, 36, 41, 44, 45])  # frost-ish, not a freeze
    assert watch.barrel_alert(rows, dt.date(2026, 10, 20), persist=True) is None


def test_first_sighting_then_the_eve_then_silence(watch):
    today = dt.date(2026, 10, 20)
    rows = forecast("2026-10-20", [40, 38, 36, 34, 30, 33, 35])  # freeze on Oct 24
    first = watch.barrel_alert(rows, today, persist=True)
    assert "drain both rain barrels" in first and "diverter" in first and "pergola" in first

    # Still in the forecast the next two mornings: already said, so quiet.
    for d in (21, 22):
        day = dt.date(2026, 10, d)
        assert watch.barrel_alert(forecast(day.isoformat(), [38, 36, 34, 30][d - 21:] + [40] * 5),
                                  day, persist=True) is None

    eve_day = dt.date(2026, 10, 23)
    assert watch.barrel_alert(forecast("2026-10-23", [34, 30, 33]), eve_day, persist=True)

    # Every later freeze that winter is about barrels already drained.
    later = dt.date(2026, 12, 10)
    assert watch.barrel_alert(forecast("2026-12-10", [20, 18]), later, persist=True) is None


def test_late_first_sighting_is_one_message_not_two(watch):
    """Freeze first appears tomorrow: the first nudge is already the eve nudge."""
    today = dt.date(2026, 10, 23)
    assert watch.barrel_alert(forecast("2026-10-23", [34, 30]), today, persist=True)
    assert watch.barrel_alert(forecast("2026-10-24", [30, 31]), dt.date(2026, 10, 24),
                              persist=True) is None


def test_next_season_speaks_again(watch):
    rows = forecast("2026-10-20", [30])
    assert watch.barrel_alert(rows, dt.date(2026, 10, 20), persist=True)
    assert watch.barrel_alert(forecast("2027-10-18", [31]), dt.date(2027, 10, 18), persist=True)


def test_dry_run_does_not_use_up_the_nudge(watch):
    rows = forecast("2026-10-20", [40, 30])
    assert watch.barrel_alert(rows, dt.date(2026, 10, 20), persist=False)
    assert watch.barrel_alert(rows, dt.date(2026, 10, 20), persist=True)


def test_spring_freeze_belongs_to_the_season_that_started_in_the_fall(watch):
    assert watch._season(dt.date(2026, 10, 1)) == 2026
    assert watch._season(dt.date(2027, 4, 15)) == 2026
