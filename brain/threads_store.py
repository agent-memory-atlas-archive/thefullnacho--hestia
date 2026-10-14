"""Conversation threads: the chat history every window shares.

One brain, many thin windows. The phone app used to keep each conversation in its own
localStorage, so a thread started on the phone was invisible to the terminal and a cleared
Safari lost it. The history lives here instead, in hestia.db beside the records, so it shares
records_store's connection authority, 0600 perms and the nightly backup with no new file.

The brain also owns the context window: a thread-mode turn sends only the new message and
history() hands back the tail that fits, trimmed by bytes rather than by message count.
"""
from __future__ import annotations

import json
import time
import uuid

import context_budget
import records_store as store

MAIN = "main"
MAIN_TITLE = "Main chat"
DEFAULT_TITLE = "New chat"
MAX_TITLE = 120
MAX_IMPORT = 500             # messages accepted in one localStorage import
MAX_CONTENT = 24000          # bytes per message, the same cap validate_messages enforces
# Headroom under validate_messages' 100-message / 128000-byte ceiling, so the assembled
# request always passes it with room for the new message's JSON framing.
HISTORY_MESSAGES = 80
HISTORY_BYTES = 96000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS thread_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    role TEXT NOT NULL, content TEXT NOT NULL, receipts TEXT, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS thread_messages_by_thread ON thread_messages(thread_id, id);
"""
_ready: set[str] = set()


def _conn():
    c = store._conn()
    key = str(store.DB_PATH)
    if key not in _ready:
        c.executescript(_SCHEMA)
        _ready.add(key)
    now = time.time()
    c.execute("INSERT OR IGNORE INTO threads VALUES (?, ?, ?, ?)", (MAIN, MAIN_TITLE, now, now))
    return c


def _row(c, r) -> dict:
    count = c.execute("SELECT COUNT(*) FROM thread_messages WHERE thread_id=?", (r["id"],)).fetchone()[0]
    return {"id": r["id"], "title": r["title"], "created": r["created"], "updated": r["updated"],
            "count": count}


def _title(title) -> str:
    title = " ".join(str(title or "").split())[:MAX_TITLE]
    return title or DEFAULT_TITLE


def clean_messages(messages) -> list[dict] | None:
    """Imported history reduced to {role, content}; None when it isn't a usable list."""
    if not isinstance(messages, list) or len(messages) > MAX_IMPORT:
        return None
    out = []
    for m in messages:
        if (not isinstance(m, dict) or m.get("role") not in ("user", "assistant")
                or not isinstance(m.get("content"), str)):
            return None
        if len(m["content"].encode()) > MAX_CONTENT:
            return None
        if m["content"].strip():
            out.append({"role": m["role"], "content": m["content"]})
    return out


def list_threads() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM threads ORDER BY updated DESC, created DESC").fetchall()
        return [_row(c, r) for r in rows]


def get(thread_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        return _row(c, r) if r else None


def create(title=None, messages: list[dict] | None = None) -> dict:
    tid = "t_" + uuid.uuid4().hex[:16]
    now = time.time()
    with _conn() as c:
        c.execute("INSERT INTO threads VALUES (?, ?, ?, ?)", (tid, _title(title), now, now))
        c.executemany("INSERT INTO thread_messages(thread_id, role, content, receipts, created) "
                      "VALUES (?, ?, ?, NULL, ?)",
                      [(tid, m["role"], m["content"], now) for m in messages or []])
    return get(tid)


def messages(thread_id: str) -> list[dict] | None:
    with _conn() as c:
        if not c.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone():
            return None
        rows = c.execute("SELECT role, content, receipts, created FROM thread_messages "
                         "WHERE thread_id=? ORDER BY id", (thread_id,)).fetchall()
    return [{"role": r["role"], "content": r["content"],
             "receipts": json.loads(r["receipts"]) if r["receipts"] else None,
             "created": r["created"]} for r in rows]


def rename(thread_id: str, title) -> dict | None:
    with _conn() as c:
        c.execute("UPDATE threads SET title=?, updated=? WHERE id=?",
                  (_title(title), time.time(), thread_id))
    return get(thread_id)


def delete(thread_id: str) -> bool:
    with _conn() as c:
        c.execute("DELETE FROM thread_messages WHERE thread_id=?", (thread_id,))
        return c.execute("DELETE FROM threads WHERE id=?", (thread_id,)).rowcount > 0


def clear(thread_id: str) -> bool:
    with _conn() as c:
        if not c.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone():
            return False
        c.execute("DELETE FROM thread_messages WHERE thread_id=?", (thread_id,))
        c.execute("UPDATE threads SET updated=? WHERE id=?", (time.time(), thread_id))
    return True


def history(thread_id: str, new_message: dict) -> list[dict]:
    """The stored tail plus the new message, oldest exchanges dropped first until it fits.
    The tail always opens on a user message, so the model never sees an orphaned reply."""
    stored = [{"role": m["role"], "content": m["content"]} for m in messages(thread_id) or []]
    budget = HISTORY_BYTES - context_budget.estimate(new_message)
    tail: list[dict] = []
    size = 0
    for m in reversed(stored):
        cost = context_budget.estimate(m) + 2
        if len(tail) + 1 >= HISTORY_MESSAGES or size + cost > budget:
            break
        tail.insert(0, m)
        size += cost
    while tail and tail[0]["role"] != "user":
        tail.pop(0)
    return tail + [new_message]


def append_exchange(thread_id: str, user_text: str, reply: str, receipts: list | None) -> None:
    """Persist one finished turn atomically; first exchange names a still-default thread."""
    now = time.time()
    with _conn() as c:
        row = c.execute("SELECT title FROM threads WHERE id=?", (thread_id,)).fetchone()
        if not row:
            return
        first = not c.execute("SELECT 1 FROM thread_messages WHERE thread_id=?", (thread_id,)).fetchone()
        c.execute("INSERT INTO thread_messages(thread_id, role, content, receipts, created) "
                  "VALUES (?, 'user', ?, NULL, ?)", (thread_id, user_text, now))
        c.execute("INSERT INTO thread_messages(thread_id, role, content, receipts, created) "
                  "VALUES (?, 'assistant', ?, ?, ?)",
                  (thread_id, reply, json.dumps(receipts) if receipts else None, now))
        title = row["title"]
        if first and title == DEFAULT_TITLE:
            snippet = " ".join(user_text.split())
            title = snippet if len(snippet) <= 28 else snippet[:26].rstrip() + "…"
        c.execute("UPDATE threads SET title=?, updated=? WHERE id=?", (title, now, thread_id))
