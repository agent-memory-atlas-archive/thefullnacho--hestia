"""NFC capture — the no-LLM logging path (nfc.py). A tag scan must never claim success without
a real write: the chat agent did exactly that on 2026-09-01 (silent tool-scoping miss), which
this endpoint exists to be immune to. Covers the pure logging helpers and the token/subject/kind
validation at the /nfc + /nfc/log routes."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest


# ----- nfc.py helpers: real writes, no FastAPI -------------------------------------------

def test_log_harvest_tag_writes_and_confirms(db):
    db.upsert_entity("place", "Bed 2")
    import nfc
    body, status = nfc.log_harvest_tag("Bed 2", "Zucchini", "7", "lb")
    assert status == 200
    assert "Logged 7 lb of Zucchini" in body
    rows = db.harvest_totals(bed="Bed 2")
    assert rows and rows[0]["crop"] == "Zucchini"


def test_log_harvest_tag_warns_on_new_bed(db):
    import nfc
    body, status = nfc.log_harvest_tag("Mystery Bed", "Kale", "2", "lb")
    assert status == 200
    assert "wasn't a known entity" in body  # loud, not silent


def test_log_harvest_tag_rejects_missing_crop(db):
    import nfc
    body, status = nfc.log_harvest_tag("Bed 2", "", "7", "lb")
    assert status == 400
    assert "Crop is required" in body


def test_log_harvest_tag_rejects_bad_qty(db):
    import nfc
    body, status = nfc.log_harvest_tag("Bed 2", "Zucchini", "not-a-number", "lb")
    assert status == 400
    body2, status2 = nfc.log_harvest_tag("Bed 2", "Zucchini", "-3", "lb")
    assert status2 == 400


def test_log_use_tag_writes_minutes(db):
    db.upsert_entity("asset", "Weedwhacker")
    import nfc
    body, status = nfc.log_use_tag("Weedwhacker", "45", "front + back yard")
    assert status == 200
    assert "Logged 45 min run" in body


def test_log_use_tag_rejects_bad_minutes(db):
    import nfc
    body, status = nfc.log_use_tag("Weedwhacker", "not-a-number", "")
    assert status == 400
    body2, status2 = nfc.log_use_tag("Weedwhacker", "0", "")
    assert status2 == 400


def test_log_use_tag_does_not_affect_due_assets(db):
    # 'use' is a metric log, not a service event — must never satisfy an interval_days reminder.
    db.upsert_entity("asset", "Weedwhacker", attrs={"interval_days": 30})
    import nfc
    nfc.log_use_tag("Weedwhacker", "45", "")
    assert any(a["name"] == "Weedwhacker" for a in db.due_assets())


def test_log_service_tag_resets_due_clock(db):
    db.upsert_entity("asset", "Furnace Filter", attrs={"interval_days": 90})
    import nfc
    # Overdue before any service is logged.
    assert any(a["name"] == "Furnace Filter" for a in db.due_assets())
    body, status = nfc.log_service_tag("Furnace Filter", "")
    assert status == 200
    assert "Logged service" in body
    assert not any(a["name"] == "Furnace Filter" for a in db.due_assets())


# ----- routes: token + validation, via FastAPI TestClient --------------------------------

@pytest.fixture
def client(monkeypatch, db):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    import hestia
    monkeypatch.setattr(hestia, "NFC_TOKEN", "test-token")
    return fastapi_testclient.TestClient(hestia.app)


def test_capture_form_rejects_bad_token(client):
    r = client.get("/nfc", params={"token": "wrong", "kind": "harvest", "subject": "Bed 2"})
    assert r.status_code == 401


def test_capture_form_requires_subject(client):
    r = client.get("/nfc", params={"token": "test-token", "kind": "harvest", "subject": ""})
    assert r.status_code == 400


def test_capture_form_renders_locked_subject(client):
    r = client.get("/nfc", params={"token": "test-token", "kind": "harvest", "subject": "Bed 2"})
    assert r.status_code == 200
    assert "Bed 2" in r.text
    assert 'action="/nfc/log"' in r.text


def test_log_route_end_to_end(client, db):
    db.upsert_entity("place", "Bed 2")
    r = client.post("/nfc/log", data={"token": "test-token", "kind": "harvest",
                                      "subject": "Bed 2", "crop": "Cucumber",
                                      "qty": "6", "unit": "lb"})
    assert r.status_code == 200
    assert "Logged 6 lb of Cucumber" in r.text
    assert db.harvest_totals(bed="Bed 2")


def test_log_route_bad_token(client):
    r = client.post("/nfc/log", data={"token": "nope", "kind": "harvest",
                                      "subject": "Bed 2", "crop": "X", "qty": "1", "unit": "lb"})
    assert r.status_code == 401


# ----- watering tags: one prefilled field between a wet hand and a logged run -------------

def test_log_watering_tag_writes_depth_and_shows_it_as_an_estimate(db):
    db.upsert_entity("place", "Back Fence")
    import nfc
    body, status = nfc.log_watering_tag("Back Fence", "15", "zone3", "hi-rise")
    assert status == 200
    assert "Logged 15 min watering" in body
    assert "0.4 in, 56.6 gal (estimated)" in body
    total = db.water_totals(place="Back Fence")[0]
    assert total["minutes"] == 15.0 and total["sources"] == ["zone3"]


def test_log_watering_tag_without_a_sprinkler_claims_no_depth(db):
    db.upsert_entity("place", "Raised Bed 2")
    import nfc
    body, status = nfc.log_watering_tag("Raised Bed 2", "20", "zone4")
    assert status == 200 and "estimated" not in body
    total = db.water_totals(place="Raised Bed 2")[0]
    assert total["minutes"] == 20.0 and total["unmeasured"] == 1


@pytest.mark.parametrize("minutes", ["", "soon", "0", "-5"])
def test_log_watering_tag_rejects_a_run_with_no_duration(db, minutes):
    import nfc
    body, status = nfc.log_watering_tag("Back Fence", minutes, "zone3", "hi-rise")
    assert status == 400 and not db.water_totals()


def test_log_watering_tag_refuses_a_sprinkler_it_has_no_rate_for(db):
    import nfc
    body, status = nfc.log_watering_tag("Back Fence", "15", "zone3", "impulse")
    # Better to refuse than to log a run with a silently dropped rate.
    assert status == 400 and "impulse" in body and not db.water_totals()


def test_log_watering_tag_warns_on_a_place_it_has_never_seen(db):
    import nfc
    body, status = nfc.log_watering_tag("Somewhere New", "15", "zone3", "hi-rise")
    assert status == 200 and "wasn't a known entity" in body


def test_watering_form_asks_only_for_minutes_when_the_tag_knows_its_sprinkler(client):
    r = client.get("/nfc", params={"token": "test-token", "kind": "watering",
                                   "subject": "Back Fence", "source": "zone3",
                                   "sprinkler": "hi-rise"})
    assert r.status_code == 200
    assert 'value="15"' in r.text and "Back Fence" in r.text
    assert '<select id="sprinkler"' not in r.text


def test_watering_form_offers_the_choice_when_the_tag_does_not_know(client):
    r = client.get("/nfc", params={"token": "test-token", "kind": "watering",
                                   "subject": "Back Fence"})
    assert '<select id="sprinkler"' in r.text
    assert "None (drip or soaker)" in r.text and "Hi-Rise" in r.text


def test_watering_form_rejects_a_tag_naming_an_unknown_sprinkler(client):
    r = client.get("/nfc", params={"token": "test-token", "kind": "watering",
                                   "subject": "Back Fence", "sprinkler": "impulse"})
    assert "unknown sprinkler" in r.text


def test_watering_log_route_end_to_end(client, db):
    db.upsert_entity("place", "Back Fence")
    r = client.post("/nfc/log", data={"token": "test-token", "kind": "watering",
                                      "subject": "Back Fence", "minutes": "15",
                                      "source": "zone3", "sprinkler": "hi-rise"})
    assert r.status_code == 200 and "Logged 15 min watering" in r.text
    assert db.water_totals(place="Back Fence")[0]["gallons"] == 56.6


def test_an_unknown_kind_still_names_the_kinds_that_work(client):
    r = client.get("/nfc", params={"token": "test-token", "kind": "flooding",
                                   "subject": "Back Fence"})
    assert "kind=watering" in r.text


# ----- the weekly photo, offered only when one is due ------------------------------------

@pytest.fixture
def photo_client(monkeypatch, tmp_path, db):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    import hestia
    monkeypatch.setattr(hestia, "NFC_TOKEN", "test-token")
    monkeypatch.setattr(hestia, "INGEST_TOKEN", "a-different-token")
    monkeypatch.setattr(hestia, "PHOTO_DIR", tmp_path / "photos")
    return fastapi_testclient.TestClient(hestia.app)


def _water(client, subject="Strawberries"):
    return client.post("/nfc/log", data={"token": "test-token", "kind": "watering",
                                         "subject": subject, "minutes": "15",
                                         "source": "zone3", "sprinkler": "hi-rise"})


def test_a_place_never_photographed_is_offered_the_camera(photo_client, db):
    db.upsert_entity("place", "Strawberries")
    body = _water(photo_client).text
    assert 'action="/nfc/photo"' in body and 'capture="environment"' in body
    assert "no photo of this one yet" in body


def test_a_place_photographed_today_is_left_alone(photo_client, db):
    db.upsert_entity("place", "Strawberries")
    db.attach_photo("Strawberries", "/tmp/x.jpg", None, "garden")
    body = _water(photo_client).text
    assert 'action="/nfc/photo"' not in body


@pytest.mark.parametrize("age_days,offered", [(6, False), (7, True), (30, True)])
def test_the_camera_returns_after_a_week(photo_client, db, age_days, offered):
    db.upsert_entity("place", "Strawberries")
    taken = dt.datetime.now() - dt.timedelta(days=age_days, minutes=1)
    db.log_event("photo", subject="Strawberries", action="photographed",
                 subject_kind="place", strict_subject=True,
                 ts=taken.isoformat(timespec="seconds"), attrs={"path": "/tmp/x.jpg"})
    assert ('action="/nfc/photo"' in _water(photo_client).text) is offered


def test_the_watering_page_never_carries_the_ingest_token(photo_client, db):
    db.upsert_entity("place", "Strawberries")
    # A stake in the ground is worth one credential, not two.
    assert "a-different-token" not in _water(photo_client).text


def test_a_tapped_photo_files_against_the_same_place(photo_client, db):
    db.upsert_entity("place", "Strawberries")
    r = photo_client.post("/nfc/photo",
                          data={"token": "test-token", "subject": "Strawberries"},
                          files={"file": ("shot.jpg", b"\xff\xd8ffdata", "image/jpeg")})
    assert r.status_code == 200 and "Photo filed" in r.text
    assert db.days_since_photo("Strawberries") is not None
    # And the offer goes away now that one has been taken.
    assert 'action="/nfc/photo"' not in _water(photo_client).text


def test_a_tapped_photo_needs_the_nfc_token(photo_client, db):
    r = photo_client.post("/nfc/photo",
                          data={"token": "a-different-token", "subject": "Strawberries"},
                          files={"file": ("shot.jpg", b"data", "image/jpeg")})
    assert r.status_code == 401 and db.last_photo("Strawberries") is None


def test_a_tapped_photo_with_nothing_attached_says_so(photo_client, db):
    r = photo_client.post("/nfc/photo", data={"token": "test-token", "subject": "Strawberries"})
    assert r.status_code == 400 and "No photo was attached" in r.text


def test_an_empty_upload_reports_failure_rather_than_looking_filed(photo_client, db):
    r = photo_client.post("/nfc/photo",
                          data={"token": "test-token", "subject": "Strawberries"},
                          files={"file": ("shot.jpg", b"", "image/jpeg")})
    assert r.status_code == 400 and "Photo filed" not in r.text
    assert db.last_photo("Strawberries") is None


def test_the_watering_run_is_written_even_when_the_photo_is_skipped(photo_client, db):
    db.upsert_entity("place", "Strawberries")
    _water(photo_client)
    # The offer is on the way out, never a gate in front of the thing being logged.
    assert db.water_totals(place="Strawberries")[0]["runs"] == 1


# ----- position slugs: what a stake tag actually carries ---------------------------------
# The long URL did not fit an NTAG213, so the tag carries a slug and the brain resolves it.
# These tests exist because the failure mode is a silent one: a slug that resolves to the
# wrong bed logs a real run against the wrong plant, outdoors, with no one checking.

@pytest.fixture
def positions(tmp_path, monkeypatch):
    import config
    import nfc
    path = tmp_path / "irrigation-positions.json"
    path.write_text(json.dumps([
        {"name": "Strawberries", "source": "zone3", "sprinkler": "hi-rise"},
        {"name": "Apple #1", "source": "zone3", "sprinkler": "hi-rise"},
        {"name": "Woodland Edge Guild", "source": "spigot", "sprinkler": "hi-rise"},
        {"name": "Pond", "source": "spigot"},
    ]))
    monkeypatch.setattr(config, "POSITIONS_DATA", path)
    return nfc


def test_slug_round_trips_every_awkward_name(positions):
    assert positions.position_slug("Apple #1") == "apple-1"
    assert positions.position_slug("Woodland Edge Guild") == "woodland-edge-guild"
    assert positions.position_slug("XMas Tree") == "xmas-tree"


def test_resolve_position_returns_the_whole_position(positions):
    assert positions.resolve_position("woodland-edge-guild") == {
        "subject": "Woodland Edge Guild", "source": "spigot", "sprinkler": "hi-rise"}


def test_resolve_position_keeps_a_missing_sprinkler_missing(positions):
    # No sprinkler means no rate, which means the run claims minutes and no depth.
    assert positions.resolve_position("pond")["sprinkler"] == ""


def test_resolve_position_refuses_an_unknown_slug(positions):
    assert positions.resolve_position("back-fence") is None
    assert positions.resolve_position("") is None


def test_resolve_position_survives_a_missing_positions_file(tmp_path, monkeypatch):
    import config
    import nfc
    monkeypatch.setattr(config, "POSITIONS_DATA", tmp_path / "gone.json")
    assert nfc.resolve_position("strawberries") is None


def test_stake_tag_fills_in_the_whole_form_from_one_slug(client, positions):
    r = client.get("/nfc", params={"token": "test-token", "p": "apple-1"})
    assert r.status_code == 200
    # The name is what records keys on, so the encoded form must come back exactly.
    assert "Apple #1" in r.text
    assert "zone3" in r.text and "hi-rise" in r.text


def test_unknown_slug_says_so_and_logs_nothing(client, positions):
    r = client.get("/nfc", params={"token": "test-token", "p": "back-fence"})
    assert r.status_code == 404
    assert "back-fence" in r.text
    assert "<form" not in r.text


def test_a_stake_tag_still_needs_the_token(client, positions):
    r = client.get("/nfc", params={"token": "wrong", "p": "apple-1"})
    assert r.status_code == 401


def test_the_long_form_asset_tags_still_work(client, positions):
    # Eight tags are already written and write-protected. They carry the spelled-out form.
    r = client.get("/nfc", params={"token": "test-token", "kind": "service",
                                  "subject": "Furnace Filter"})
    assert r.status_code == 200 and "Furnace Filter" in r.text


def test_the_generator_and_the_brain_agree_on_slugs(positions):
    # Two copies of one function: if they drift, every tag resolves to nothing.
    spec = importlib.util.spec_from_file_location(
        "make_tags", Path(__file__).resolve().parents[2] / "hardware" / "make_tags.py")
    make_tags = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_tags)
    for name in ("Strawberries", "Apple #1", "XMas Tree", "Woodland Edge Guild", "Rose of Sharon"):
        assert make_tags.slug(name) == positions.position_slug(name)


def test_every_real_stake_url_fits_an_ntag213(positions):
    spec = importlib.util.spec_from_file_location(
        "make_tags", Path(__file__).resolve().parents[2] / "hardware" / "make_tags.py")
    make_tags = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_tags)
    base = "http://alex-ms-7e51:8730"
    token = "4" * 48
    for name in ("Woodland Edge Guild", "Rose of Sharon", "Strawberries"):
        url = f"{base}/nfc?token={token}&p={make_tags.slug(name)}"
        assert len(url) <= make_tags.NDEF_SAFE_BYTES


# ----- rain readings: the cup's own job ---------------------------------------------------
# Rain is the inverse of a watering run: no duration, and the depth is the only fact. These
# tests pin the two things that would quietly corrupt a season — a measured number landing in
# the same column as a spec-sheet estimate, and a brim-full cup reading as a total.

def test_log_rain_writes_a_measured_depth(db):
    db.upsert_entity("place", "Meadow")
    r = db.log_rain("Meadow", 1.75)
    assert r.get("created") is False
    row = db.recent_events(kind="rain", limit=1)[0]
    assert row["subject"] == "Meadow"
    assert row["attrs"]["inches"] == 1.75
    assert row["attrs"]["mm"] == 44.4
    assert row["attrs"]["basis"] == "measured"


def test_rain_never_counts_as_a_watering_run(db):
    db.upsert_entity("place", "Meadow")
    db.log_rain("Meadow", 1.75)
    # The whole point of a separate kind: applied and fallen stay separate questions.
    assert db.water_totals(place="Meadow") == []
    assert db.rain_totals(place="Meadow")[0]["inches"] == 1.75


def test_rain_totals_sum_readings_per_place(db):
    db.upsert_entity("place", "Meadow")
    db.log_rain("Meadow", 1.75)
    db.log_rain("Meadow", 0.5)
    total = db.rain_totals(place="Meadow")[0]
    assert total["readings"] == 2 and total["inches"] == 2.25 and total["mm"] == 57.1


@pytest.mark.parametrize("depth", ["0", "-1", "", "wet"])
def test_rain_tag_rejects_a_depth_that_is_not_a_positive_number(db, depth):
    import nfc
    body, status = nfc.log_rain_tag("Meadow", depth)
    assert status == 400 and "Logged" not in body


def test_rain_tag_converts_mm_to_inches(db):
    db.upsert_entity("place", "Meadow")
    import nfc
    body, status = nfc.log_rain_tag("Meadow", "44.5", unit="mm")
    assert status == 200
    assert db.recent_events(kind="rain", limit=1)[0]["attrs"]["inches"] == 1.752


def test_rain_tag_refuses_more_than_the_cup_can_hold(db):
    # 3 inches is 76mm in a 50mm cup: the unit is wrong, or it was emptied mid-storm.
    import nfc
    body, status = nfc.log_rain_tag("Meadow", "3")
    assert status == 400 and "deeper than the cup" in body
    assert db.recent_events(kind="rain", limit=1) == []


def test_a_brim_full_cup_is_reported_as_a_floor(db):
    db.upsert_entity("place", "Meadow")
    import nfc
    body, status = nfc.log_rain_tag("Meadow", "50", unit="mm")
    assert status == 200
    assert "floor" in body and "or more" in body


def test_a_part_full_cup_is_not_hedged(db):
    db.upsert_entity("place", "Meadow")
    import nfc
    body, status = nfc.log_rain_tag("Meadow", "1.75")
    assert status == 200 and "floor" not in body


def test_the_stake_form_offers_rain_alongside_watering(client, positions):
    r = client.get("/nfc", params={"token": "test-token", "p": "strawberries"})
    assert r.status_code == 200
    # Two separate forms, so the two kinds can never be submitted together.
    assert r.text.count('name="kind"') == 2
    assert 'value="rain"' in r.text and 'value="watering"' in r.text


def test_rain_route_end_to_end(client, db):
    db.upsert_entity("place", "Meadow")
    r = client.post("/nfc/log", data={"token": "test-token", "kind": "rain",
                                     "subject": "Meadow", "depth": "1.75", "unit": "in"})
    assert r.status_code == 200 and "1.75 in of rain" in r.text
    assert db.rain_totals(place="Meadow")[0]["inches"] == 1.75


def test_rain_route_needs_the_token(client, db):
    r = client.post("/nfc/log", data={"token": "nope", "kind": "rain",
                                     "subject": "Meadow", "depth": "1.75"})
    assert r.status_code == 401
    assert db.recent_events(kind="rain", limit=1) == []
