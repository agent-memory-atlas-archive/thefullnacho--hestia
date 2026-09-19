"""Whelp capture form — the no-model path for logging a birth and a daily weight.

The point of this surface is that it cannot lie about what it wrote, so the tests are mostly
about exactly that: a write happens, or a readable refusal does, and never a page that looks
like success over an empty table.
"""
from __future__ import annotations

import datetime as dt

import pytest

import whelp_form


@pytest.fixture
def client(monkeypatch, tmp_path, db):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    import hestia
    monkeypatch.setattr(hestia, "NFC_TOKEN", "test-token")
    monkeypatch.setattr(hestia, "WHELP_DAM", "Lily")
    monkeypatch.setattr(hestia, "WHELP_SIRE", "Bodhi")
    return fastapi_testclient.TestClient(hestia.app)


def _born(client, collar="Red", oz="6.0", sex="male"):
    return client.post("/whelp/born", data={"token": "test-token", "collar": collar,
                                            "oz": oz, "sex": sex})


def _weigh(client, pup, oz):
    return client.post("/whelp/weigh", data={"token": "test-token", "pup": pup, "oz": oz})


# ----- the token is the whole access control --------------------------------

@pytest.mark.parametrize("path", ["/whelp", "/whelp/weigh", "/whelp/born"])
def test_every_whelp_route_refuses_a_bad_token(client, path):
    get = client.get(path, params={"token": "wrong"})
    post = client.post(path, data={"token": "wrong"})
    assert 401 in (get.status_code, post.status_code)


def test_the_board_shares_the_nfc_token_not_a_second_one(client, monkeypatch, db):
    import hestia
    monkeypatch.setattr(hestia, "INGEST_TOKEN", "a-different-token")
    body = client.get("/whelp", params={"token": "test-token"}).text
    assert "a-different-token" not in body


# ----- before the whelp -----------------------------------------------------

def test_an_empty_board_still_offers_the_first_pup(client, db):
    body = client.get("/whelp", params={"token": "test-token"}).text
    assert "No litter yet" in body and "First pup" in body
    assert "Lily" in body and "Bodhi" in body


# ----- recording a birth ----------------------------------------------------

def test_a_birth_creates_the_pup_the_litter_and_the_lineage(client, db):
    r = _born(client, collar="Red", oz="6.0")
    assert r.status_code == 200 and "Red pup recorded" in r.text
    pup = db.entity_profile("Red")
    assert pup["attrs"]["collar"] == "Red" and pup["attrs"]["sex"] == "male"
    assert {x["rel"] for x in pup["relations"]} >= {"dam", "sire"}
    assert db.entity_profile("Lily")["puppies_total"] == 1


def test_the_birth_weight_is_day_zero_on_the_curve(client, db):
    _born(client, collar="Red", oz="6.0")
    series = db.weight_series("Red")
    assert series and series[0]["source"] == "birth"
    assert series[0]["grams"] == pytest.approx(170.1, abs=0.1)


def test_a_birth_without_a_weight_is_still_recorded(client, db):
    """A pup that arrives while your hands are full gets logged now and weighed after."""
    r = _born(client, collar="Blue", oz="")
    assert r.status_code == 200
    assert db.resolve("Blue", kind="pet") is not None


def test_an_unknown_collar_colour_is_refused_not_invented(client, db):
    r = _born(client, collar="Teal")
    assert r.status_code == 400 and "collar colours" in r.text
    assert db.resolve("Teal", kind="pet") is None


def test_a_second_litter_reusing_a_colour_does_not_share_the_first_pups_weights(db):
    """Two pups quietly sharing one row of weights is the worst outcome here: the curve
    would look fine while one of them was fading."""
    db.add_birth("Red", dam="Lily", sire="Bodhi", born="2025-12-25T02:00:00")
    assert whelp_form.pup_name("Red", "2026-09-25") == "Red (2026-09-25)"


def test_collar_names_normalise_but_do_not_guess(db):
    assert whelp_form.collar_name(" baby blue ") == "Baby Blue"
    assert whelp_form.collar_name("BLUE") == "Blue"
    assert whelp_form.collar_name("lt blue") is None
    assert whelp_form.collar_name("") is None


# ----- weighing -------------------------------------------------------------

def test_a_weight_is_written_and_the_reply_says_the_number(client, db):
    _born(client, collar="Red", oz="6.0")
    r = _weigh(client, "Red", "6.4")
    assert r.status_code == 200 and "6.4 oz" in r.text
    assert db.weight_series("Red")[-1]["grams"] == pytest.approx(181.4, abs=0.1)


def test_the_reply_names_the_direction_without_judging_it(client, db):
    _born(client, collar="Red", oz="6.0")
    _weigh(client, "Red", "6.4")
    down = _weigh(client, "Red", "6.0")
    assert "Down 0.4 oz" in down.text and "Watch it nurse" in down.text


def test_a_junk_weight_is_refused_and_nothing_is_written(client, db):
    _born(client, collar="Red", oz="6.0")
    before = len(db.weight_series("Red"))
    r = _weigh(client, "Red", "heavy")
    assert r.status_code == 400 and "ounces" in r.text
    assert len(db.weight_series("Red")) == before


def test_a_weight_with_no_puppy_named_is_refused(client, db):
    r = client.post("/whelp/weigh", data={"token": "test-token", "oz": "6.4"})
    assert r.status_code == 400


# ----- the board once pups exist --------------------------------------------

def test_the_board_lists_each_pup_with_its_last_weight(client, db):
    _born(client, collar="Red", oz="6.0")
    _born(client, collar="Blue", oz="5.5")
    _weigh(client, "Red", "6.4")
    body = client.get("/whelp", params={"token": "test-token"}).text
    assert "Red" in body and "Blue" in body
    assert "6.4 oz" in body and body.count('action="/whelp/weigh"') == 2


def test_a_pup_never_weighed_says_so_on_the_board(client, db):
    # Dated relative to today: the board reads the real clock, so a hard-coded date makes the
    # test pass or fail depending on when it is run.
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    db.add_birth("Red", dam="Lily", sire="Bodhi", born=f"{yesterday}T02:00:00")
    body = client.get("/whelp", params={"token": "test-token"}).text
    assert "never weighed" in body


def test_the_board_and_the_watcher_agree_on_the_live_litter(db):
    """Different windows would mean the page shows a pup the alerts have stopped watching."""
    import puppy_watch
    assert whelp_form.WATCH_DAYS == puppy_watch.WATCH_DAYS
