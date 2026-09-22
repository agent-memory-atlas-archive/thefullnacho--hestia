"""Soil sensors that lie rather than read.

Two real failures this season motivate every test here. The Ecowitt gateway froze 2026-06-02
to 06-07 and Home Assistant carried the last value forward, so six beds reported one identical
number per day (including a literal 0.0%) and it read as perfectly steady soil. Separately,
Hot Peppers went unavailable 2026-08-15 and nobody noticed for a month, because `soil_beds`
skips what it cannot parse and the briefing simply said "all 5 beds fine" instead of six.

A frozen sensor is worse than a missing one: it is confidently wrong, and the plan is to let
these readings drive the B-hyve zone 4 valve.
"""
from __future__ import annotations

import datetime as dt

import pytest

import garden_watch


def _moisture(channel: int, bed: str, state):
    return {"entity_id": f"sensor.unknown_device_soilmoisture{channel}", "state": str(state),
            "attributes": {"friendly_name": f"{bed} Soil Moisture"}}


def _batt(channel: int, volts):
    return {"entity_id": f"sensor.unknown_device_soilbatt{channel}", "state": str(volts),
            "attributes": {"friendly_name": f"Garden Soil Sensors soilbatt{channel}"}}


BEDS = {1: "Beets", 2: "Carrots", 3: "Potatoes & Snow Peas",
        6: "Tomatoes", 7: "Hot Peppers", 8: "Artichoke & Sweet Pepper"}


@pytest.fixture
def soil(monkeypatch):
    """Drive the checker off fixed states and a fixed distinct-value count per entity."""
    def configure(states, distinct=None):
        monkeypatch.setattr(garden_watch, "_soil_states", lambda: states)
        monkeypatch.setattr(garden_watch, "_changed_recently",
                            lambda ids, hours: {i: (distinct or {}).get(i, 5) for i in ids})
    return configure


def _healthy():
    return ([_moisture(c, b, 40 + c) for c, b in BEDS.items()]
            + [_batt(c, 1.5) for c in BEDS])


def test_a_healthy_estate_says_nothing(soil):
    soil(_healthy())
    assert garden_watch.stale_sensors() == []


def test_an_unavailable_sensor_is_named(soil):
    states = _healthy()
    states[4] = _moisture(7, "Hot Peppers", "unavailable")
    soil(states)
    out = garden_watch.stale_sensors()
    assert len(out) == 1
    assert "Hot Peppers" in out[0] and "not reporting" in out[0]


def test_a_zero_reading_is_a_fault_not_dry_soil(soil):
    # June 2026: Artichoke and Potatoes sat at exactly 0.0 for 144 hours.
    states = _healthy()
    states[5] = _moisture(8, "Artichoke & Sweet Pepper", 0)
    soil(states)
    out = garden_watch.stale_sensors()
    assert len(out) == 1
    assert "0%" in out[0] and "not dry soil" in out[0]


def test_one_frozen_sensor_is_called_frozen(soil):
    states = _healthy()
    soil(states, distinct={"sensor.unknown_device_soilmoisture2": 1})
    out = garden_watch.stale_sensors()
    assert len(out) == 1
    assert "Carrots" in out[0] and "unchanged" in out[0]


def test_every_sensor_frozen_at_once_blames_the_gateway(soil):
    states = _healthy()
    soil(states, distinct={s["entity_id"]: 1 for s in states})
    out = garden_watch.stale_sensors()
    # One line about the gateway, not six about probes that are all fine.
    assert len(out) == 1
    assert "gateway" in out[0]
    assert not any(bed in out[0] for bed in BEDS.values())


def test_the_gateway_line_says_not_to_act_on_the_readings(soil):
    # The whole point: B-hyve zone 4 is meant to run off these numbers one day.
    states = _healthy()
    soil(states, distinct={s["entity_id"]: 1 for s in states})
    assert "nothing should act on them" in garden_watch.stale_sensors()[0]


def test_a_mixed_failure_is_not_blamed_on_the_gateway(soil):
    # Five frozen and one dead is not a clean gateway story, so name them individually.
    states = _healthy()
    states[4] = _moisture(7, "Hot Peppers", "unavailable")
    soil(states, distinct={s["entity_id"]: 1 for s in states})
    out = garden_watch.stale_sensors()
    assert not any("gateway" in line for line in out)
    assert any("Hot Peppers" in line for line in out)
    assert len(out) == 6


def test_a_low_battery_is_flagged_against_its_bed(soil):
    states = _healthy()
    states[-1] = _batt(8, 1.2)
    soil(states)
    out = garden_watch.stale_sensors()
    # soilbatt8 carries no bed in its own name; the channel is what ties it to one.
    assert len(out) == 1
    assert "Artichoke & Sweet Pepper" in out[0] and "1.2V" in out[0]


def test_a_healthy_battery_is_silent(soil):
    states = _healthy()
    states[-1] = _batt(8, 1.4)
    soil(states)
    assert garden_watch.stale_sensors() == []


def test_a_history_outage_never_invents_a_frozen_sensor(soil, monkeypatch):
    monkeypatch.setattr(garden_watch, "_soil_states", _healthy)
    def boom(ids, hours):
        raise RuntimeError("recorder unreachable")
    monkeypatch.setattr(garden_watch, "_changed_recently", boom)
    # Not knowing whether a value moved is not evidence that it did not.
    assert garden_watch.stale_sensors() == []


def test_no_sensors_at_all_is_silent_not_six_alerts(soil):
    soil([])
    assert garden_watch.stale_sensors() == []


def test_staleness_leads_the_garden_alerts(monkeypatch):
    # It has to come before "water these beds", because whether to water is only as good as
    # the readings it was decided from.
    import briefing
    monkeypatch.setattr(garden_watch, "build_alerts",
                        lambda persist=False: ["Soil sensor Hot Peppers is not reporting (unavailable)."])
    facts = briefing._garden_facts()
    assert facts and facts[0].startswith("ALERT (garden):")
    assert "Hot Peppers" in facts[0]


# ----- battery readings that are older than the fix ------------------------

def _aged(state: dict, hours: float) -> dict:
    """The same state, but last reported `hours` ago."""
    heard = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    return {**state, "last_reported": heard.isoformat()}


def test_a_fresh_low_battery_is_still_an_alert(soil):
    states = _healthy()
    states[-1] = _aged(_batt(8, 1.1), 2)
    soil(states)
    out = garden_watch.stale_sensors()
    assert len(out) == 1 and "battery low" in out[0] and "1.1V" in out[0]


def test_a_day_old_low_battery_is_not_acted_on(soil):
    """The cell may already have been swapped; the entity just hasn't reported since."""
    states = _healthy()
    states[-1] = _aged(_batt(8, 1.1), 30)
    soil(states)
    assert garden_watch.stale_sensors() == []


def test_batteries_that_stop_refreshing_are_reported_once(soil):
    states = [_moisture(c, b, 40 + c) for c, b in BEDS.items()]
    states += [_aged(_batt(c, 1.1), 60) for c in BEDS]
    soil(states)
    out = garden_watch.stale_sensors()
    assert len(out) == 1
    assert "have not refreshed in 60h" in out[0] and "paused" in out[0]


def test_a_battery_state_with_no_timestamp_is_trusted(soil):
    """Anything without `last_reported` is taken at face value rather than silently dropped."""
    states = _healthy()
    states[-1] = _batt(8, 1.1)
    soil(states)
    assert "battery low" in garden_watch.stale_sensors()[0]
