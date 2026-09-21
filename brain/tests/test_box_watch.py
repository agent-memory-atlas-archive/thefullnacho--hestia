"""Box watch — whelping-box temperature bands and the alert state machine.

The failure this guards against is a lamp that cooks or a pad that dies while everyone is
asleep, and the second failure is an alert that cries wolf until it gets muted. So every rule
has a case that fires and a case that must not.
"""
from __future__ import annotations

import datetime as dt

import pytest

import box_watch

TZ = dt.timezone(dt.timedelta(hours=-4))


def at(day, hour=3, minute=0):
    return dt.datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=TZ)


def ha(value, heard, unit="°F"):
    return {"state": str(value), "last_reported": heard.isoformat(),
            "attributes": {"unit_of_measurement": unit}}


@pytest.fixture
def watch(tmp_path, monkeypatch, db):
    monkeypatch.setattr(box_watch, "STATE_PATH", tmp_path / "box_watch.json")
    monkeypatch.setattr(box_watch, "records_store", db)
    return box_watch


def whelp(db, day="2026-09-25"):
    db.add_birth("Pup One", dam="Lily", sire="Bodhi", born=f"{day}T02:00:00",
                 attrs={"weight": "6.0 oz"})


# ----- bands and readings ---------------------------------------------------

def test_bands_step_down_by_litter_day():
    assert box_watch.band(0) == (82.0, 93.0)
    assert box_watch.band(7) == (82.0, 93.0)
    assert box_watch.band(8) == (75.0, 88.0)
    assert box_watch.band(35) == (68.0, 83.0)
    assert box_watch.band(36) is None


def test_reading_in_celsius_is_converted():
    now = at("2026-09-25")
    assert box_watch.box_temp_f(ha(32.0, now, unit="°C"), now) == pytest.approx(89.6)


def test_stale_or_blank_reading_is_no_reading():
    now = at("2026-09-25")
    assert box_watch.box_temp_f(ha(90, now - dt.timedelta(minutes=11)), now) is None
    assert box_watch.box_temp_f({"state": "unknown"}, now) is None
    assert box_watch.box_temp_f(None, now) is None
    assert box_watch.box_temp_f(ha(90, now - dt.timedelta(minutes=2)), now) == 90.0


# ----- the state machine ----------------------------------------------------

def test_brief_excursion_does_not_push(watch):
    t0 = at("2026-09-25")
    msg, s = watch.step({}, "hot", 95.0, 82, 93, 0, t0)
    assert msg is None
    msg, s = watch.step(s, "hot", 95.0, 82, 93, 0, t0 + dt.timedelta(minutes=4))
    assert msg is None
    msg, s = watch.step(s, "ok", 90.0, 82, 93, 0, t0 + dt.timedelta(minutes=6))
    assert msg is None  # never announced, so no "back in range" either


def test_sustained_heat_pushes_then_repeats_then_recovers(watch):
    t0 = at("2026-09-25")
    _, s = watch.step({}, "hot", 95.0, 82, 93, 0, t0)
    msg, s = watch.step(s, "hot", 95.0, 82, 93, 0, t0 + dt.timedelta(minutes=5))
    assert "above 93" in msg and "heat lamp" in msg
    msg, s = watch.step(s, "hot", 95.0, 82, 93, 0, t0 + dt.timedelta(minutes=20))
    assert msg is None
    msg, s = watch.step(s, "hot", 95.0, 82, 93, 0, t0 + dt.timedelta(minutes=36))
    assert "above 93" in msg
    msg, s = watch.step(s, "ok", 90.0, 82, 93, 0, t0 + dt.timedelta(minutes=40))
    assert "back in range" in msg


def test_cold_pushes_after_sustain(watch):
    t0 = at("2026-09-25")
    _, s = watch.step({}, "cold", 78.0, 82, 93, 1, t0)
    msg, _ = watch.step(s, "cold", 78.0, 82, 93, 1, t0 + dt.timedelta(minutes=6))
    assert "below 82" in msg and "pad" in msg


def test_silent_sensor_pushes_on_sight(watch):
    """The silence already lasted SILENT_MIN before it could be seen; no second wait."""
    msg, _ = watch.step({"status": "ok", "since": at("2026-09-25").isoformat()},
                        "silent", None, 82, 93, 0, at("2026-09-25", 4))
    assert "No reading" in msg


# ----- the whole run --------------------------------------------------------

def test_no_litter_means_no_alert_and_no_fetch(watch):
    def fetch():
        raise AssertionError("must not read HA with no litter to watch")
    assert watch.run(now=at("2026-09-21"), fetch=fetch) is None


def test_litter_past_the_last_band_is_not_watched(watch, db):
    whelp(db, day="2026-08-01")
    assert watch.run(now=at("2026-09-21"), fetch=lambda: None) is None


def test_whelped_litter_arms_the_watch(watch, db):
    whelp(db)
    t0 = at("2026-09-25", 3)
    cold = lambda: ha(74.0, t0)  # noqa: E731 — a pre-whelp room temperature, lamp still off
    assert watch.run(now=t0, fetch=cold) is None
    t1 = t0 + dt.timedelta(minutes=6)
    msg = watch.run(now=t1, fetch=lambda: ha(74.0, t1))
    assert "below 82" in msg and "day 0" in msg
