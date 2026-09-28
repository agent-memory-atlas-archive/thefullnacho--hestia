"""The board: the operator's to-dos and the home's state, drawn as one image for an e-ink screen.

Three columns, split by the kind of attention each job takes rather than where it came from:

  - HANDS   — the operator's queue items that need a body: outdoors, the printer, a tag
  - SCREEN  — queue items that pull attention onto a screen: replying, deciding, publishing
  - HOME    — what Hestia itself knows needs a person: the watches, due assets, reminders

A small character in the header carries the home's mood: the most urgent Home item decides
it. No LLM anywhere in this file. What is due, what is out of band and what is late are rows
and comparisons, the same as every watcher, so the face can never say something the records
don't.

The device only draws. This module renders a PNG; whatever screen shows it (a jailbroken
Kindle today) fetches or is sent that image and nothing else. `png_bytes(device="kindle")`
returns the panel's native portrait orientation in 16 grays.

The operator's queue is a markdown table the brain does not own. It is read from
`BOARD_QUEUE` (default `data/board-queue.md`, typically a symlink), never written here.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import config  # puts brain/ on sys.path + owns paths

config.load_secrets()

BOARD_QUEUE = Path(os.environ.get("BOARD_QUEUE") or config.DATA_DIR / "board-queue.md")
HOME_CACHE_S = int(os.environ.get("BOARD_HOME_CACHE_S", "300"))  # HA + forecast reads, not per frame
STALE_DAYS = 7          # queue items older than this carry their age on the board
REVIEW_DAYS = int(os.environ.get("BOARD_REVIEW_DAYS", "14"))  # older rows ask "still real?"
NIGHT = (22, 5)         # the character sleeps unless something is urgent

SIZE = (1024, 758)      # landscape; the Kindle Paperwhite 1 panel is 758x1024 portrait
MARGIN = 28
GUTTER = 24
HEADER_H = 132
FOOTER_H = 34

INK, MID, LIGHT, PAPER = 0, 90, 170, 255

_FONT_DIRS = ("/usr/share/fonts/truetype/ibm-plex", "/usr/share/fonts/truetype/dejavu")
_FONT_FILES = {
    "regular": ("IBMPlexSans-Regular.ttf", "DejaVuSans.ttf"),
    "medium": ("IBMPlexSans-Medium.ttf", "DejaVuSans.ttf"),
    "bold": ("IBMPlexSans-SemiBold.ttf", "DejaVuSans-Bold.ttf"),
}


def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    for name in _FONT_FILES[weight]:
        for d in _FONT_DIRS:
            p = Path(d) / name
            if p.exists():
                return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


# ── the queue ────────────────────────────────────────────────────────────────────────────

_ROW = re.compile(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_SLOT = re.compile(
    r"^(?:(?:morning|evening|afternoon|tonight|late \w+|\w+ or \w+ morning|after [^:]{0,60}|"
    r"when [^:]{0,80})[^:]{0,60}:|\d{1,2}:\d{2}(?: or \d{1,2}:\d{2})?(?: on [^:,]+)?[,:]?)\s*",
    re.IGNORECASE)
_SCREEN_SLOT = re.compile(r"\b1[25]:00\b")
_SCREEN_VERB = re.compile(
    r"^(?:reply|send|email|publish|submit|upload|comment|decide|read|confirm|call|ask)\b",
    re.IGNORECASE)
_HANDS_WORDS = re.compile(
    r"\b(?:morning|evening|hands(?!-only)|outdoors|forage|oak|tree|garden|greenhouse|print|reprint|solder|wire|measure|"
    r"photograph|photo|shoot|stake|tags?|sensor|battery|multimeter|walk|hunt|dig|plant|"
    r"power-cycle|flash|plug|scale|bed|pot)\b", re.IGNORECASE)
_ON_DATE = re.compile(r"\bon(?: or after)? (\d{4}-\d{2}-\d{2})")
_LATE_MONTH = re.compile(r"^(?:early|mid|late) (january|february|march|april|may|june|july|"
                         r"august|september|october|november|december)\b", re.IGNORECASE)
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]


def _clean(text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return re.sub(r"\s+", " ", text.replace("**", "")).strip()


def title_of(item: str) -> str:
    """The short line the board shows: the bolded part if the row has one, minus its slot."""
    m = _BOLD.match(item.strip())
    if m:
        title = _clean(m.group(1))
    else:  # an unformatted row: its first clause is the job
        title = re.split(r" — | - |\. |; | \(", _clean(item), maxsplit=1)[0]
    for _ in range(2):  # "Morning, first thing (…): 12:00: x" peels twice
        title = _SLOT.sub("", title, count=1)
    title = title.strip(" .:;,")
    return title[:1].upper() + title[1:] if title else _clean(item)


def column_of(item: str, title: str) -> str:
    """The slot the row names wins. Without one, anything physical is hands and the rest is
    screen, because an untagged job on this queue is almost always something at a keyboard."""
    if _SCREEN_SLOT.search(item) or _SCREEN_VERB.match(title):
        return "screen"
    return "hands" if _HANDS_WORDS.search(item) else "screen"


def deferred(item: str, today: dt.date) -> bool:
    """A row that says when it starts ("on or after 2026-10-04", "Late November") stays off
    the board until then. It is still in the queue; it just isn't today's work."""
    text = _clean(item)
    m = _ON_DATE.search(text)
    if m:
        try:
            if dt.date.fromisoformat(m.group(1)) > today:
                return True
        except ValueError:
            pass
    m = _LATE_MONTH.match(text)
    return bool(m) and _MONTHS.index(m.group(1).lower()) + 1 != today.month


def queue_items(text: str, today: dt.date) -> list[dict]:
    """Open rows of the operator's queue, oldest first. Rows with a Done date are finished
    even if nobody has moved them yet, so they never reach the board."""
    out, in_open = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            in_open = line.strip().lower().startswith("## open")
            continue
        if not in_open or not _ROW.match(line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or cells[4]:
            continue
        added, project, item = cells[0], cells[1], cells[2]
        if deferred(item, today):
            continue
        try:
            age = (today - dt.date.fromisoformat(added)).days
        except ValueError:
            age = 0
        title = title_of(item)
        out.append({"title": title, "project": project, "age": age,
                    "column": column_of(item, title), "key": "queue:" + line, "line": line,
                    "done": "queue"})
    out.sort(key=lambda r: -r["age"])
    return out


# Rows past REVIEW_DAYS get Keep / Done / Trash instead of a plain two-tap done. The backlog
# is what gets ignored while new work is handled as it arrives, and an old row is as likely to
# be done-but-never-logged as it is to be dead. "Keep" snoozes the question for REVIEW_DAYS;
# the snooze is keyed on the row's exact text, so an edited row is asked about afresh.
QUEUE_CHOICES = (("Keep", "keep"), ("Done", "done"), ("Trash", "trash"))
TRASH_LOG = config.BOARD_REVIEW_STATE.with_name("board_trashed.tsv")


def _row_id(line: str) -> str:
    return hashlib.sha256(line.encode()).hexdigest()[:16]


def _reviews() -> dict:
    try:
        return json.loads(config.BOARD_REVIEW_STATE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def due_for_review(item: dict, today: dt.date, reviews: dict) -> bool:
    if item["age"] < REVIEW_DAYS:
        return False
    kept = reviews.get(_row_id(item["line"]))
    return not kept or (today - dt.date.fromisoformat(kept)).days >= REVIEW_DAYS


def keep_row(line: str, today: dt.date) -> None:
    reviews = _reviews()
    reviews[_row_id(line)] = today.isoformat()
    config.BOARD_REVIEW_STATE.parent.mkdir(parents=True, exist_ok=True)
    config.BOARD_REVIEW_STATE.write_text(json.dumps(reviews))


def read_queue(today: dt.date) -> list[dict]:
    try:
        items = queue_items(BOARD_QUEUE.read_text(), today)
    except FileNotFoundError:
        return []
    reviews = _reviews()
    for item in items:
        if due_for_review(item, today, reviews):
            item["choices"] = QUEUE_CHOICES
            item["detail"] = f"{item['title']} ({item['age']} days). Still real?"
    return items


# ── home ─────────────────────────────────────────────────────────────────────────────────
# level 2: needs a person now · 1: act on it today · 0: fine, shown quietly

def _first_sentence(text: str) -> str:
    m = re.match(r"(.+?[.!?])(?:\s|$)", text)
    return (m.group(1) if m else text).rstrip(".")


def _box(now: dt.datetime) -> list[dict]:
    import box_watch
    import records_store
    litter = records_store.active_litter(box_watch.WATCH_DAYS, now=now)
    limits = box_watch.band(litter["age_days"]) if litter else None
    if not limits:
        return []
    low, high = limits
    temp = box_watch.box_temp_f(box_watch.fetch_state(), now)
    status = box_watch.classify(temp, low, high)
    day = f"day {litter['age_days']}"
    if status == "silent":
        return [{"title": "Box: no reading", "sub": f"{day} · check the Govee", "level": 2}]
    if status in ("hot", "cold"):
        return [{"title": f"Box {temp:.0f}°F, too {status}", "sub": f"band {low:.0f}-{high:.0f} · {day}",
                 "level": 2}]
    return [{"title": f"Box {temp:.0f}°F", "sub": f"in band {low:.0f}-{high:.0f} · {day}", "level": 0}]


def _pups(now: dt.datetime) -> list[dict]:
    import puppy_watch
    import records_store
    litter = records_store.active_litter(puppy_watch.WATCH_DAYS, now=now)
    if not litter:
        return []
    pups = litter["pups"]
    if not pups:
        return [{"title": "Log the pups", "sub": litter["name"], "level": 2}]
    unweighed, worries = [], []
    for pup in pups:
        for alert in puppy_watch.pup_alerts(pup, records_store.weight_series(pup),
                                            litter["age_days"], now.date()):
            if "not been weighed today" in alert:
                if now.hour >= puppy_watch.WEIGH_BY_HOUR:
                    unweighed.append(pup)
            else:
                worries.append({"title": _first_sentence(alert), "sub": pup, "level": 2,
                                "detail": alert})
    out = worries
    if unweighed:
        out.append({"title": "Weigh the pups", "sub": f"{len(unweighed)} of {len(pups)} not weighed today",
                    "level": 1})
    if not out:
        out.append({"title": f"{len(pups)} pups, all gaining", "sub": f"day {litter['age_days']}",
                    "level": 0})
    return out


def _garden(now: dt.datetime) -> list[dict]:
    import garden_watch
    return [{"title": _first_sentence(a), "sub": "garden", "level": 1, "detail": a}
            for a in garden_watch.build_alerts(persist=False)]


def _assets(now: dt.datetime) -> list[dict]:
    import records_store
    return [{"title": a["name"], "sub": f"service due · {a['schedule']}", "level": 1,
             "done": "service", "asset": a["name"],
             "detail": f"{a['name']}: service due, {a['schedule']}, last {a['last']}"}
            for a in records_store.due_assets(now.replace(tzinfo=None))]


def _reminders(now: dt.datetime) -> list[dict]:
    import reminders_store
    out = []
    for r in reminders_store.pending():
        try:
            due = dt.datetime.fromisoformat(r["due_at"])
        except (ValueError, TypeError):
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=now.tzinfo)
        if due - now <= dt.timedelta(hours=24):
            when = due.strftime("%H:%M") if due.date() == now.date() else due.strftime("%a %H:%M")
            out.append({"title": _clean(r["text"]), "sub": when, "level": 1 if due <= now else 0})
    return out


_SOURCES = (_box, _pups, _garden, _assets, _reminders)
_home_cache: tuple[float, list[dict]] | None = None


def forget_home() -> None:
    global _home_cache
    _home_cache = None


def home_items(now: dt.datetime | None = None, fresh: bool = False) -> list[dict]:
    """Everything Hestia knows needs a person, most urgent first. Each source fails alone:
    a dead HA must not blank the due assets, and a failure is itself shown."""
    global _home_cache
    if not fresh and _home_cache and time.monotonic() - _home_cache[0] < HOME_CACHE_S:
        return _home_cache[1]
    now = now or dt.datetime.now().astimezone()
    items: list[dict] = []
    for source in _SOURCES:
        try:
            items.extend(source(now))
        except Exception as e:  # noqa: BLE001 — one broken watch must not blank the board
            print(f"board: {source.__name__} failed: {e}", file=sys.stderr)
            items.append({"title": f"Can't read {source.__name__.strip('_')}", "sub": "check the brain",
                          "level": 1})
    for i in items:
        i.setdefault("key", "home:" + i["title"])
    items.sort(key=lambda r: -r["level"])
    _home_cache = (time.monotonic(), items)
    return items


# ── memory ───────────────────────────────────────────────────────────────────────────
# The note-taker proposes durable facts into an inbox; nothing is remembered until a person
# keeps it. The board is one more place to say keep or discard, beside `review_notes.py`.

MEMORY_CHOICES = (("Keep", "promote"), ("Discard", "discard"))


def memory_items() -> list[dict]:
    import review_notes
    out = []
    for p in review_notes.proposals():
        kind = str(p["meta"].get("type", "note"))
        out.append({"title": p["body"], "sub": kind, "level": 1, "key": "memory:" + p["id"],
                    "memory": p["id"], "detail": p["body"], "choices": MEMORY_CHOICES})
    return out


def read_memory() -> list[dict]:
    try:
        return memory_items()
    except Exception as e:  # noqa: BLE001 — a bad inbox file must not blank the board
        print(f"board: memory inbox failed: {e}", file=sys.stderr)
        return []


# ── mood ─────────────────────────────────────────────────────────────────────────────────

def mood(home: list[dict], now: dt.datetime) -> str:
    top = max((i["level"] for i in home), default=0)
    if top >= 2:
        return "concerned"
    if now.hour >= NIGHT[0] or now.hour < NIGHT[1]:
        return "asleep"
    return "attentive" if top == 1 else "content"


def headline(home: list[dict], m: str) -> str:
    if m == "concerned":
        return next(i["title"] for i in home if i["level"] >= 2)
    n = sum(1 for i in home if i["level"] == 1)
    if n:
        return f"{n} thing{'s' if n > 1 else ''} at home today"
    return "Resting, all quiet" if m == "asleep" else "All quiet at home"


# ── drawing ──────────────────────────────────────────────────────────────────────────────

def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int, max_lines: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        cur = w
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    used = " ".join(lines).split()
    if len(used) < len(words) and lines:
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > width:
            last = last[:-1]
        lines[-1] = last.rstrip(" ,;:") + "…"
    return lines


def draw_character(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], m: str) -> None:
    """A banked ember with a face. Placeholder art: the expression carries the mood, and the
    shape is simple enough to read at arm's length on a 16-gray panel."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    cx = x0 + w // 2
    body = (x0 + 6, y0 + h * 0.30, x1 - 6, y1 - 4)
    # the flame tip, drawn first so the body's outline covers the seam
    tip_h = h * 0.42 if m != "asleep" else h * 0.22
    draw.polygon([(cx - w * 0.26, body[1] + h * 0.18), (cx + w * 0.02, y1 - h * 0.30 - tip_h - h * 0.14),
                  (cx + w * 0.26, body[1] + h * 0.18)], fill=LIGHT if m != "concerned" else MID)
    draw.ellipse(body, fill=PAPER, outline=INK, width=5)
    ey = body[1] + (body[3] - body[1]) * 0.42
    ex = w * 0.17
    my = body[1] + (body[3] - body[1]) * 0.70
    if m == "content":
        for s in (-1, 1):
            draw.arc((cx + s * ex - 10, ey - 6, cx + s * ex + 10, ey + 12), 200, 340, fill=INK, width=4)
        draw.arc((cx - 16, my - 14, cx + 16, my + 8), 20, 160, fill=INK, width=4)
    elif m == "attentive":
        for s in (-1, 1):
            draw.ellipse((cx + s * ex - 6, ey - 7, cx + s * ex + 6, ey + 7), fill=INK)
        draw.line((cx - 12, my, cx + 12, my), fill=INK, width=4)
    elif m == "concerned":
        for s in (-1, 1):
            draw.ellipse((cx + s * ex - 7, ey - 6, cx + s * ex + 7, ey + 8), fill=INK)
            draw.line((cx + s * ex - s * 12, ey - 20, cx + s * ex + s * 8, ey - 13), fill=INK, width=4)
        draw.ellipse((cx - 8, my - 8, cx + 8, my + 8), outline=INK, width=4)
    else:  # asleep
        for s in (-1, 1):
            draw.line((cx + s * ex - 9, ey + 2, cx + s * ex + 9, ey + 2), fill=INK, width=4)
        draw.line((cx - 7, my, cx + 7, my), fill=INK, width=3)
        z = _font("bold", 22)
        draw.text((x1 - 16, y0 + 8), "z", font=z, fill=MID)
        draw.text((x1 - 2, y0 - 8), "z", font=_font("bold", 16), fill=LIGHT)


def _column(draw, x: int, y: int, w: int, bottom: int, label: str, items: list[dict], home: bool,
            selected: str | None = None, hits: list | None = None) -> None:
    head = _font("bold", 22)
    draw.text((x, y), label, font=head, fill=INK)
    count = str(len(items))
    draw.text((x + w - draw.textlength(count, font=head), y), count, font=head, fill=MID)
    y += 34
    draw.line((x, y, x + w, y), fill=INK, width=3)
    y += 14

    title_f, sub_f = _font("medium", 21), _font("regular", 16)
    more_f = _font("regular", 17)
    reserve = 30  # room for "+N more"
    shown = 0
    for i, item in enumerate(items):
        level = item.get("level", 1)
        lines = _wrap(draw, item["title"], title_f, w - 22, 2)
        sub = item.get("sub") or ""
        if not home:
            sub = item["project"] + (f" · {item['age']}d" if item["age"] > STALE_DAYS else "")
            if item.get("choices"):
                sub += " · review"
        h = len(lines) * 26 + (22 if sub else 0) + 14
        if y + h > bottom - (reserve if i < len(items) - 1 else 0):
            break
        top = y
        chosen = item.get("key") is not None and item.get("key") == selected
        if chosen:
            draw.rectangle((x - 8, y - 6, x + w + 4, y + h - 8), fill=INK)
        # the bullet says how much it matters: filled square, filled dot, hollow dot
        by = y + 9
        mark = PAPER if chosen else INK
        if home and level >= 2:
            draw.rectangle((x, by - 2, x + 12, by + 10), fill=mark)
        elif home and level == 0:
            draw.ellipse((x + 1, by, x + 11, by + 10), outline=LIGHT if chosen else MID, width=2)
        else:
            draw.ellipse((x + 1, by, x + 11, by + 10), fill=mark)
        color = PAPER if chosen else (MID if home and level == 0 else INK)
        for ln in lines:
            draw.text((x + 22, y), ln, font=title_f, fill=color)
            y += 26
        if sub:
            draw.text((x + 22, y + 1), sub, font=sub_f, fill=LIGHT if chosen else MID)
            y += 22
        y += 14
        shown += 1
        if hits is not None:
            hits.append({"box": (x - 8, top - 7, x + w + 8, y - 7), "item": item})
    if shown < len(items):
        draw.text((x + 22, bottom - 24), f"+{len(items) - shown} more", font=more_f, fill=MID)


def render(queue: list[dict], home: list[dict], now: dt.datetime, selected: str | None = None,
           note: str | None = None, hits: list | None = None,
           memory: list[dict] | None = None) -> Image.Image:
    """`selected` is the key of a tapped item (drawn inverted), `note` a line for the foot of
    the board, and `hits`, when given, is filled with each drawn item's box for tap lookup.
    A MEMORY column appears only while the inbox holds proposals."""
    img = Image.new("L", SIZE, PAPER)
    draw = ImageDraw.Draw(img)
    m = mood(home, now)

    draw_character(draw, (MARGIN, 14, MARGIN + 100, 14 + 104), m)
    tx = MARGIN + 128
    draw.text((tx, 24), now.strftime("%A, %B ") + str(now.day), font=_font("bold", 36), fill=INK)
    draw.text((tx, 72), headline(home, m), font=_font("regular", 26),
              fill=INK if m == "concerned" else MID)
    draw.line((MARGIN, HEADER_H, SIZE[0] - MARGIN, HEADER_H), fill=INK, width=2)

    memory = memory or []
    cols = [("HANDS", [q for q in queue if q["column"] == "hands"], False),
            ("SCREEN", [q for q in queue if q["column"] == "screen"], False),
            ("HOME", home, True)]
    if memory:
        cols.append(("MEMORY", memory, True))
    col_w = (SIZE[0] - 2 * MARGIN - (len(cols) - 1) * GUTTER) // len(cols)
    top, bottom = HEADER_H + 20, SIZE[1] - FOOTER_H
    for n, (label, items, is_home) in enumerate(cols):
        x = MARGIN + n * (col_w + GUTTER)
        if n:
            draw.line((x - GUTTER // 2, top, x - GUTTER // 2, bottom - 6), fill=LIGHT, width=1)
        _column(draw, x, top, col_w, bottom, label, items, is_home, selected, hits)

    stamp = f"updated {now.strftime('%H:%M')}"
    f = _font("regular", 15)
    draw.text((SIZE[0] - MARGIN - draw.textlength(stamp, font=f), SIZE[1] - 24), stamp, font=f, fill=LIGHT)
    chosen = next((i for _, items, _ in cols for i in items if i.get("key") == selected), None)
    choices = (chosen or {}).get("choices") or ()
    if note:
        nf = _font("medium", 22)
        btn_w, btn_h = 150, 56
        text_w = SIZE[0] - 2 * MARGIN - 8 - len(choices) * (btn_w + 16)
        lines = _wrap(draw, note, nf, text_w, 3 if choices else 2)
        band = max(24 + 30 * len(lines), btn_h + 28 if choices else 0)
        draw.rectangle((0, SIZE[1] - band, SIZE[0], SIZE[1]), fill=INK)
        for n, ln in enumerate(lines):
            draw.text((MARGIN, SIZE[1] - band + 12 + 30 * n), ln, font=nf, fill=PAPER)
        # buttons sit in the band, so they are hit-tested before the items underneath it
        bf = _font("bold", 24)
        bx1 = SIZE[0] - MARGIN
        for n, (label, action) in enumerate(reversed(choices)):
            x1 = bx1 - n * (btn_w + 16)
            box = (x1 - btn_w, SIZE[1] - band + (band - btn_h) // 2, x1, SIZE[1] - band + (band + btn_h) // 2)
            primary = n == len(choices) - 1
            draw.rectangle(box, fill=PAPER if primary else INK, outline=PAPER, width=3)
            tw = draw.textlength(label, font=bf)
            draw.text((box[0] + (btn_w - tw) / 2, box[1] + 12), label, font=bf, fill=INK if primary else PAPER)
            if hits is not None:
                hits.insert(0, {"box": box, "item": {"key": f"choice:{action}", "choice": action,
                                                     "target": chosen, "title": label}})
    return img


def for_device(img: Image.Image, device: str | None) -> Image.Image:
    """The panel's native frame. The Paperwhite 1 is 758x1024 portrait with 16 gray levels;
    anything finer is dithered by the panel anyway, so quantize here where it can be seen."""
    if device == "kindle":
        img = img.rotate(90, expand=True)
        img = img.point(lambda v: (v // 17) * 17)
    return img


def frame(device: str | None = None, now: dt.datetime | None = None, selected: str | None = None,
          note: str | None = None) -> tuple[bytes, list[dict]]:
    """The board as PNG bytes plus the hit map, in board coordinates (landscape)."""
    now = now or dt.datetime.now().astimezone()
    hits: list[dict] = []
    img = for_device(render(read_queue(now.date()), home_items(now), now, selected, note, hits,
                            read_memory()), device)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), hits


def png_bytes(device: str | None = None, now: dt.datetime | None = None) -> bytes:
    return frame(device, now)[0]


def from_panel(px: int, py: int) -> tuple[int, int]:
    """A touch on the Kindle's portrait panel, in board coordinates. The board is rotated a
    quarter turn onto the panel (power button on the left), so panel y runs along board x."""
    return SIZE[0] - py, px


def hit_at(hits: list[dict], bx: int, by: int) -> dict | None:
    for h in hits:
        x0, y0, x1, y1 = h["box"]
        if x0 <= bx <= x1 and y0 <= by <= y1:
            return h["item"]
    return None


# ── the same board as a read-only page ───────────────────────────────────────────────
# For the phone: the board's columns as text on the tailnet, refreshing itself. No buttons on
# purpose; closing things stays on the Kindle, where a tap is two deliberate touches.

REFRESH_S = 60


def snapshot(now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now().astimezone()
    queue, home, memory = read_queue(now.date()), home_items(now), read_memory()
    m = mood(home, now)

    def row(i: dict, home_col: bool = False) -> dict:
        if home_col:
            return {"title": i["title"], "sub": i.get("sub", ""), "level": i.get("level", 1)}
        sub = i["project"] + (f" · {i['age']}d" if i["age"] > STALE_DAYS else "")
        return {"title": i["title"], "sub": sub + (" · review" if i.get("choices") else ""),
                "level": 1, "age": i["age"]}

    return {"updated": now.isoformat(timespec="seconds"), "mood": m, "headline": headline(home, m),
            "columns": {"hands": [row(q) for q in queue if q["column"] == "hands"],
                        "screen": [row(q) for q in queue if q["column"] == "screen"],
                        "home": [row(i, True) for i in home],
                        "memory": [row(i, True) for i in memory]}}


def queue_page(now: dt.datetime | None = None) -> str:
    import html
    snap = snapshot(now)
    when = dt.datetime.fromisoformat(snap["updated"])
    sections = []
    for name, rows in snap["columns"].items():
        if name == "memory" and not rows:
            continue
        items = "".join(
            f'<li class="l{r["level"]}"><span class="t">{html.escape(r["title"])}</span>'
            f'<span class="s">{html.escape(r["sub"])}</span></li>' for r in rows) or '<li class="none">Nothing here</li>'
        sections.append(f'<section><h2>{name.upper()} <span class="n">{len(rows)}</span></h2><ul>{items}</ul></section>')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hestia queue</title>
<style>
  :root {{ --bg:#f6f4ef; --fg:#1b1b1b; --mid:#6b6b6b; --line:#d9d5cc; --hot:#1b1b1b; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#14171a; --fg:#ececec; --mid:#9aa0a6; --line:#2b3035; --hot:#ececec; }} }}
  body {{ margin:0; padding:20px 16px 48px; background:var(--bg); color:var(--fg);
         font:17px/1.35 -apple-system, system-ui, sans-serif; }}
  header {{ margin-bottom:18px; }}
  h1 {{ font-size:1.35rem; margin:0; }}
  .head {{ color:var(--mid); margin-top:2px; }}
  .head.concerned {{ color:var(--fg); font-weight:600; }}
  main {{ display:grid; gap:22px; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); }}
  h2 {{ font-size:.85rem; letter-spacing:.08em; margin:0 0 6px; padding-bottom:6px;
        border-bottom:2px solid var(--fg); display:flex; justify-content:space-between; }}
  .n {{ color:var(--mid); }}
  ul {{ list-style:none; margin:0; padding:0; }}
  li {{ padding:9px 0; border-bottom:1px solid var(--line); }}
  .t {{ display:block; }}
  .s {{ display:block; color:var(--mid); font-size:.85rem; margin-top:2px; }}
  li.l2 .t {{ font-weight:700; }}
  li.l2 .t::before {{ content:"■ "; }}
  li.l0 .t {{ color:var(--mid); }}
  li.none {{ color:var(--mid); }}
  footer {{ color:var(--mid); font-size:.8rem; margin-top:24px; }}
</style></head>
<body>
<div id="board">
<header><h1>{when.strftime("%A, %B ")}{when.day}</h1>
<div class="head {snap["mood"]}">{html.escape(snap["headline"])}</div></header>
<main>{"".join(sections)}</main>
<footer>Read-only. Updated {when.strftime("%H:%M")}; refreshes every minute.</footer>
</div>
<script>
// Swap in a fresh copy every minute without a full reload, so the page keeps its scroll.
setInterval(async () => {{
  try {{
    const r = await fetch(location.pathname, {{cache: "no-store"}});
    if (!r.ok) return;
    const doc = new DOMParser().parseFromString(await r.text(), "text/html");
    const next = doc.getElementById("board");
    if (next) document.getElementById("board").replaceWith(next);
  }} catch (e) {{}}
}}, {REFRESH_S * 1000});
</script>
</body></html>"""


# ── closing things from the board ────────────────────────────────────────────────────────

def _exact_row(lines: list[str], line: str) -> int:
    at = [i for i, ln in enumerate(lines) if ln == line]
    if len(at) != 1:
        raise ValueError("that row changed since the board was drawn")
    return at[0]


def _write(path: Path, lines: list[str]) -> None:
    tmp = path.with_name(path.name + ".board-tmp")
    tmp.write_text("\n".join(lines))
    tmp.replace(path)


def trash_queue_row(line: str, path: Path | None = None) -> str:
    """Delete a row that should never have stayed: no Shipped entry, because nothing shipped.
    It is one tap, so the row is appended to a trash log first and a misfire can be put back."""
    path = (path or BOARD_QUEUE).resolve()
    lines = path.read_text().split("\n")
    at = _exact_row(lines, line)
    TRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with TRASH_LOG.open("a") as f:
        f.write(f"{dt.datetime.now().isoformat(timespec='seconds')}\t{line}\n")
    del lines[at]
    _write(path, lines)
    return title_of([c.strip() for c in line.strip().strip("|").split("|")][2])


def close_queue_row(line: str, today: dt.date, path: Path | None = None) -> str:
    """Move one Open row to Shipped, reshaped to the Shipped table's three columns. Refuses
    unless the exact line is there once: a board frame can be minutes old, and a row that
    moved or changed since must not be guessed at."""
    path = (path or BOARD_QUEUE).resolve()
    lines = path.read_text().split("\n")
    at = _exact_row(lines, line)
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    project, title = cells[1], title_of(cells[2])
    del lines[at]
    head = next((i for i, ln in enumerate(lines) if ln.lower().startswith("## shipped")), None)
    sep = next((i for i in range(head, len(lines)) if lines[i].startswith("|---")), None) if head is not None else None
    if sep is None:
        raise ValueError("no Shipped table to move it to")
    lines.insert(sep + 1, f"| {today.isoformat()} | {project} | {title} (closed from the board) |")
    _write(path, lines)
    return title


def complete(item: dict, choice: str | None = None, now: dt.datetime | None = None) -> str:
    """Do what a confirmed tap means. Returns the line the board shows afterwards."""
    now = now or dt.datetime.now().astimezone()
    if item.get("memory"):
        import review_notes
        if choice == "promote":
            review_notes.cmd_promote([item["memory"]])
            return f"Kept: {item['title']}"
        if choice == "discard":
            review_notes.cmd_discard([item["memory"]])
            return f"Discarded: {item['title']}"
        raise ValueError("keep or discard?")
    if item.get("done") == "queue":
        if choice == "keep":
            keep_row(item["line"], now.date())
            return f"Kept: {item['title']}. Asking again in {REVIEW_DAYS} days"
        if choice == "trash":
            return f"Trashed: {trash_queue_row(item['line'])}"
        return f"Done: {close_queue_row(item['line'], now.date())}"
    if item.get("done") == "service":
        import records_store
        records_store.log_event("service", subject=item["asset"], action="serviced",
                                detail="closed from the board", subject_kind="asset",
                                strict_subject=True)
        forget_home()
        return f"Logged service: {item['asset']}"
    raise ValueError("nothing to close on this one")


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Render the board to a PNG.")
    ap.add_argument("--out", default="board.png")
    ap.add_argument("--device", choices=["kindle"], default=None)
    args = ap.parse_args()
    Path(args.out).write_bytes(png_bytes(args.device))
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
