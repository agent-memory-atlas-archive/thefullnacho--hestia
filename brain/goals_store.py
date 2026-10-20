"""Household goals: a plan the model drafts, the operator approves, and code tracks.

The model's part is judgment: turning "the greenhouse winter-ready by December" into a few
dated steps. Its draft lands here as `proposed` and does nothing until a person approves it
on the Goals page, editing steps on the way. From approval on it is all rows: each dated step
gets a 9am phone reminder, a done step cancels its reminder and writes a records event, and
overdue is a date comparison in the briefing. No timer, count or threshold goes to the model.

The model's original draft is kept beside the approved plan, and approval stores how much the
operator changed it. That edit count is the plan-quality signal: a draft accepted untouched is
a good plan, one rewritten step by step is not.

Shares hestia.db, its 0600 perms and the nightly backup with the records, like threads_store.
"""
from __future__ import annotations

import datetime as dt
import json
import re

import records_store as store

MAX_STEPS = 12
MAX_TEXT = 200
REMINDER_HOUR = 9           # a dated step pings the phone at 9am on its day

_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  why TEXT NOT NULL DEFAULT '',
  target TEXT,                     -- YYYY-MM-DD the whole goal is aimed at, optional
  status TEXT NOT NULL DEFAULT 'proposed',   -- proposed | active | done | dropped
  draft TEXT NOT NULL,             -- the model's plan as proposed, JSON, never edited
  review TEXT,                     -- what approval changed, JSON (the plan-quality signal)
  created_at TEXT NOT NULL,
  approved_at TEXT,
  closed_at TEXT
);
CREATE TABLE IF NOT EXISTS goal_steps (
  id INTEGER PRIMARY KEY,
  goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  ord INTEGER NOT NULL,
  text TEXT NOT NULL,
  due TEXT,                        -- YYYY-MM-DD, optional
  done_at TEXT,
  reminder_id INTEGER              -- the pending 9am push for this step, if any
);
CREATE INDEX IF NOT EXISTS idx_goal_steps_goal ON goal_steps(goal_id, ord);
"""
_ready: set[str] = set()


def _conn():
    c = store._conn()
    key = str(store.DB_PATH)
    if key not in _ready:
        c.executescript(_SCHEMA)
        _ready.add(key)
    return c


def _today() -> dt.date:
    return dt.date.today()


def _clean_steps(steps) -> list[dict]:
    """Validate a step list from the model or the page: 1..MAX_STEPS, text required, due an
    ISO date or empty. Raises ValueError with a sentence a person can act on."""
    if not isinstance(steps, list) or not steps:
        raise ValueError("A plan needs at least one step.")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"A plan holds at most {MAX_STEPS} steps; split the goal.")
    out = []
    for s in steps:
        if not isinstance(s, dict):
            raise ValueError("Each step needs text.")
        text = str(s.get("text") or "").strip()
        if not text:
            raise ValueError("Each step needs text.")
        if len(text) > MAX_TEXT:
            raise ValueError(f"A step is over {MAX_TEXT} characters.")
        due = (s.get("due") or "").strip() or None
        if due:
            try:
                due = dt.date.fromisoformat(due).isoformat()
            except ValueError:
                raise ValueError(f"'{due}' is not a date (YYYY-MM-DD).") from None
        out.append({"text": text, "due": due, "id": s.get("id")})
    return out


def _clean_title(title) -> str:
    title = str(title or "").strip()
    if not title:
        raise ValueError("A goal needs a title.")
    if len(title) > MAX_TEXT:
        raise ValueError(f"The title is over {MAX_TEXT} characters.")
    return title


def _clean_date(value) -> str | None:
    value = (value or "").strip() if isinstance(value, str) else value
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"'{value}' is not a date (YYYY-MM-DD).") from None


def _goal(c, row) -> dict:
    steps = [dict(s) for s in c.execute(
        "SELECT id,ord,text,due,done_at,reminder_id FROM goal_steps WHERE goal_id=? ORDER BY ord",
        (row["id"],))]
    g = {k: row[k] for k in ("id", "title", "why", "target", "status", "created_at",
                             "approved_at", "closed_at")}
    g["draft"] = json.loads(row["draft"])
    g["review"] = json.loads(row["review"]) if row["review"] else None
    g["steps"] = steps
    g["done"] = sum(1 for s in steps if s["done_at"])
    return g


# ----- the model's side: propose ---------------------------------------------

def propose(title: str, steps: list[dict], why: str = "", target: str | None = None) -> dict:
    """File the model's draft. It stays inert (no reminders, no records) until approved."""
    title = _clean_title(title)
    clean = _clean_steps(steps)
    target = _clean_date(target)
    draft = {"title": title, "why": why or "", "target": target,
             "steps": [{"text": s["text"], "due": s["due"]} for s in clean]}
    with _conn() as c:
        cur = c.execute("INSERT INTO goals(title,why,target,status,draft,created_at) "
                        "VALUES(?,?,?,'proposed',?,?)",
                        (title, why or "", target, json.dumps(draft), store._now()))
        gid = cur.lastrowid
        for i, s in enumerate(clean):
            c.execute("INSERT INTO goal_steps(goal_id,ord,text,due) VALUES(?,?,?,?)",
                      (gid, i, s["text"], s["due"]))
        return _goal(c, c.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())


# ----- the person's side: approve, discard -----------------------------------

def _review(draft_steps: list[dict], final: list[dict]) -> dict:
    """How far approval moved the plan from the draft. Steps keep their row id through the
    page, so an edit is told apart from a delete-and-add."""
    by_id = {s["id"]: s for s in draft_steps}
    kept = edited = redated = added = 0
    for s in final:
        old = by_id.get(s.get("id"))
        if old is None:
            added += 1
            continue
        text_changed = old["text"] != s["text"]
        date_changed = (old["due"] or None) != (s["due"] or None)
        edited += text_changed
        redated += date_changed
        kept += not (text_changed or date_changed)
    removed = len(set(by_id) - {s.get("id") for s in final})
    return {"drafted": len(draft_steps), "kept": kept, "edited": edited, "redated": redated,
            "added": added, "removed": removed, "approved": len(final)}


# Reminder rows are written on the goal's own connection: a second connection would wait on
# this one's open write and stall until the busy timeout.
def _add_reminder(c, due_at: str, text: str) -> int:
    return c.execute("INSERT INTO reminders(due_at,text,created_at,announce) VALUES(?,?,?,0)",
                     (due_at, text, store._now())).lastrowid


def _cancel_reminder(c, rid: int) -> None:
    c.execute("DELETE FROM reminders WHERE id=? AND fired_at IS NULL", (rid,))


def _schedule(c, goal_title: str, step_id: int, text: str, due: str | None) -> int | None:
    """The 9am push for a dated step, if that morning hasn't passed. Code, not the model."""
    if not due:
        return None
    when = dt.datetime.combine(dt.date.fromisoformat(due), dt.time(REMINDER_HOUR))
    if when <= dt.datetime.now():
        return None
    rid = _add_reminder(c, when.isoformat(timespec="seconds"), f"{text} (goal: {goal_title})")
    c.execute("UPDATE goal_steps SET reminder_id=? WHERE id=?", (rid, step_id))
    return rid


def approve(gid: int, title: str, steps: list[dict], why: str = "",
            target: str | None = None) -> dict:
    """Make a proposed goal live with the person's edits. Steps that carry an `id` are the
    draft's rows (possibly edited); steps without one are new."""
    title = _clean_title(title)
    final = _clean_steps(steps)
    target = _clean_date(target)
    with _conn() as c:
        row = c.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
        if row is None:
            raise LookupError(f"No goal #{gid}.")
        if row["status"] != "proposed":
            raise ValueError(f"Goal #{gid} is already {row['status']}.")
        draft_rows = [dict(s) for s in c.execute(
            "SELECT id,text,due FROM goal_steps WHERE goal_id=?", (gid,))]
        review = _review(draft_rows, final)
        review["title_changed"] = title != row["title"]
        c.execute("DELETE FROM goal_steps WHERE goal_id=?", (gid,))
        c.execute("UPDATE goals SET title=?,why=?,target=?,status='active',review=?,approved_at=? "
                  "WHERE id=?", (title, why or "", target, json.dumps(review), store._now(), gid))
        for i, s in enumerate(final):
            sid = c.execute("INSERT INTO goal_steps(goal_id,ord,text,due) VALUES(?,?,?,?)",
                            (gid, i, s["text"], s["due"])).lastrowid
            _schedule(c, title, sid, s["text"], s["due"])
        goal = _goal(c, c.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
    store.upsert_entity("goal", title)
    store.log_event("goal", subject=title, subject_kind="goal", action="approved",
                    detail=f"{len(final)} steps", attrs=review)
    return goal


def discard(gid: int) -> dict:
    """Drop a draft. The row stays (status dropped) so discarded plans count in the record."""
    with _conn() as c:
        row = c.execute("SELECT status FROM goals WHERE id=?", (gid,)).fetchone()
        if row is None:
            raise LookupError(f"No goal #{gid}.")
        if row["status"] not in ("proposed", "active"):
            raise ValueError(f"Goal #{gid} is already {row['status']}.")
        for s in c.execute("SELECT reminder_id FROM goal_steps WHERE goal_id=? AND done_at IS NULL "
                           "AND reminder_id IS NOT NULL", (gid,)).fetchall():
            _cancel_reminder(c, s["reminder_id"])
        c.execute("UPDATE goals SET status='dropped',closed_at=? WHERE id=?", (store._now(), gid))
        return _goal(c, c.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())


# ----- progress: done, undo -------------------------------------------------

def _step(c, sid: int):
    row = c.execute("SELECT s.*, g.title AS goal_title, g.status AS goal_status FROM goal_steps s "
                    "JOIN goals g ON g.id=s.goal_id WHERE s.id=?", (sid,)).fetchone()
    if row is None:
        raise LookupError(f"No goal step #{sid}.")
    return row


def mark_done(sid: int) -> dict:
    """Tick a step: stamp it, cancel its pending push, log it, close the goal on the last one."""
    with _conn() as c:
        s = _step(c, sid)
        if s["goal_status"] not in ("active", "done"):
            raise ValueError("That goal is not active.")
        if s["done_at"]:
            return {"step": dict(s), "goal_done": s["goal_status"] == "done", "already": True}
        if s["reminder_id"]:
            _cancel_reminder(c, s["reminder_id"])
        c.execute("UPDATE goal_steps SET done_at=?,reminder_id=NULL WHERE id=?", (store._now(), sid))
        left = c.execute("SELECT COUNT(*) FROM goal_steps WHERE goal_id=? AND done_at IS NULL",
                         (s["goal_id"],)).fetchone()[0]
        if left == 0:
            c.execute("UPDATE goals SET status='done',closed_at=? WHERE id=?", (store._now(), s["goal_id"]))
    store.log_event("goal", subject=s["goal_title"], subject_kind="goal", action="step done",
                    detail=s["text"])
    if left == 0:
        store.log_event("goal", subject=s["goal_title"], subject_kind="goal", action="goal done")
    return {"step": dict(s), "goal_done": left == 0, "already": False}


def undo_done(sid: int) -> dict:
    """Untick a step (a wrong tap or a wrong guess from chat). Re-arms its push if still ahead."""
    with _conn() as c:
        s = _step(c, sid)
        if not s["done_at"]:
            return {"step": dict(s), "already": True}
        c.execute("UPDATE goal_steps SET done_at=NULL WHERE id=?", (sid,))
        if s["goal_status"] == "done":
            c.execute("UPDATE goals SET status='active',closed_at=NULL WHERE id=?", (s["goal_id"],))
        _schedule(c, s["goal_title"], sid, s["text"], s["due"])
    store.log_event("goal", subject=s["goal_title"], subject_kind="goal", action="step reopened",
                    detail=s["text"])
    return {"step": dict(s), "already": False}


# ----- reads ----------------------------------------------------------------

def get(gid: int) -> dict:
    with _conn() as c:
        row = c.execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone()
        if row is None:
            raise LookupError(f"No goal #{gid}.")
        return _goal(c, row)


def goals(statuses=("proposed", "active"), limit: int = 50) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    with _conn() as c:
        rows = c.execute(f"SELECT * FROM goals WHERE status IN ({marks}) "
                         "ORDER BY CASE status WHEN 'proposed' THEN 0 ELSE 1 END, id DESC LIMIT ?",
                         (*statuses, limit)).fetchall()
        return [_goal(c, r) for r in rows]


def find(ref) -> dict | None:
    """A goal by id, exact title, or a title containing every word given. None if not exactly one."""
    live = goals(("proposed", "active", "done"))
    if isinstance(ref, int) or str(ref).strip().lstrip("#").isdigit():
        n = int(str(ref).strip().lstrip("#"))
        return next((g for g in live if g["id"] == n), None)
    words = _words(ref)
    exact = [g for g in live if g["title"].lower() == str(ref).strip().lower()]
    if exact:
        return exact[0]
    hits = [g for g in live if words and words <= _words(g["title"])]
    return hits[0] if len(hits) == 1 else None


_STOP = {"the", "a", "an", "to", "and", "of", "for", "on", "in", "my", "our", "it", "is",
         "i", "we", "up", "get", "got", "did", "done", "just", "finally", "with", "at", "by"}


def _words(text) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", str(text or "").lower()) if w not in _STOP and len(w) > 2}


def match_open_steps(text: str) -> list[dict]:
    """Open steps of active goals sharing at least two meaningful words with `text` (or every
    word of a one-word step), best first. Used to find the step a chat message means, and by
    the router to offer the goals tool when a message reads like a step being finished."""
    said = _words(text)
    if not said:
        return []
    with _conn() as c:
        rows = c.execute("SELECT s.id,s.text,s.due,g.title AS goal_title,g.id AS goal_id "
                         "FROM goal_steps s JOIN goals g ON g.id=s.goal_id "
                         "WHERE g.status='active' AND s.done_at IS NULL").fetchall()
    scored = []
    for r in rows:
        mine = _words(r["text"])
        shared = len(mine & said)
        if shared >= 2 or (mine and shared == len(mine)):
            scored.append((shared / max(len(mine), 1), shared, dict(r)))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [r for _, _, r in scored]


def overdue(today: dt.date | None = None) -> list[dict]:
    """Open steps of active goals whose day has passed, oldest first. Pure date comparison."""
    today = today or _today()
    with _conn() as c:
        rows = c.execute("SELECT s.id,s.text,s.due,g.title AS goal_title FROM goal_steps s "
                         "JOIN goals g ON g.id=s.goal_id WHERE g.status='active' "
                         "AND s.done_at IS NULL AND s.due IS NOT NULL AND s.due<? ORDER BY s.due",
                         (today.isoformat(),)).fetchall()
    return [{**dict(r), "days": (today - dt.date.fromisoformat(r["due"])).days} for r in rows]


def due_on(day: dt.date | None = None) -> list[dict]:
    day = day or _today()
    with _conn() as c:
        rows = c.execute("SELECT s.id,s.text,g.title AS goal_title FROM goal_steps s "
                         "JOIN goals g ON g.id=s.goal_id WHERE g.status='active' "
                         "AND s.done_at IS NULL AND s.due=? ORDER BY s.ord", (day.isoformat(),)).fetchall()
    return [dict(r) for r in rows]
