"""Puppy watch — the neonatal weight curve and the thresholds that fire off it.

These tests exist because the failure mode is silent: a fading pup looks like a normal pup
until the number stops going up, and nobody is doing arithmetic at 3am. Each rule gets a
case that fires and a case that must not, because a watcher that cries wolf gets muted and
a muted watcher is the same as no watcher.
"""
from __future__ import annotations

import datetime as dt

import pytest

import puppy_watch


@pytest.fixture
def watch(tmp_path, monkeypatch, db):
    """puppy_watch pointed at the throwaway DB and a throwaway dedupe state file."""
    monkeypatch.setattr(puppy_watch, "STATE_PATH", tmp_path / "puppy_watch.json")
    monkeypatch.setattr(puppy_watch, "records_store", db)
    return puppy_watch


def born(db, name, weight="6.0 oz", day="2026-09-25"):
    return db.add_birth(name, dam="Lily", sire="Bodhi", born=f"{day}T02:00:00",
                        attrs={"weight": weight})


def weigh(db, name, oz, day):
    return db.log_weight(name, oz, unit="oz", ts=f"{day}T08:00:00")


def at(day, hour=18):
    return dt.datetime.fromisoformat(f"{day}T{hour:02d}:00:00")


# ----- the weight series ----------------------------------------------------

def test_birth_weight_is_day_zero_on_the_curve(db):
    born(db, "Pup One", weight="6.0 oz")
    weigh(db, "Pup One", 6.4, "2026-09-26")
    series = db.weight_series("Pup One")
    assert [r["source"] for r in series] == ["birth", "weighed"]
    assert series[0]["grams"] == pytest.approx(170.1, abs=0.1)


def test_legacy_free_text_weight_still_lands_on_the_curve(db):
    """Weights logged before log_weight existed were prose in a health event. They are
    parsed rather than ignored, so an existing litter isn't invisible to the watcher."""
    born(db, "Pup One", weight="6.0 oz")
    db.log_event("health", subject="Pup One", action="weighed", detail="6.5 oz, nursing well",
                 ts="2026-09-26T08:00:00")
    series = db.weight_series("Pup One")
    assert series[-1]["source"] == "text"
    assert series[-1]["grams"] == pytest.approx(184.3, abs=0.1)


def test_two_weighings_in_one_day_count_as_one_point(db, watch):
    """A re-weigh corrects the first reading; it is not a gain followed by a loss."""
    born(db, "Pup One", weight="6.0 oz")
    db.log_weight("Pup One", 6.4, unit="oz", ts="2026-09-26T08:00:00")
    db.log_weight("Pup One", 6.2, unit="oz", ts="2026-09-26T20:00:00")
    days = watch._daily(db.weight_series("Pup One"))
    assert [d["date"] for d in days] == ["2026-09-25", "2026-09-26"]
    assert days[-1]["grams"] == pytest.approx(175.8, abs=0.1)


def test_a_weight_needs_a_weight_unit(db):
    born(db, "Pup One")
    with pytest.raises(ValueError):
        db.log_weight("Pup One", 3, unit="each")


def test_compound_spoken_weight_parses(db):
    born(db, "Pup One")
    r = db.log_weight("Pup One", "1 lb 2 oz")
    assert r["grams"] == pytest.approx(510.3, abs=0.1)


# ----- the thresholds -------------------------------------------------------

def test_a_gaining_pup_weighed_today_says_nothing(db, watch):
    born(db, "Pup One", weight="6.0 oz")
    weigh(db, "Pup One", 6.4, "2026-09-26")
    weigh(db, "Pup One", 6.9, "2026-09-27")
    assert watch.build_alerts(now=at("2026-09-27")) == []


def test_a_loss_since_the_last_weighing_alerts(db, watch):
    born(db, "Pup One", weight="6.0 oz")
    weigh(db, "Pup One", 6.4, "2026-09-26")
    weigh(db, "Pup One", 6.1, "2026-09-27")
    assert any("LOST weight" in a for a in watch.build_alerts(now=at("2026-09-27")))


def test_flat_for_two_days_alerts_even_though_nothing_was_lost(db, watch):
    born(db, "Pup One", weight="6.0 oz")
    for day in ("2026-09-26", "2026-09-27", "2026-09-28"):
        weigh(db, "Pup One", 6.4, day)
    alerts = watch.build_alerts(now=at("2026-09-28"))
    assert any("has not gained in 2 days" in a for a in alerts)
    assert not any("LOST weight" in a for a in alerts)


def test_below_birth_weight_only_counts_after_the_regain_day(db, watch):
    """Pups dip in the first day or two. That dip is normal and must not alert."""
    born(db, "Pup One", weight="6.0 oz")
    weigh(db, "Pup One", 5.7, "2026-09-26")
    assert not any("birth weight" in a for a in watch.build_alerts(now=at("2026-09-26")))
    weigh(db, "Pup One", 5.7, "2026-09-28")
    assert any("birth weight" in a for a in watch.build_alerts(now=at("2026-09-28")))


def test_off_peak_drop_names_the_fading_pattern_and_the_vet(db, watch):
    born(db, "Pup One", weight="6.0 oz")
    weigh(db, "Pup One", 7.0, "2026-09-26")
    weigh(db, "Pup One", 6.2, "2026-09-27")
    alert = next(a for a in watch.build_alerts(now=at("2026-09-27")) if "below its best" in a)
    assert "fading-pup pattern" in alert and "Call the vet." in alert


def test_a_pup_never_weighed_is_reported_not_skipped(db, watch):
    born(db, "Pup One", weight=None)
    assert any("never been weighed" in a for a in watch.build_alerts(now=at("2026-09-26")))


def test_a_missing_weighing_waits_until_the_weigh_by_hour(db, watch):
    """Before the weigh-in hour it is just a morning that hasn't happened yet."""
    born(db, "Pup One", weight="6.0 oz")
    weigh(db, "Pup One", 6.4, "2026-09-26")
    assert watch.build_alerts(now=at("2026-09-27", hour=7)) == []
    assert any("not been weighed today" in a for a in watch.build_alerts(now=at("2026-09-27", hour=13)))


def test_a_real_problem_is_reported_regardless_of_the_hour(db, watch):
    """The weigh-in-hour grace applies only to the nag, never to a pup that lost weight."""
    born(db, "Pup One", weight="6.0 oz")
    weigh(db, "Pup One", 6.4, "2026-09-26")
    weigh(db, "Pup One", 6.0, "2026-09-27")
    assert any("LOST weight" in a for a in watch.build_alerts(now=at("2026-09-27", hour=6)))


# ----- litter selection + dedupe --------------------------------------------

def test_no_litter_means_silence(db, watch):
    """The timer is enabled before the whelp, so the quiet path is the normal path."""
    db.upsert_entity("pet", "Lily", attrs={"role": "dam"})
    assert watch.build_alerts(now=at("2026-09-27")) == []


def test_a_litter_past_the_watch_window_is_no_longer_watched(db, watch):
    born(db, "Pup One", weight="6.0 oz", day="2026-08-01")
    assert watch.build_alerts(now=at("2026-09-27")) == []


def test_only_the_newest_litter_is_watched(db, watch):
    db.add_birth("Old Pup", dam="Lily", sire="Bodhi", born="2025-12-25T02:00:00",
                 attrs={"weight": "6.0 oz"})
    born(db, "New Pup", weight="6.0 oz")
    alerts = watch.build_alerts(now=at("2026-09-26"))
    assert any("New Pup" in a for a in alerts)
    assert not any("Old Pup" in a for a in alerts)


def test_a_whelped_litter_with_no_pups_logged_says_so(db, watch):
    db.upsert_entity("litter", "Litter 2026-09-25 (Lily x Bodhi)",
                     attrs={"whelp_date": "2026-09-25", "dam": "Lily", "sire": "Bodhi"})
    alerts = watch.build_alerts(now=at("2026-09-26"))
    assert len(alerts) == 1 and "no puppies are logged" in alerts[0]


def test_the_same_alert_does_not_repeat_within_a_day(db, watch):
    born(db, "Pup One", weight="6.0 oz")
    weigh(db, "Pup One", 6.4, "2026-09-26")
    weigh(db, "Pup One", 6.0, "2026-09-27")
    first = watch.build_alerts(now=at("2026-09-27", hour=8), persist=True)
    assert first
    assert watch.build_alerts(now=at("2026-09-27", hour=20), persist=True) == []


def test_a_dry_run_does_not_consume_the_alert(db, watch):
    born(db, "Pup One", weight="6.0 oz")
    weigh(db, "Pup One", 6.4, "2026-09-26")
    weigh(db, "Pup One", 6.0, "2026-09-27")
    assert watch.build_alerts(now=at("2026-09-27"), persist=False)
    assert watch.build_alerts(now=at("2026-09-27"), persist=True)


def test_a_new_day_re_reports_a_continuing_problem(db, watch):
    born(db, "Pup One", weight="6.0 oz")
    weigh(db, "Pup One", 6.4, "2026-09-26")
    weigh(db, "Pup One", 6.0, "2026-09-27")
    watch.build_alerts(now=at("2026-09-27"), persist=True)
    weigh(db, "Pup One", 5.7, "2026-09-28")
    assert any("LOST weight" in a for a in watch.build_alerts(now=at("2026-09-28"), persist=True))
