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

Nothing leaves the box. The photo page keeps each identification as a sighting (the photos,
upright with EXIF stripped, and the classifiers' call) for a person to label with what it
really was: the local in-field dataset, in sightings_store. The JSON route keeps nothing.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import html
import importlib
import importlib.util
import io
import os
import sys
import threading
from pathlib import Path

import config
import sightings_store
from nfc import page

STATION = Path(os.environ.get("HESTIA_FORAGER_STATION")
               or Path.home() / "Documents/Forager/forager-field-station")
# The two-photo rule fuses up to this many angles of one specimen. Two is what it asks for.
MAX_PHOTOS = 2
# Kept photos are re-encoded no larger than this on the long side: plenty for the 224px
# experts and for a person checking a label, without keeping 12MP originals.
KEEP_SIDE = 1600

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


def _open(photos: list[bytes]) -> list:
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
    return images


def _classify(images: list) -> dict:
    state = _load()
    result = state["session"].resolve_session(state["pipe"], images)
    return {**dataclasses.asdict(result), "photos": len(images)}


def identify(photos: list[bytes]) -> dict:
    """Identify one specimen from one or two photos. Raises Unavailable or ValueError."""
    return _classify(_open(photos))


def keep(photos: list[bytes]) -> dict:
    """Identify, then keep the photos and the call as a sighting to be labeled.

    Each photo is saved re-encoded from the decoded pixels, so no EXIF survives (a phone
    photo's GPS would otherwise sit in the dataset)."""
    images = _open(photos)
    result = _classify(images)
    rel_dir = Path("identify") / dt.datetime.now().strftime("%Y-%m")
    (config.PHOTO_DIR / rel_dir).mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    saved = []
    for n, img in enumerate(images, 1):
        img = img.copy()
        img.thumbnail((KEEP_SIDE, KEEP_SIDE))
        rel = rel_dir / f"{stamp}-{n}.jpg"
        img.save(config.PHOTO_DIR / rel, format="JPEG", quality=90)
        saved.append(str(rel))
    return {**result, "sighting": sightings_store.add(saved, result)}


def photo_path(sid: int, n: int) -> Path | None:
    """The file for photo n (1-based) of a sighting, only if it is one this module kept."""
    s = sightings_store.get(sid)
    if not s or not 1 <= n <= len(s["photos"]):
        return None
    root = (config.PHOTO_DIR / "identify").resolve()
    path = (config.PHOTO_DIR / s["photos"][n - 1]).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


# The app's manifest scope is "/", so these pages open inside the installed app, which has no
# back button. Every page carries its own way back.
_BACK = '<div class="meta" style="margin:-8px 0 14px"><a href="/app" style="color:#9ad">&larr; Hestia</a></div>'


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
    return page(f"{_BACK}<h1>What is this?</h1>"
                f'<div class="subject">Berries, mushrooms and wild plants</div>{_form()}'
                f'<div class="meta"><a href="/identify/review" style="color:#9ad">Sightings to label</a></div>',
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
    labeling = _label_form(r["sighting"], r) if r.get("sighting") else ""
    retry = _form('<div class="due"></div>')
    return page(f"{_BACK}<h1>Identify</h1>{body}{labeling}{retry}", title="Identify")


def _guess(r: dict) -> str:
    """What the classifiers said, in a few words, for the label form and the review list."""
    if not r["abstained"]:
        return _name(r["species"])
    lean = r["key_diff"].removeprefix("Leaning ").split(",")[0] if r["reason"] == "need_second_photo" else ""
    return f"not sure (leaning {lean})" if lean else "not sure"


def _species_list() -> str:
    known = sorted({_name(k) for k in _class_keys()})
    return '<datalist id="species">' + "".join(f'<option value="{html.escape(k)}">' for k in known) + "</datalist>"


def _label_form(sid: int, r: dict, current: str | None = None, species_list: bool = True) -> str:
    now = f'<div class="meta">Labeled: <strong>{html.escape(current)}</strong></div>' if current else ""
    return f"""
    <div class="due">
      <form method="post" action="/identify/{sid}/label">
        <label for="l{sid}">What was it really?</label>
        {now}
        <input id="l{sid}" name="label" list="species" placeholder="e.g. cilantro" autocomplete="off">
        {_species_list() if species_list else ""}
        <div class="meta">The classifiers said: {html.escape(_guess(r))}. Only label what you are sure of.</div>
        <button type="submit">Save label</button>
      </form>
    </div>"""


def _class_keys() -> list[str]:
    try:
        pipe = _load()["pipe"]
    except Unavailable:
        return []
    return [k for name, keys in getattr(pipe, "_classes", {}).items()
            if name.endswith("_expert") for k in keys]


def labeled_page(sid: int, text: str) -> str:
    return page(f'{_BACK}<div class="big">&#9989;</div><div><strong>Labeled: {html.escape(text)}</strong></div>'
                f'<div class="meta">Sighting {sid}, kept on the box.</div>'
                f'<button type="button" onclick="location.href=\'/identify\'">Identify another</button>'
                f'<div class="meta"><a href="/identify/review" style="color:#9ad">All sightings</a></div>',
                title="Identify")


def review_page(rows: list[dict]) -> str:
    """Every sighting, unlabeled first, each with its photos and a label form."""
    if not rows:
        return page(f'{_BACK}<h1>Sightings</h1><div class="meta">None yet. '
                    '<a href="/identify" style="color:#9ad">Identify something</a>.</div>', title="Sightings")
    items = []
    for s in rows:
        when = dt.datetime.fromtimestamp(s["ts"]).strftime("%b %-d, %-I:%M %p")
        imgs = "".join(f'<img src="/identify/photo/{s["id"]}/{n}" loading="lazy" '
                       f'style="width:48%;margin:4px 1% 0 0;border-radius:8px">'
                       for n in range(1, len(s["photos"]) + 1))
        items.append(f'<div class="due"><div class="meta">#{s["id"]} &middot; {when}</div>{imgs}'
                     f'{_label_form(s["id"], s["result"], s["label"], species_list=False)}</div>')
    todo = sum(1 for s in rows if not s["label"])
    return page(f'{_BACK}<h1>Sightings</h1><div class="subject">{todo} to label</div>{_species_list()}{"".join(items)}',
                title="Sightings")
