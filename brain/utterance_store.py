"""Utterance log: what people said to Hestia, word for word, from every window.

Off unless HESTIA_UTTERANCE_LOG=1. It exists to collect real requests (spoken ones with
Whisper's mistakes included) for testing a small on-device router, so it keeps only the
person's words, where they came from, and the line Hestia said just before when the words are
an answer to it ("kitchen" means nothing without "Which room?"). No replies, no tool results.

It lives in hestia.db beside the threads, so it shares records_store's 0600 perms and the
nightly backup. Review it with:

    cd brain && uv run python utterance_store.py dump [--since 2026-10-15]
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime

import records_store as store

MAX_TEXT = 2000
_SCHEMA = """
CREATE TABLE IF NOT EXISTS utterances (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, source TEXT NOT NULL,
    text TEXT NOT NULL, prior TEXT);
"""
_ready: set[str] = set()


def enabled() -> bool:
    return os.environ.get("HESTIA_UTTERANCE_LOG") == "1"


def _conn():
    c = store._conn()
    key = str(store.DB_PATH)
    if key not in _ready:
        c.executescript(_SCHEMA)
        _ready.add(key)
    return c


def source_of(user_agent: str, thread_mode: bool) -> str:
    """Which window: the app sends a thread, Home Assistant's session names itself."""
    if thread_mode:
        return "app"
    if user_agent.startswith("HomeAssistant"):
        return "voice"
    return "other"


def record(messages: list, source: str) -> None:
    """Log the newest user message, and Hestia's line before it when there is one."""
    if not enabled() or not messages:
        return
    last = messages[-1]
    if last.get("role") != "user" or not isinstance(last.get("content"), str):
        return
    prior = None
    if len(messages) > 1 and messages[-2].get("role") == "assistant":
        p = messages[-2].get("content")
        prior = p[:MAX_TEXT] if isinstance(p, str) else None
    with _conn() as c:
        c.execute("INSERT INTO utterances (ts, source, text, prior) VALUES (?, ?, ?, ?)",
                  (time.time(), source, last["content"][:MAX_TEXT], prior))


def since(ts: float = 0) -> list[dict]:
    rows = _conn().execute("SELECT * FROM utterances WHERE ts >= ? ORDER BY id", (ts,)).fetchall()
    return [dict(r) for r in rows]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump", help="print the log, one utterance per line")
    d.add_argument("--since", help="YYYY-MM-DD")
    a = ap.parse_args()
    start = datetime.strptime(a.since, "%Y-%m-%d").timestamp() if a.since else 0
    for r in since(start):
        when = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M")
        after = f"   (after: {r['prior'][:80]!r})" if r["prior"] else ""
        print(f"{when}  {r['source']:5}  {r['text']}{after}")


if __name__ == "__main__":
    main()
