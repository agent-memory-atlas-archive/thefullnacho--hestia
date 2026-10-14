"""The feed's picks and reactions, in hestia.db beside the threads.

One row per pick the nightly job showed someone. A URL is shown to a person once, ever, so the
UNIQUE(person, url) constraint is also the dedupe. Today's set is the rows of the newest day;
older rows stay as the record of hearts and "not for me" taps the ranker learns from.
"""
from __future__ import annotations

import time

import records_store as store

REACTIONS = ("heart", "nope")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feed_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL, day TEXT NOT NULL, rank INTEGER NOT NULL,
    url TEXT NOT NULL, title TEXT NOT NULL, source TEXT NOT NULL,
    description TEXT, why TEXT NOT NULL, image TEXT, query TEXT,
    reaction TEXT, reacted REAL, created REAL NOT NULL,
    UNIQUE(person, url));
CREATE INDEX IF NOT EXISTS feed_items_by_day ON feed_items(person, day, rank);
"""
_ready: set[str] = set()
_COLS = ("id", "person", "day", "rank", "url", "title", "source", "description", "why",
         "image", "reaction")


def _conn():
    c = store._conn()
    key = str(store.DB_PATH)
    if key not in _ready:
        c.executescript(_SCHEMA)
        _ready.add(key)
    return c


def _item(r) -> dict:
    return {k: r[k] for k in _COLS}


def seen(person: str, before_day: str) -> set[str]:
    """URLs already shown to this person on earlier days. Today's own picks don't count, so a
    rerun of tonight's job can pick them again."""
    with _conn() as c:
        return {r["url"] for r in c.execute("SELECT url FROM feed_items WHERE person=? AND "
                                            "(day < ? OR reaction IS NOT NULL)", (person, before_day))}


def save_day(person: str, day: str, picks: list[dict]) -> int:
    """Store one day's picks in rank order. A rerun on the same day replaces that day's set."""
    now = time.time()
    with _conn() as c:
        c.execute("DELETE FROM feed_items WHERE person=? AND day=? AND reaction IS NULL", (person, day))
        n = 0
        for i, p in enumerate(picks):
            n += c.execute(
                "INSERT OR IGNORE INTO feed_items(person, day, rank, url, title, source, description, "
                "why, image, query, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (person, day, i, p["url"], p["title"], p["source"], p.get("description"),
                 p["why"], p.get("image"), p.get("query"), now)).rowcount
    return n


def latest(person: str) -> dict:
    with _conn() as c:
        row = c.execute("SELECT MAX(day) AS day FROM feed_items WHERE person=?", (person,)).fetchone()
        day = row["day"] if row else None
        if not day:
            return {"day": None, "items": []}
        rows = c.execute("SELECT * FROM feed_items WHERE person=? AND day=? ORDER BY rank",
                         (person, day)).fetchall()
    return {"day": day, "items": [_item(r) for r in rows]}


def get(item_id: int) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM feed_items WHERE id=?", (item_id,)).fetchone()
    return _item(r) if r else None


def react(item_id: int, reaction: str | None) -> dict | None:
    """Set or clear the heart / "not for me" on one item. Anything else is refused."""
    if reaction is not None and reaction not in REACTIONS:
        raise ValueError(f"reaction must be one of {REACTIONS} or null")
    with _conn() as c:
        c.execute("UPDATE feed_items SET reaction=?, reacted=? WHERE id=?",
                  (reaction, time.time() if reaction else None, item_id))
    return get(item_id)


def reactions(person: str, reaction: str, limit: int = 20) -> list[dict]:
    """The most recent items this person hearted (or waved off), newest first."""
    with _conn() as c:
        rows = c.execute("SELECT * FROM feed_items WHERE person=? AND reaction=? "
                         "ORDER BY reacted DESC LIMIT ?", (person, reaction, limit)).fetchall()
    return [_item(r) for r in rows]


def stale_images(before_day: str) -> list[str]:
    """Cached images of items older than before_day that nobody hearted, safe to delete."""
    with _conn() as c:
        rows = c.execute("SELECT image FROM feed_items WHERE day < ? AND image IS NOT NULL "
                         "AND (reaction IS NULL OR reaction != 'heart')", (before_day,)).fetchall()
        live = {r["image"] for r in c.execute(
            "SELECT image FROM feed_items WHERE image IS NOT NULL AND (day >= ? OR reaction='heart')",
            (before_day,))}
        c.execute("UPDATE feed_items SET image=NULL WHERE day < ? AND image IS NOT NULL "
                  "AND (reaction IS NULL OR reaction != 'heart')", (before_day,))
    return sorted({r["image"] for r in rows} - live)
