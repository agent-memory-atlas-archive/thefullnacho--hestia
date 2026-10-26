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


def _jpeg() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (40, 120, 40)).save(buf, format="JPEG")
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
def client(db):
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
