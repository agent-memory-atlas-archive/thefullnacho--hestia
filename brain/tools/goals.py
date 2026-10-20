"""`goals` tool — the model drafts household plans; people approve them; code tracks them.

"Get the greenhouse winter-ready by December 1" → `propose` with a title and a few dated
steps. The draft is inert until someone approves it on the Goals page (/app#goals), the same
draft-then-approve gate recipes use. After that the tool can read goals and tick a step done
when the user says they finished it; ticking is reversible from the page.

Dates are phrases passed verbatim ("Nov 15", "next Saturday", "in 2 weeks"); this tool turns
them into days with the reminder tool's parser, never the model.
"""
from __future__ import annotations

import datetime as dt
import re

import goals_store as store
from tools.reminder import parse_when

SCHEMA = {
    "type": "function",
    "function": {
        "name": "goals",
        "description": (
            "Household goals with dated steps. 'propose' drafts a plan for a goal the user "
            "describes: a short title, why it matters, an optional target date, and 3 to 8 "
            "concrete steps, each something one person can do in one sitting, the first one "
            "small enough to start this week. The draft waits for the user to review and "
            "approve it on the Goals page; say so, and never call it scheduled. 'list' shows "
            "goals and progress. 'show' gives one goal's steps. 'done' ticks off a step when the "
            "user says they finished it: pass their words for the step. For any date, pass the "
            "user's phrase verbatim ('Nov 15', 'next Saturday', 'in 2 weeks'); the tool works "
            "out the day."),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["propose", "list", "show", "done"]},
                "title": {"type": "string", "description": "for propose: the goal in a few words"},
                "why": {"type": "string", "description": "for propose: one sentence on why it matters"},
                "target": {"type": "string", "description": "for propose: when the whole goal should be done, as the user said it"},
                "steps": {
                    "type": "array",
                    "description": "for propose: the plan, in order",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "one concrete step"},
                            "due": {"type": "string", "description": "optional day for this step, as a phrase"},
                        },
                        "required": ["text"],
                    },
                },
                "goal": {"type": "string", "description": "for show or done: the goal's title or #id"},
                "step": {"type": "string", "description": "for done: the step the user finished, in their words, or its #id"},
            },
            "required": ["action"],
        },
    },
}

_REL = re.compile(r"^(?:in\s+)?(an?|\d+)\s+(day|days|week|weeks|month|months)(?:\s+from now)?$", re.I)


def _day(phrase: str | None, today: dt.date | None = None) -> str | None:
    """A date phrase -> 'YYYY-MM-DD'. Raises ValueError naming the phrase it couldn't read."""
    s = (phrase or "").strip()
    if not s:
        return None
    today = today or dt.date.today()
    m = _REL.match(s)
    if m:
        n = 1 if m.group(1).lower() in ("a", "an") else int(m.group(1))
        unit = m.group(2).lower().rstrip("s")
        days = {"day": 1, "week": 7, "month": 30}[unit] * n
        return (today + dt.timedelta(days=days)).isoformat()
    when = parse_when(s, now=dt.datetime.combine(today, dt.datetime.now().time()))
    if when is None:
        raise ValueError(f"I couldn't read the date '{s}'. Try 'Nov 15', 'next Saturday' or 'in 2 weeks'.")
    return when.date().isoformat()


def _when(iso: str | None) -> str:
    if not iso:
        return ""
    d = dt.date.fromisoformat(iso)
    return d.strftime("%a %b ") + str(d.day)


def _line(s: dict) -> str:
    mark = "done" if s["done_at"] else (f"due {_when(s['due'])}" if s["due"] else "no date")
    return f"  #{s['id']} {s['text']} ({mark})"


def _summary(g: dict) -> str:
    state = {"proposed": "awaiting approval on the Goals page", "active": "active",
             "done": "done", "dropped": "dropped"}[g["status"]]
    target = f", aim {_when(g['target'])}" if g["target"] else ""
    return f"#{g['id']} {g['title']}: {g['done']}/{len(g['steps'])} steps, {state}{target}"


def execute(action: str, title: str | None = None, why: str | None = None,
            target: str | None = None, steps: list | None = None,
            goal: str | None = None, step: str | None = None) -> str:
    if action == "propose":
        # An unreadable date costs that date, not the plan: the step stays undated and the
        # person can set it on the Goals page. A habit's "ongoing" target is the usual case.
        unread = []

        def lenient(phrase):
            try:
                return _day(phrase)
            except ValueError:
                unread.append(phrase.strip())
                return None
        try:
            plan = [{"text": s.get("text", ""), "due": lenient(s.get("due"))} for s in (steps or [])]
            g = store.propose(title or "", plan, why=why or "", target=lenient(target))
        except ValueError as e:
            return f"Error: {e}"
        lines = "\n".join(_line(s) for s in g["steps"])
        note = ("\nLeft undated (not a date I could read): " + "; ".join(f"'{u}'" for u in unread) +
                ". Set dates on the Goals page if they need one.") if unread else ""
        if not any(s["due"] for s in g["steps"]):
            # The first real plan (2026-10-20) came back with no dates: nothing would remind
            # anyone. Say so, so the model can tell the user or propose again with dates.
            note += ("\nNo step has a date, so no reminders will fire and nothing can go overdue. "
                     "Tell the user, and suggest dating at least the first steps on the Goals page.")
        return (f"Drafted goal #{g['id']} for review: {g['title']}, {len(g['steps'])} steps.\n"
                f"{lines}{note}\nNothing is scheduled until it is approved on the Goals page (/app#goals).")
    if action == "list":
        live = store.goals(("proposed", "active"))
        if not live:
            return "No goals yet."
        return "Goals:\n" + "\n".join(_summary(g) for g in live)
    if action == "show":
        g = store.find(goal) if goal else None
        if g is None:
            return f"No single goal matches '{goal}'. Say 'list my goals' to see them."
        return _summary(g) + ("\nWhy: " + g["why"] if g["why"] else "") + "\n" + "\n".join(_line(s) for s in g["steps"])
    if action == "done":
        if not (step or "").strip():
            return "Which step? Tell me what you finished."
        ref = step.strip().lstrip("#")
        if ref.isdigit():
            sid = int(ref)
        else:
            hits = store.match_open_steps(step)
            if goal:
                g = store.find(goal)
                hits = [h for h in hits if g and h["goal_id"] == g["id"]]
            if not hits:
                return f"No open goal step matches '{step}'. Say 'show' with the goal to see its steps."
            if len(hits) > 1 and len(store._words(hits[0]["text"]) & store._words(step)) == \
                    len(store._words(hits[1]["text"]) & store._words(step)):
                options = "; ".join(f"#{h['id']} {h['text']} ({h['goal_title']})" for h in hits[:4])
                return f"Which step did you mean? {options}"
            sid = hits[0]["id"]
        try:
            r = store.mark_done(sid)
        except (LookupError, ValueError) as e:
            return f"Error: {e}"
        s = r["step"]
        if r["already"]:
            return f"Already done: {s['text']} (goal: {s['goal_title']})."
        g = store.get(s["goal_id"])
        tail = " That was the last step, so the goal is done." if r["goal_done"] else ""
        return (f"Marked done: {s['text']} (goal: {g['title']}). {g['done']} of {len(g['steps'])} "
                f"steps done.{tail} It can be undone on the Goals page.")
    return f"Error: unknown goals action '{action}'."
