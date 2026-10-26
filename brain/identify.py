"""Plant, berry and mushroom ID from a photo, with no model in the loop.

The forager_ml classifiers do the identifying: a domain router (berry / mushroom / plant /
other) hands the photo to the one expert that owns that domain, on CPU through onnxruntime.
A general vision model cannot do this job. Gemma 4 named 14 of 130 deadly photos as a safe
species and scored 16 to 37% where the experts score 88 to 96% (benchmarks/RESULTS.md,
2026-10-25), so species ID is a classifier's work, the same way a due date is a timer's.

The pipeline is the field station's own package, loaded in place from its checkout rather
than copied here: the safety metadata (which species is DEADLY) and the two-photo rule (never
an edible verdict on one photo's word) must keep one source. If that checkout or its
`session.py` is missing, this refuses to identify rather than run without the rule.

Nothing is written and nothing leaves the box. Keeping photos and their confirmed labels as
a local dataset is the next step, not this one.
"""
from __future__ import annotations

import dataclasses
import html
import importlib
import importlib.util
import io
import os
import sys
import threading
from pathlib import Path

from nfc import page

STATION = Path(os.environ.get("HESTIA_FORAGER_STATION")
               or Path.home() / "Documents/Forager/forager-field-station")
# The two-photo rule fuses up to this many angles of one specimen. Two is what it asks for.
MAX_PHOTOS = 2

_lock = threading.Lock()
_loaded: dict = {}


class Unavailable(RuntimeError):
    """The classifiers can't be loaded, so nothing gets identified."""


def _load() -> dict:
    with _lock:
        if _loaded:
            return _loaded
        pkg = STATION / "pipeline"
        if not (pkg / "session.py").is_file():
            raise Unavailable(f"the field station pipeline with its two-photo rule is not at {pkg}")
        # A unique package name, so the station's generic `pipeline` can't collide with
        # anything on the brain's flat import path.
        spec = importlib.util.spec_from_file_location(
            "forager_pipeline", pkg / "__init__.py", submodule_search_locations=[str(pkg)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["forager_pipeline"] = mod
        spec.loader.exec_module(mod)
        session = importlib.import_module("forager_pipeline.session")
        infer = importlib.import_module("forager_pipeline.infer")
        try:
            pipe = infer.Pipeline(str(STATION / "models"))
        except Exception as e:  # noqa: BLE001 — a missing ONNX file is a setup problem, say so
            raise Unavailable(f"the classifier models would not load: {e}") from e
        _loaded.update(session=session, pipe=pipe)
        return _loaded


def identify(photos: list[bytes]) -> dict:
    """Identify one specimen from one or two photos. Raises Unavailable or ValueError."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not photos:
        raise ValueError("no photo")
    if len(photos) > MAX_PHOTOS:
        raise ValueError(f"at most {MAX_PHOTOS} photos of one specimen")
    images = []
    for data in photos:
        try:
            # Phones store rotation in EXIF; the classifier must see the photo upright.
            images.append(ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB"))
        except (UnidentifiedImageError, OSError) as e:
            raise ValueError("that file isn't a photo this can read (JPEG or PNG works)") from e
    state = _load()
    result = state["session"].resolve_session(state["pipe"], images)
    return {**dataclasses.asdict(result), "photos": len(images)}


def _name(key: str) -> str:
    return key.replace("_", " ").removesuffix(" toxic").removesuffix(" deadly").capitalize()


def _form(note: str = "") -> str:
    return f"""
    {note}
    <form method="post" action="/identify" enctype="multipart/form-data">
      <input id="p1" name="file" type="file" accept="image/*" capture="environment" class="filein">
      <label class="filebtn" id="b1" for="p1">&#128247; Photo of the specimen</label>
      <input id="p2" name="file" type="file" accept="image/*" capture="environment" class="filein">
      <label class="filebtn" id="b2" for="p2">&#128247; Second angle (optional)</label>
      <div class="meta">A second photo from a different angle is what lets it name an edible.</div>
      <button type="submit">Identify</button>
    </form>
    <script>
    [['p1', 'b1'], ['p2', 'b2']].forEach(function (ids) {{
      var input = document.getElementById(ids[0]), btn = document.getElementById(ids[1]);
      var label = btn.textContent;
      input.addEventListener('change', function () {{
        var f = input.files && input.files[0];
        btn.textContent = f ? '✓ ' + f.name : label;
        btn.classList.toggle('chosen', !!f);
      }});
    }});
    </script>"""


def form_page() -> str:
    return page(f"<h1>What is this?</h1>"
                f'<div class="subject">Berries, mushrooms and wild plants</div>{_form()}',
                title="Identify")


def result_page(r: dict) -> str:
    """Render a result. DEADLY is loud, an edible call says what to check, a refusal says why."""
    esc = html.escape
    sci = f'<div class="meta"><em>{esc(r["scientific_name"])}</em></div>'
    look = (f'<div class="meta">Lookalike: {esc(r["lookalike"])}</div>'
            if r["lookalike"] not in ("", "N/A") else "")
    diff = f'<div class="meta">{esc(r["key_diff"])}</div>'
    conf = f'{r["confidence"]:.0%} confident from {r["photos"]} photo{"s" if r["photos"] > 1 else ""}'
    if r["safety"] == "DEADLY" and not r["abstained"]:
        body = (f'<div class="err"><strong>&#9888; DEADLY: {esc(_name(r["species"]))}</strong>'
                f'<br>Do not eat or handle without care.</div>{sci}{diff}<div class="meta">{conf}</div>')
    elif r["abstained"]:
        again = "Add a second photo from a different angle." if r["reason"] == "need_second_photo" \
            else "Try a closer, well-lit photo of the fruit, cap or leaves."
        body = (f'<div class="big">&#129300;</div><div><strong>Not sure</strong></div>{diff}'
                f'<div class="meta">{esc(again)}</div>')
    else:
        tier = "Edible" if r["safety"] == "SAFE" else "Edible with care"
        body = (f'<div class="big">&#127807;</div><div><strong>{esc(_name(r["species"]))}</strong>'
                f' &mdash; {tier}</div>{sci}<div class="meta">{conf}</div>{look}{diff}'
                f'<div class="warn">Check the lookalike difference yourself before eating.</div>')
    retry = _form('<div class="due"></div>')
    return page(f"<h1>Identify</h1>{body}{retry}", title="Identify")
