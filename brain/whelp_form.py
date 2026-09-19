"""Whelp capture: the no-LLM path for logging a birth and a puppy's daily weight.

Same reasoning as `nfc.py`, and the same reason it does not go through the chat agent: the
model can tell you something was logged when it never called the tool (2026-09-01, and the
root cause was an unresolved subject name). At 3am, holding a pup that was born twenty
minutes ago, every name in this system is one the model has never heard. That is precisely
the condition that produces a confident "logged" and an empty table, and there is no human
backstop, because the whole point is that the operator will not remember what he logged.

So: a form POSTs straight to `records_store`, and the reply says what was written.

Identification is the collar, not a tag. A litter of Lhasa pups wears standard coloured
whelping bands, so the colour IS the pup's name until someone picks a real one. That makes
the capture page a row of coloured buttons, which is a bigger tap target than any NFC tag
would be and needs no hunting with the back of a phone in a dark whelping box.

Weights land through `records_store.log_weight`, in grams, which is what `puppy_watch` reads.
Nothing here computes a curve or decides whether a pup is fine — that is the watcher's job,
on a timer, and it is deliberately not a judgement anyone makes at 3am.
"""
from __future__ import annotations

import datetime as dt
import html

import nfc
import records_store as store

# Standard whelping-band colours. Order is the order they appear on the page, and the hex is
# only for the chip: the record stores the colour name, because "Baby Blue" survives a
# screenshot, a phone call to the vet, and a conversation with a buyer. A hex code does not.
COLLARS = [
    ("Red", "#c0392b"), ("Blue", "#2471a3"), ("Green", "#1e8449"), ("Yellow", "#d4ac0d"),
    ("Orange", "#ca6f1e"), ("Purple", "#7d3c98"), ("Pink", "#d96ba0"), ("Baby Blue", "#5dade2"),
    ("Black", "#2c3e50"), ("Brown", "#7e5109"), ("Beige", "#b7a17a"), ("Grey", "#7f8c8d"),
]
_COLLAR_NAMES = {c.lower(): c for c, _ in COLLARS}
_HEX = dict(COLLARS)

# How long a litter stays on the capture page. Matches puppy_watch's window so the page and
# the alerts never disagree about which litter is the live one.
WATCH_DAYS = 21

_OZ = 28.3495


def _oz(grams: float) -> str:
    return f"{grams / _OZ:.1f} oz"


def collar_name(raw: str) -> str | None:
    """The canonical collar colour, or None if it isn't one of ours. Refusing an unknown
    colour is deliberate: a free-text collar field turns into 'blue', 'Blue ', and 'lt blue'
    inside a week, and then the pup has three identities and one of them has the weights."""
    return _COLLAR_NAMES.get((raw or "").strip().lower())


def pup_name(collar: str, whelp_date: str) -> str:
    """The pup's entity name. The colour alone reads best everywhere it shows up (an alert
    says 'Red is 12% below its best weight'), so the colour alone is the name — until a
    previous litter already used it, at which point the date disambiguates rather than two
    pups quietly sharing one row of weights."""
    if store.resolve(collar, kind="pet") is None:
        return collar
    return f"{collar} ({whelp_date})"


def _chip(collar: str) -> str:
    bg = _HEX.get(collar, "#555")
    # Black and Brown chips need light text; the rest are read against dark.
    fg = "#fff" if collar in ("Black", "Brown", "Purple", "Red", "Green", "Blue") else "#12161a"
    return (f'<span class="chip" style="background:{bg};color:{fg}">'
            f'{html.escape(collar)}</span>')


def style() -> str:
    """Page-specific CSS, appended to the shared shell's."""
    return """
  .chip { display:inline-block; padding:6px 14px; border-radius:999px; font-weight:700;
          font-size:1rem; }
  .pup { border:1px solid #333; border-radius:12px; padding:14px; margin:14px 0;
         background:#1a1e22; }
  .pup .row { display:flex; align-items:center; gap:12px; justify-content:space-between; }
  .pup .last { color:#9aa; font-size:0.95rem; }
  .pup form { display:flex; gap:10px; align-items:stretch; margin-top:12px; }
  .pup input[type=number] { flex:1; margin:0; }
  .pup button { margin:0; width:auto; padding:14px 20px; font-size:1.1rem; }
  .up { color:#6c6; } .down { color:#f88; } .flat { color:#dc6; }
  details { margin-top:26px; border-top:1px solid #333; padding-top:10px; }
  summary { font-size:1.05rem; color:#9ad; padding:10px 0; }
  .collars { display:flex; flex-wrap:wrap; gap:10px; margin:12px 0; }
  .collars label { margin:0; }
  .collars input { position:absolute; opacity:0; width:1px; height:1px; }
  .collars .chip { opacity:0.45; border:2px solid transparent; }
  .collars input:checked + .chip { opacity:1; border-color:#fff; }
"""


def _trend(series: list[dict]) -> str:
    """Direction since the previous day, stated and not interpreted."""
    if len(series) < 2:
        return ""
    delta = series[-1]["grams"] - series[-2]["grams"]
    cls = "up" if delta > 0 else "down" if delta < 0 else "flat"
    word = "up" if delta > 0 else "down" if delta < 0 else "flat"
    amount = f" {abs(delta) / _OZ:.1f} oz" if delta else ""
    return f' <span class="{cls}">({word}{amount})</span>'


def _pup_block(pup: str, token: str) -> str:
    series = store.weight_series(pup)
    if series:
        last = series[-1]
        when = "today" if last["date"] == dt.date.today().isoformat() else last["date"]
        summary = f"{_oz(last['grams'])} &middot; {when}{_trend(series)}"
    else:
        summary = '<span class="flat">never weighed</span>'
    collar = pup.split(" (")[0]
    return f"""
    <div class="pup">
      <div class="row">
        <div>{_chip(collar)}</div>
        <div class="last">{summary}</div>
      </div>
      <form method="post" action="/whelp/weigh">
        <input type="hidden" name="token" value="{html.escape(token)}">
        <input type="hidden" name="pup" value="{html.escape(pup)}">
        <input type="number" name="oz" step="0.1" min="0.1" max="200"
               inputmode="decimal" placeholder="oz" required>
        <button type="submit">Log</button>
      </form>
    </div>"""


def _born_block(token: str, litter_known: bool) -> str:
    chips = "".join(
        f'<label><input type="radio" name="collar" value="{html.escape(c)}" required>'
        f'{_chip(c)}</label>' for c, _ in COLLARS)
    heading = "Another pup" if litter_known else "First pup"
    return f"""
    <details{"" if litter_known else " open"}>
      <summary>{heading} &mdash; record a birth</summary>
      <form method="post" action="/whelp/born">
        <input type="hidden" name="token" value="{html.escape(token)}">
        <label>Collar</label>
        <div class="collars">{chips}</div>
        <label for="bw">Birth weight (oz)</label>
        <input id="bw" type="number" name="oz" step="0.1" min="0.1" max="200"
               inputmode="decimal" placeholder="oz">
        <label for="sex">Sex</label>
        <select id="sex" name="sex">
          <option value="">not saying yet</option>
          <option value="male">male</option>
          <option value="female">female</option>
        </select>
        <button type="submit">Record the birth</button>
      </form>
    </details>"""


def board(token: str, dam: str, sire: str) -> str:
    """The live litter page: one row per pup, a weight box on each, and a birth form."""
    litter = store.active_litter(WATCH_DAYS)
    if litter:
        day = f"day {litter['age_days']}" if litter["age_days"] else "born today"
        head = (f"<h1>{html.escape(litter['name'])}</h1>"
                f'<div class="subject">{len(litter["pups"])} pup'
                f'{"s" if len(litter["pups"]) != 1 else ""} &middot; {day}</div>')
        body = "".join(_pup_block(p, token) for p in litter["pups"])
    else:
        head = (f"<h1>No litter yet</h1><div class=\"subject\">"
                f"{html.escape(dam)} &times; {html.escape(sire)} &mdash; record the first pup "
                f"and this page becomes the board</div>")
        body = ""
    return nfc.page(head + body + _born_block(token, bool(litter)), "Whelp", style())


def weigh_result(pup: str, oz_text: str) -> tuple[str, int]:
    """Log one weight. Returns the confirmation page and its status."""
    try:
        r = store.log_weight(pup, oz_text, unit="oz")
    except (TypeError, ValueError):
        return error("That weight didn't read as a number of ounces.", "400"), 400
    series = store.weight_series(pup)
    return confirm(f"{pup.split(' (')[0]} &mdash; {r['amount']}",
                   f"Logged against {pup}.{_plain_trend(series)}"), 200


def _plain_trend(series: list[dict]) -> str:
    if len(series) < 2:
        return " First weight on record."
    delta = series[-1]["grams"] - series[-2]["grams"]
    if delta > 0:
        return f" Up {abs(delta) / _OZ:.1f} oz since the last weighing."
    if delta < 0:
        return f" Down {abs(delta) / _OZ:.1f} oz since the last weighing. Watch it nurse."
    return " Flat since the last weighing."


def born_result(collar_raw: str, oz_text: str, sex: str, dam: str, sire: str) -> tuple[str, int]:
    """Record a birth against the collar colour. Returns the confirmation page and status."""
    collar = collar_name(collar_raw)
    if not collar:
        return error("Pick one of the collar colours.", "400"), 400
    now = dt.datetime.now()
    name = pup_name(collar, now.date().isoformat())
    attrs = {"collar": collar}
    if sex in ("male", "female"):
        attrs["sex"] = sex
    weight_note = ""
    if (oz_text or "").strip():
        try:
            grams = float(oz_text) * _OZ
        except (TypeError, ValueError):
            return error("That birth weight didn't read as a number of ounces.", "400"), 400
        attrs["weight"] = f"{float(oz_text):g} oz"
        weight_note = f" Birth weight {_oz(grams)}."
    # Second precision, matching records_store._now(). A microsecond timestamp string-sorts
    # after every second-precision one from the same second, which put a birth *after* the
    # first weighing of that pup and made the board read the birth weight as the latest.
    r = store.add_birth(name, dam=dam, sire=sire, born=now.isoformat(timespec="seconds"),
                        attrs=attrs)
    return confirm(f"{collar} pup recorded",
                   f"{r['litter']} is now {r['litter_size']} pup"
                   f"{'s' if r['litter_size'] != 1 else ''}.{weight_note}"), 200


def error(msg: str, hint: str = "") -> str:
    return nfc.error_page(msg, hint)


def confirm(headline: str, detail: str) -> str:
    now = dt.datetime.now().strftime("%b %-d, %Y %-I:%M %p")
    return nfc.page(f"""
    <div class="big">&#9989;</div>
    <div><strong>{headline}</strong></div>
    <div class="meta">{detail}</div>
    <div class="meta">{now}</div>
    """, "Whelp", style())
