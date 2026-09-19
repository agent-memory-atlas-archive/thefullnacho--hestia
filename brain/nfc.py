"""NFC capture: a deterministic, no-LLM logging path for garden harvests and asset service.

Exists because the chat agent can (and did — 2026-09-01) tell the user something was logged
when it never called the tool. A tag scan can't afford that: the whole point is "take the
action in the moment, because I will forget I did it otherwise," so there is no human
backstop to notice a silent no-op later. This module never touches the model — a scan hits a
plain form, the form POSTs straight to `records_store`, and the reply says exactly what was
written (or exactly what was wrong), synchronously, in the response.

A watering stake encodes the short form `/nfc?token=...&p=<slug>`, and the slug is resolved
against the positions file into subject, source and sprinkler. That indirection is not
tidiness: the long form ran to 157 bytes and an NTAG213 took 137 on the bench and refused
141, so the full URL physically would not write. An unknown slug is an error page, never a default.

The long form still works and is what the asset tags carry:
A tag encodes a URL like `/nfc?token=...&kind=harvest&subject=Bed+2` (garden beds),
`/nfc?token=...&kind=service&subject=Furnace+Filter` (assets — resets the `due` clock, see
`records_store.due_assets`), or `/nfc?token=...&kind=use&subject=Weedwhacker` (assets whose
maintenance isn't calendar-based — just a running log of minutes run, no due date implied), or
`/nfc?token=...&kind=watering&subject=Back+Fence&source=zone3&sprinkler=hi-rise` (a stake in the
ground where a hose-fed sprinkler gets set down).
`subject` is the bed/asset/place name; GET renders the capture form with it locked in, POST
/nfc/log does the write and renders the confirmation.

A stake also answers the opposite question. The watering form carries a second, separate form
for a rain reading off the catch cup mounted on that same stake: `kind=rain` with a depth and
no duration, `basis` always `measured`, kept out of `watering` so a season's applied water and
its fallen water never end up summed in one column. A reading at the cup's brim is reported
back as a floor rather than a total, because a full cup and one that overflowed twice look the
same to a ruler.

A watering tag carries its own `source` and `sprinkler` because the stake never moves and the
answers never change, which leaves one prefilled field between a scan and a logged run. That
matters more here than anywhere else: watering is the job most likely to be done with wet hands,
in a hurry, before coffee.

The watering confirmation offers a camera when that place has not been photographed in
`PHOTO_EVERY_DAYS`, posting to `/nfc/photo`, which authorises on the NFC token rather than the
ingest one so a stake in the ground carries a single credential. The offer comes after the run
is already written, so declining it never costs the logged watering.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re

import config
import records_store as store

UNITS = ["lb", "oz", "kg", "g", "each", "pint", "quart", "basket"]

# The standing cycle length. Prefilled so the common case is scan, glance, tap.
DEFAULT_WATERING_MINUTES = 15

# How often a position is worth photographing. In a hot spell the sprinkler runs four times a
# week, and four pictures a week of the same shrub is a flipbook, not a record. Weekly is the
# cadence that shows a season. Asking only when one is due keeps the decision a row lookup
# rather than the operator's judgement, which is the same reason schedules are not the model's
# business either.
PHOTO_EVERY_DAYS = 7

# The catch cup's inside height, from hardware/nfc-stake.scad. A reading at the brim is a
# floor and not a total, and the form has to say so, because a full cup and a cup that
# overflowed twice look identical to a ruler.
CUP_DEPTH_MM = 50


def _page(body: str, title: str = "Hestia") -> str:
    # One shared shell: big tap targets and large text, meant to be read at arm's length
    # outdoors or read once and dismissed, not a UI anyone lingers in.
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; background: #14171a; color: #eee;
         margin: 0; padding: 24px 20px 60px; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 4px; }}
  .subject {{ color: #9ad; font-size: 1.1rem; margin-bottom: 20px; }}
  label {{ display: block; margin: 18px 0 6px; font-size: 1rem; color: #ccc; }}
  input:not([type=file]), select {{ width: 100%; box-sizing: border-box; font-size: 1.4rem;
                   padding: 14px; border-radius: 10px; border: 1px solid #444; background: #1e2226;
                   color: #fff; }}
  /* A styled `input[type=file]` is a trap on a phone: iOS draws only a small native "Choose
     File" control inside whatever box the CSS makes, so a full-width dark box is mostly dead
     pixels and tapping it does nothing. The input is hidden and a full-width <label for> is the
     tap target instead, which every mobile browser forwards to the picker. Hidden by clipping,
     never `display:none`, because a display:none input is not focusable and some browsers
     refuse to open the picker for it. */
  .filein {{ position: absolute; width: 1px; height: 1px; opacity: 0; overflow: hidden; }}
  .filebtn {{ display: block; width: 100%; box-sizing: border-box; margin: 6px 0 0;
             text-align: center; font-size: 1.3rem; padding: 16px; border-radius: 10px;
             border: 1px dashed #3a7; background: #1e2226; color: #9ad; cursor: pointer; }}
  .filebtn.chosen {{ border-style: solid; background: #1d2e1d; color: #bfe6c8; }}
  .altbtn {{ display: block; width: 100%; margin-top: 10px; background: none; border: none;
            color: #9ad; font-size: 0.95rem; text-decoration: underline; padding: 8px; }}
  button {{ margin-top: 28px; width: 100%; font-size: 1.3rem; padding: 16px; border-radius: 10px;
           border: none; background: #3a7; color: #04120a; font-weight: 600; }}
  .big {{ font-size: 2.4rem; margin: 20px 0 8px; }}
  .warn {{ background: #5a3a10; color: #ffd699; padding: 14px; border-radius: 10px; margin-top: 18px; }}
  .err {{ background: #5a1010; color: #ffb3b3; padding: 14px; border-radius: 10px; }}
  .meta {{ color: #999; font-size: 0.95rem; margin-top: 6px; }}
  .due {{ border-top: 1px solid #333; margin-top: 28px; padding-top: 8px; }}
</style></head>
<body>{body}</body></html>"""


def position_slug(name: str) -> str:
    """The stake's short name. Same function as `hardware/make_tags.py`, on purpose: the slug
    is the tag's whole payload, so the two must agree or a scan resolves to nothing."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def resolve_position(key: str) -> dict | None:
    """Turn a scanned slug back into name, source and sprinkler. Returns None when the slug
    names nothing, so the caller can say so out loud — never a guess or a partial match. A
    watering run logged against the wrong bed is worse than one that refuses to log."""
    try:
        positions = json.loads(config.POSITIONS_DATA.read_text())
    except (OSError, ValueError):
        return None
    for position in positions:
        name = str(position.get("name") or "").strip()
        if name and position_slug(name) == key:
            return {"subject": name,
                    "source": str(position.get("source") or ""),
                    "sprinkler": str(position.get("sprinkler") or "")}
    return None


def unknown_position_page(key: str) -> str:
    return error_page(
        f"This tag names a position the brain does not know: <code>{html.escape(key)}</code>. "
        f"Nothing was logged. Check {html.escape(config.POSITIONS_DATA.name)}, or re-write the "
        f"tag from data/stake-urls.txt.", "404")


def error_page(msg: str, status_hint: str = "") -> str:
    return _page(f'<div class="err"><strong>{status_hint or "Error"}</strong><br>{msg}</div>')


def bad_token_page() -> str:
    return error_page("Missing or bad token. Re-scan the tag, or check secrets/nfc.env.", "401")


def capture_form(kind: str, subject: str, token: str,
                 source: str = "", sprinkler: str = "") -> str:
    extra_form = ""
    if kind == "harvest":
        fields = f"""
        <label for="crop">Crop</label>
        <input id="crop" name="crop" type="text" autofocus required autocomplete="off">
        <label for="qty">Amount</label>
        <input id="qty" name="qty" type="number" step="any" inputmode="decimal" required>
        <label for="unit">Unit</label>
        <select id="unit" name="unit">{"".join(f'<option value="{u}">{u}</option>' for u in UNITS)}</select>
        """
        button = "Log harvest"
    elif kind == "service":
        fields = """
        <label for="note">Note (optional)</label>
        <input id="note" name="note" type="text" placeholder="serviced" autocomplete="off">
        """
        button = "Log service"
    elif kind == "use":
        fields = """
        <label for="minutes">Minutes run</label>
        <input id="minutes" name="minutes" type="number" step="any" inputmode="decimal" autofocus required>
        <label for="note">Note (optional)</label>
        <input id="note" name="note" type="text" placeholder="e.g. front + back yard" autocomplete="off">
        """
        button = "Log run"
    elif kind == "watering":
        if sprinkler and sprinkler.lower() not in store.SPRINKLERS:
            return error_page(f"Tag names an unknown sprinkler '{html.escape(sprinkler)}'.", "400")
        # A tag that already knows its sprinkler asks nothing about it; one that doesn't
        # offers the choice rather than silently assuming a rate that was never applied.
        if sprinkler:
            picker = f'<input type="hidden" name="sprinkler" value="{html.escape(sprinkler)}">'
        else:
            options = "".join(
                f'<option value="{html.escape(k)}">{html.escape(v.get("label") or k)}</option>'
                for k, v in store.SPRINKLERS.items())
            picker = f"""
        <label for="sprinkler">Sprinkler</label>
        <select id="sprinkler" name="sprinkler">
          <option value="">None (drip or soaker)</option>{options}</select>"""
        fields = f"""
        <label for="minutes">Minutes</label>
        <input id="minutes" name="minutes" type="number" step="any" inputmode="decimal"
               value="{DEFAULT_WATERING_MINUTES}" autofocus required>{picker}
        <input type="hidden" name="source" value="{html.escape(source)}">
        """
        button = "Log watering"
        # The same stake answers both questions, because the cup is mounted on it. One tap
        # after a run logs minutes; one tap after rain logs a depth someone actually read.
        extra_form = _rain_form(subject, token)
    elif kind == "rain":
        fields = """
        <label for="depth">Depth in the cup</label>
        <input id="depth" name="depth" type="number" step="any" inputmode="decimal" autofocus required>
        <label for="unit">Unit</label>
        <select id="unit" name="unit"><option value="in">inches</option><option value="mm">mm</option></select>
        <label for="note">Note (optional)</label>
        <input id="note" name="note" type="text" placeholder="e.g. emptied mid-storm" autocomplete="off">
        """
        button = "Log rain"
    else:
        return error_page(f"Unknown kind '{html.escape(kind)}' — tag should encode "
                          "kind=harvest, kind=service, kind=use, kind=watering, or kind=rain.", "400")

    safe_subject = html.escape(subject)
    heading = {"harvest": "Harvest", "watering": "Watering",
               "rain": "Rain reading"}.get(kind, "Service")
    return _page(f"""
    <h1>{heading}</h1>
    <div class="subject">{safe_subject}</div>
    <form method="post" action="/nfc/log">
      <input type="hidden" name="token" value="{html.escape(token)}">
      <input type="hidden" name="kind" value="{html.escape(kind)}">
      <input type="hidden" name="subject" value="{safe_subject}">
      {fields}
      <button type="submit">{button}</button>
    </form>
    {extra_form}
    """)


def _rain_form(subject: str, token: str) -> str:
    """The second question a stake can answer. Its own <form> rather than a second button on
    the watering one, so the two kinds can never be submitted together or confused."""
    safe_subject = html.escape(subject)
    return f"""
    <div class="due">
      <form method="post" action="/nfc/log">
        <input type="hidden" name="token" value="{html.escape(token)}">
        <input type="hidden" name="kind" value="rain">
        <input type="hidden" name="subject" value="{safe_subject}">
        <label for="depth">Or log rain caught in the cup</label>
        <input id="depth" name="depth" type="number" step="any" inputmode="decimal"
               placeholder="depth">
        <select name="unit"><option value="in">inches</option><option value="mm">mm</option></select>
        <button type="submit">Log rain</button>
      </form>
    </div>"""


def photo_due(subject: str) -> float | None:
    """Days since this place was last photographed, if a new photo is due. None means it was
    photographed recently enough to leave the operator alone."""
    days = store.days_since_photo(subject)
    if days is None:
        return -1.0                      # never photographed: always worth the first one
    return days if days >= PHOTO_EVERY_DAYS else None


def _photo_form(subject: str, token: str, days: float) -> str:
    """A camera button, shown only when one is due.

    The tap target is the <label>, not the input. A styled file input on iOS is a box whose
    tappable area is only the small native control inside it, so the obvious full-width box
    did nothing when tapped (reported 2026-09-19, in the field, which is the only place it
    could have been found).

    `capture` sends the primary button straight to the camera, which is the whole point at a
    stake. It is also the one attribute that can make a tap silently do nothing when Safari's
    camera permission for this site has been denied, so there is a second control that drops
    `capture` and opens the library. Same input, same field name, so the server sees one file
    either way. With JS off the primary label still works, because `for=` needs no script.
    """
    safe_subject = html.escape(subject)
    when = "no photo of this one yet" if days < 0 else f"last photo {days:.0f} days ago"
    return f"""
    <div class="due">
      <form method="post" action="/nfc/photo" enctype="multipart/form-data">
        <input type="hidden" name="token" value="{html.escape(token)}">
        <input type="hidden" name="subject" value="{safe_subject}">
        <div class="meta">Weekly photo &mdash; {when}</div>
        <input id="photo" name="file" type="file" accept="image/*" capture="environment"
               class="filein">
        <label class="filebtn" id="photobtn" for="photo">&#128247; Take the photo</label>
        <button type="button" class="altbtn" id="photolib">or choose an existing photo</button>
        <button type="submit">Add photo</button>
      </form>
    </div>
    <script>
    (function () {{
      var input = document.getElementById('photo');
      var btn = document.getElementById('photobtn');
      var lib = document.getElementById('photolib');
      // Hidden input means no native filename readout, and at arm's length outdoors you need
      // to know the photo took before you commit to the submit.
      input.addEventListener('change', function () {{
        var f = input.files && input.files[0];
        btn.textContent = f ? '✓ ' + f.name : '📷 Take the photo';
        btn.classList.toggle('chosen', !!f);
      }});
      lib.addEventListener('click', function () {{
        input.removeAttribute('capture');
        input.click();
      }});
    }})();
    </script>"""


def _confirm(headline: str, detail: str, created: bool, subject: str, extra: str = "") -> str:
    now = dt.datetime.now().strftime("%b %-d, %Y %-I:%M %p")
    safe_subject = html.escape(subject)
    warn = (f'<div class="warn">⚠ \'{safe_subject}\' wasn\'t a known entity — created it new. '
            f"If that's a mishear, fix it in the records DB.</div>" if created else "")
    return _page(f"""
    <div class="big">✅</div>
    <div><strong>{html.escape(headline)}</strong></div>
    <div class="meta">{html.escape(detail)}</div>
    <div class="meta">{now}</div>
    {warn}
    {extra}
    """)


def log_harvest_tag(subject: str, crop: str, qty: str, unit: str) -> tuple[str, int]:
    """Write the harvest and render the confirmation. Returns (html, status)."""
    crop = (crop or "").strip()
    if not crop:
        return error_page("Crop is required.", "400"), 400
    try:
        q = float(qty)
    except (TypeError, ValueError):
        return error_page("Amount must be a number.", "400"), 400
    if not q > 0:
        return error_page("Amount must be a positive number.", "400"), 400

    r = store.log_harvest(subject, crop, q, unit=unit)
    cls, canon = store.normalize_unit(unit)
    amount = f"{q:g}" + (f" {canon}" if cls != "count" else "")
    return _confirm(f"Logged {amount} of {crop}", subject, r.get("created", False), subject), 200


def log_service_tag(subject: str, note: str) -> tuple[str, int]:
    """Write the service event and render the confirmation. Returns (html, status)."""
    detail = (note or "").strip() or "serviced"
    r = store.log_event("service", subject=subject, action="serviced", detail=detail,
                        subject_kind="asset", strict_subject=True)
    return _confirm("Logged service", f"{subject} — {detail}", r.get("created", False), subject), 200


def photo_result(subject: str, payload: dict, status: int) -> tuple[str, int]:
    """Render the outcome of a tag-tap photo. A failure says so rather than looking filed."""
    if status != 200:
        return error_page(html.escape(str(payload.get("error") or "Upload failed.")),
                          str(status)), status
    filed = (payload.get("filed") or [{}])[0]
    return _confirm("Photo filed", f"{subject} — {payload.get('bytes', 0) // 1024} KB",
                    bool(filed.get("created")), subject), 200


def log_watering_tag(subject: str, minutes: str, source: str = "",
                     sprinkler: str = "", token: str = "") -> tuple[str, int]:
    """Write a watering run for a place and render the confirmation. The derived depth and
    volume are shown back so an estimate is visibly an estimate at the moment it is made,
    not a number discovered later in a report. Returns (html, status)."""
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        return error_page("Minutes must be a number.", "400"), 400
    if not m > 0:
        return error_page("Minutes must be a positive number.", "400"), 400
    sprinkler = (sprinkler or "").strip().lower()
    if sprinkler and sprinkler not in store.SPRINKLERS:
        return error_page(f"Unknown sprinkler '{html.escape(sprinkler)}'.", "400"), 400
    seconds = int(round(m * 60))
    inches, gallons = store.water_applied(seconds, sprinkler)
    r = store.log_watering(subject, seconds, source=(source or "").strip()[:32] or None,
                           sprinkler=sprinkler or None)
    detail = subject if inches is None else f"{subject} — {inches:g} in, {gallons:g} gal (estimated)"
    # The run is already written. A due photo is an offer on the way out, never a gate.
    due = photo_due(subject) if token else None
    extra = _photo_form(subject, token, due) if due is not None else ""
    return _confirm(f"Logged {m:g} min watering", detail,
                    r.get("created", False), subject, extra), 200


def log_rain_tag(subject: str, depth: str, unit: str = "in",
                 note: str = "") -> tuple[str, int]:
    """Write a rain reading taken off the catch cup. The cup is 50mm deep, so a reading at
    or past that is reported back as a floor rather than a total: a full cup means "at least
    this much", and saying so is the difference between a measurement and a guess."""
    try:
        d = float(depth)
    except (TypeError, ValueError):
        return error_page("Depth must be a number.", "400"), 400
    if not d > 0:
        return error_page("Depth must be a positive number.", "400"), 400
    inches = d / store.MM_PER_IN if (unit or "in").lower() == "mm" else d
    mm = inches * store.MM_PER_IN
    if mm > CUP_DEPTH_MM + 1:
        return error_page(
            f"{mm:.0f}mm is deeper than the cup is tall ({CUP_DEPTH_MM:g}mm). "
            "Check the unit, or log it as two readings if you emptied it mid-storm.",
            "400"), 400
    r = store.log_rain(subject, round(inches, 3), note=(note or "").strip() or None)
    overflow = ("<div class=\"warn\">Cup was at or near full, so this is a floor, "
                "not a total. Real rainfall was this much or more.</div>"
                if mm >= CUP_DEPTH_MM - 2 else "")
    return _confirm(f"Logged {inches:.2f} in of rain", f"{subject} — {mm:.0f} mm, measured",
                    r.get("created", False), subject, overflow), 200


def log_use_tag(subject: str, minutes: str, note: str) -> tuple[str, int]:
    """Write a runtime/usage event (no due-date implied — just a log for averages later,
    e.g. the weedwhacker: no service interval, just 'how long did this run'). Returns (html, status)."""
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        return error_page("Minutes must be a number.", "400"), 400
    if not m > 0:
        return error_page("Minutes must be a positive number.", "400"), 400
    note = (note or "").strip()
    detail = f"{m:g} min" + (f" — {note}" if note else "")
    r = store.log_event("use", subject=subject, action="ran", detail=detail,
                        subject_kind="asset", strict_subject=True, attrs={"minutes": m})
    return _confirm(f"Logged {m:g} min run", subject, r.get("created", False), subject), 200
