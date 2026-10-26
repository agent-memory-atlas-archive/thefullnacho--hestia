"""Sightings: every photo identified at /identify, what the classifiers said, and what it was.

This is the local in-field dataset. The machine's call is kept beside a person's label, so a
miss is visible and a confirmed photo can later train or test the forager_ml experts. Only a
person writes a label; the classifiers never do. Nothing here leaves the box (export is
undecided).

Rows live in hestia.db beside the threads, so they share records_store's 0600 perms and the
nightly backup. The photos are files under PHOTO_DIR/identify (see identify.keep).
"""
from __future__ import annotations

import json
import time

import records_store as store

MAX_LABEL = 80
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, photos TEXT NOT NULL,
    result TEXT NOT NULL, label TEXT, labeled_at REAL);
"""
_ready: set[str] = set()


def _conn():
    c = store._conn()
    key = str(store.DB_PATH)
    if key not in _ready:
        c.executescript(_SCHEMA)
        _ready.add(key)
    return c


def _row(r) -> dict:
    return {"id": r["id"], "ts": r["ts"], "photos": json.loads(r["photos"]),
            "result": json.loads(r["result"]), "label": r["label"], "labeled_at": r["labeled_at"]}


def add(photos: list[str], result: dict) -> int:
    """Record one identification. `photos` are paths relative to PHOTO_DIR."""
    with _conn() as c:
        cur = c.execute("INSERT INTO sightings(ts,photos,result) VALUES(?,?,?)",
                        (time.time(), json.dumps(photos), json.dumps(result)))
        return cur.lastrowid


def get(sid: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM sightings WHERE id=?", (sid,)).fetchone()
    return _row(r) if r else None


def label(sid: int, text: str) -> bool:
    """Set what the specimen really was. A later label replaces an earlier one."""
    text = " ".join(text.split())[:MAX_LABEL]
    if not text:
        return False
    with _conn() as c:
        cur = c.execute("UPDATE sightings SET label=?, labeled_at=? WHERE id=?",
                        (text, time.time(), sid))
        return cur.rowcount > 0


def recent(limit: int = 50) -> list[dict]:
    """Newest first, unlabeled ones ahead of labeled ones."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM sightings ORDER BY label IS NOT NULL, id DESC LIMIT ?",
                         (limit,)).fetchall()
    return [_row(r) for r in rows]
