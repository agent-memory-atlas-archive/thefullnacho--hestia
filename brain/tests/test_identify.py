"""Species ID from a photo: the classifiers decide, the page only reports, nothing is written."""
from __future__ import annotations

import dataclasses
import io
from pathlib import Path

import pytest

import identify


@dataclasses.dataclass
class FakeResult:
    domain: str = "plant"
    species: str = "unknown"
    scientific_name: str = "N/A"
    confidence: float = 0.0
    safety: str = "UNKNOWN"
    lookalike: str = "N/A"
    key_diff: str = ""
    low_confidence: bool = True
    expert_model: str = "none"
    abstained: bool = True
    reason: str = ""
    energy: float | None = None


def _jpeg(size=(32, 32), gps=False) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    img = Image.new("RGB", size, (40, 120, 40))
    if gps:
        exif = Image.Exif()
        exif[0x8825] = {2: (40.0, 26.0, 46.0), 4: (79.0, 58.0, 56.0)}  # GPSInfo
        img.save(buf, format="JPEG", exif=exif)
    else:
        img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def fake(monkeypatch):
    """Stand in for the classifiers: `fake.result` is what the next call returns."""
    class Session:
        result = FakeResult()
        seen: list = []

        def resolve_session(self, pipe, images):
            self.seen.append(len(images))
            return self.result

    session = Session()
    monkeypatch.setattr(identify, "_loaded", {"session": session, "pipe": object()})
    return session


@pytest.fixture
def photo_dir(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "PHOTO_DIR", tmp_path / "photos")
    return tmp_path / "photos"


@pytest.fixture
def client(db, photo_dir):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    import hestia
    return fastapi_testclient.TestClient(hestia.app)


def test_form_page_offers_a_photo_and_a_second_angle(client):
    r = client.get("/identify")
    assert r.status_code == 200
    assert r.text.count('name="file"') == 2
    assert "Second angle" in r.text


def test_deadly_is_loud_from_one_photo(client, fake):
    fake.result = FakeResult(species="poison_hemlock_deadly", scientific_name="Conium maculatum",
                             confidence=0.78, safety="DEADLY", abstained=False,
                             key_diff="Purple-blotched hollow stem")
    r = client.post("/identify", files={"file": ("a.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 200
    assert "DEADLY: Poison hemlock" in r.text
    assert "Conium maculatum" in r.text


def test_an_edible_lean_from_one_photo_asks_for_a_second(client, fake):
    fake.result = FakeResult(reason="need_second_photo", confidence=0.63,
                             key_diff="Leaning blackberry common, but an edible call needs a second photo")
    r = client.post("/identify", files={"file": ("a.jpg", _jpeg(), "image/jpeg")})
    assert "Not sure" in r.text
    assert "second photo from a different angle" in r.text
    assert "Edible" not in r.text


def test_an_edible_call_says_to_check_the_lookalike(client, fake):
    fake.result = FakeResult(species="blackberry_common", scientific_name="Rubus allegheniensis",
                             confidence=0.91, safety="SAFE", abstained=False, low_confidence=False,
                             lookalike="Pokeweed (young)", key_diff="Pokeweed has smooth stems")
    r = client.post("/identify", files=[("file", ("a.jpg", _jpeg(), "image/jpeg")),
                                        ("file", ("b.jpg", _jpeg(), "image/jpeg"))])
    assert "Blackberry common" in r.text and "Edible" in r.text
    assert "Lookalike: Pokeweed (young)" in r.text
    assert "from 2 photos" in r.text


def test_an_untouched_second_picker_is_not_a_photo(client, fake):
    r = client.post("/v1/identify", files=[("file", ("a.jpg", _jpeg(), "image/jpeg")),
                                           ("file", ("", b"", "application/octet-stream"))])
    assert r.status_code == 200
    assert r.json()["photos"] == 1
    assert fake.seen == [1]


@pytest.mark.parametrize("files, why", [
    ([], "no photo"),
    ([("file", ("a.txt", b"not an image", "text/plain"))], "isn't a photo"),
    ([("file", (f"{i}.jpg", _jpeg(), "image/jpeg")) for i in range(3)], "at most 2"),
])
def test_bad_input_is_a_400_with_a_reason(client, fake, files, why):
    r = client.post("/v1/identify", files=files) if files else client.post("/v1/identify", data={"x": "1"})
    assert r.status_code == 400
    assert why in r.json()["error"]


def test_no_two_photo_rule_means_no_identifying(client, monkeypatch, tmp_path):
    monkeypatch.setattr(identify, "_loaded", {})
    monkeypatch.setattr(identify, "STATION", tmp_path)
    r = client.post("/v1/identify", files={"file": ("a.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 503
    assert "two-photo rule" in r.json()["error"]


_VAL = Path.home() / "Documents/Forager/forager_ml/medicinals_dataset_split/val/poison_hemlock_deadly"


@pytest.mark.skipif(not (identify.STATION / "pipeline/session.py").is_file() or not _VAL.is_dir(),
                    reason="needs the field station checkout and forager_ml val photos")
def test_real_models_never_call_a_hemlock_edible(monkeypatch):
    monkeypatch.setattr(identify, "_loaded", {})
    for photo in sorted(_VAL.iterdir())[:5]:
        r = identify.identify([photo.read_bytes()])
        assert r["safety"] not in ("SAFE", "CAUTION"), (photo.name, r)


# ----- keeping sightings and labeling them ---------------------------------------------------

LEAN = FakeResult(reason="need_second_photo", confidence=0.78,
                  key_diff="Leaning motherwort, but an edible call needs a second photo")


def test_the_page_keeps_a_sighting_upright_small_and_without_gps(client, fake, photo_dir):
    from PIL import Image
    import sightings_store
    fake.result = LEAN
    r = client.post("/identify", files={"file": ("a.jpg", _jpeg((3000, 2000), gps=True), "image/jpeg")})
    assert "What was it really?" in r.text
    assert "not sure (leaning motherwort)" in r.text
    (s,) = sightings_store.recent()
    assert s["label"] is None and s["result"]["reason"] == "need_second_photo"
    kept = Image.open(photo_dir / s["photos"][0])
    assert max(kept.size) == 1600
    assert not kept.getexif().get_ifd(0x8825)


def test_the_json_route_keeps_nothing(client, fake, photo_dir):
    import sightings_store
    client.post("/v1/identify", files={"file": ("a.jpg", _jpeg(), "image/jpeg")})
    assert sightings_store.recent() == []
    assert not photo_dir.exists()


def test_a_label_sticks_and_a_relabel_replaces_it(client, fake):
    import sightings_store
    fake.result = LEAN
    client.post("/identify", files={"file": ("a.jpg", _jpeg(), "image/jpeg")})
    sid = sightings_store.recent()[0]["id"]
    r = client.post(f"/identify/{sid}/label", data={"label": "  cilantro  "})
    assert "Labeled: cilantro" in r.text
    client.post(f"/identify/{sid}/label", data={"label": "parsley"})
    assert sightings_store.get(sid)["label"] == "parsley"


@pytest.mark.parametrize("sid, label", [(1, "   "), (999, "cilantro")])
def test_an_empty_label_or_unknown_sighting_is_refused(client, fake, sid, label):
    fake.result = LEAN
    client.post("/identify", files={"file": ("a.jpg", _jpeg(), "image/jpeg")})
    assert client.post(f"/identify/{sid}/label", data={"label": label}).status_code == 400


def test_review_lists_unlabeled_first_and_serves_their_photos(client, fake):
    import sightings_store
    fake.result = LEAN
    for _ in range(2):
        client.post("/identify", files={"file": ("a.jpg", _jpeg(), "image/jpeg")})
    sightings_store.label(1, "cilantro")
    page = client.get("/identify/review").text
    assert "1 to label" in page
    assert page.index("#2 &middot;") < page.index("#1 &middot;")
    assert client.get("/identify/photo/2/1").headers["content-type"] == "image/jpeg"
    assert client.get("/identify/photo/2/2").status_code == 404


def test_a_photo_path_outside_the_kept_folder_is_never_served(client, db, photo_dir):
    import sightings_store
    (photo_dir / "pets").mkdir(parents=True)
    (photo_dir / "pets" / "x.jpg").write_bytes(_jpeg())
    sid = sightings_store.add(["identify/../pets/x.jpg"], {})
    assert client.get(f"/identify/photo/{sid}/1").status_code == 404
