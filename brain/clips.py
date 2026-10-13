"""`clips` — video/audio clip intake for the property log (iOS Shortcut).

Additive module, mounted via APIRouter from hestia.py's marked clips block.
Follows the /ingest/photo house patterns: manual form parsing with clear
missing-field errors (never a bare 422), INGEST_TOKEN auth via
X-Ingest-Token header / Bearer / `token` form field.

Two capture modes from the one iOS Shortcut:
  Mode A (Share Sheet): video file + note/tags/lat/lon/taken_at.
  Mode B (standalone): NO file — just a timestamped, geotagged note
    (dictated text). Zero camera interaction.
An empty note lands with needs_note=1 so it surfaces in the review filter.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sqlite3
import subprocess
import uuid

import config
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

router = APIRouter()

CLIPS_DIR = config.DATA_DIR / "clips"
_MAX_CLIP_BYTES = 200 * 1024 * 1024  # 200 MB — a phone clip is tens of MB; reject the absurd
_VIDEO_PREFIXES = ("video/",)
_AUDIO_PREFIXES = ("audio/",)
_FFMPEG_TIMEOUT = 30


# --- storage (lazy schema, same pattern as records_store) ---
_schema_ready: set[str] = set()


def _conn() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    if str(config.DB_PATH) not in _schema_ready:
        c.execute(
            """CREATE TABLE IF NOT EXISTS clips (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL,
                thumb TEXT,
                original_name TEXT,
                sender TEXT,
                note TEXT,
                tags TEXT,
                lat REAL,
                lon REAL,
                taken_at TEXT,
                created_at TEXT NOT NULL,
                bytes INTEGER,
                duration REAL,
                needs_note INTEGER NOT NULL DEFAULT 0
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_clips_created ON clips(id DESC)")
        c.commit()
        _schema_ready.add(str(config.DB_PATH))
    return c


def _token_ok(request: Request, form) -> bool:
    """Same triple-source auth as /ingest/photo: header, Bearer, or form field."""
    auth = request.headers.get("authorization", "")
    token = (
        request.headers.get("x-ingest-token", "")
        or (auth[7:] if auth.lower().startswith("bearer ") else "")
        or str(form.get("token") or "")
    ).strip()
    want = os.environ.get("HESTIA_INGEST_TOKEN", "") or os.environ.get("INGEST_TOKEN", "")  # ingest.env provides INGEST_TOKEN (naming drift, 2026-10-13)
    return bool(want) and token == want


def _as_float(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _poster_for(video_path: str) -> str | None:
    """Pull one poster frame (~1s in, 320px wide). Returns path or None."""
    out = os.path.splitext(video_path)[0] + ".jpg"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", video_path,
             "-vframes", "1", "-vf", "scale=320:-1", out],
            timeout=_FFMPEG_TIMEOUT, check=True)
        return out if os.path.isfile(out) else None
    except Exception:
        return None


def _duration_of(media_path: str) -> float | None:
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", media_path],
            timeout=_FFMPEG_TIMEOUT, capture_output=True, text=True, check=True)
        return float(p.stdout.strip())
    except Exception:
        return None


@router.post("/api/clips")
async def clips_ingest(request: Request):
    """Multipart intake. `file` OPTIONAL (video/* or audio/*)."""
    form = await request.form()
    got = sorted(k for k in form.keys())
    if not _token_ok(request, form):
        return JSONResponse(status_code=401, content={
            "error": "missing or bad token",
            "hint": "send the token as header X-Ingest-Token, Authorization: Bearer, "
                    "or form field 'token'",
            "received_fields": got})

    file = form.get("file")
    has_file = file is not None and not isinstance(file, str)
    data = b""
    filename = ""
    if has_file:
        data = await file.read()
        filename = file.filename or ""
        if not data:
            return JSONResponse(status_code=400, content={"error": "empty file"})
        if len(data) > _MAX_CLIP_BYTES:
            return JSONResponse(status_code=413, content={"error": "file too large"})
        ctype = (file.content_type or "").lower()
        if not (ctype.startswith(_VIDEO_PREFIXES) or ctype.startswith(_AUDIO_PREFIXES)):
            return JSONResponse(status_code=400, content={
                "error": f"unsupported media type {ctype!r}",
                "hint": "send video/* or audio/* as the 'file' field"})

    sender = str(form.get("sender") or "").strip()
    note = str(form.get("note") or "").strip()
    tags = ",".join(t.strip() for t in str(form.get("tags") or "").split(",") if t.strip())
    lat = _as_float(form.get("lat"))
    lon = _as_float(form.get("lon"))
    taken_at = str(form.get("taken_at") or "").strip() or None
    if taken_at:
        try:
            _dt.datetime.fromisoformat(taken_at.replace("Z", "+00:00"))
        except ValueError:
            taken_at = None
    needs_note = 1 if not note else 0

    # Date bucket from taken_at when valid, else today (local box time).
    try:
        day = _dt.datetime.fromisoformat(
            (taken_at or "").replace("Z", "+00:00")).strftime("%Y-%m-%d") if taken_at else None
    except ValueError:
        day = None
    day = day or _dt.datetime.now().strftime("%Y-%m-%d")

    path = ""
    thumb = None
    duration = None
    if has_file:
        ext = os.path.splitext(filename)[1].lower() or (
            ".mp4" if (file.content_type or "").startswith("video/") else ".m4a")
        ext = re.sub(r"[^a-z0-9.]", "", ext) or ".bin"
        dest_dir = CLIPS_DIR / day
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%H%M%S-%f")
        dest = dest_dir / f"{stamp}-{uuid.uuid4().hex[:8]}{ext}"
        dest.write_bytes(data)
        path = str(dest)
        if (file.content_type or "").lower().startswith(_VIDEO_PREFIXES):
            thumb = _poster_for(path)
        duration = _duration_of(path)

    created_at = _dt.datetime.now().isoformat(timespec="seconds")
    c = _conn()
    cur = c.execute(
        """INSERT INTO clips
           (path, thumb, original_name, sender, note, tags, lat, lon,
            taken_at, created_at, bytes, duration, needs_note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (path, thumb, filename or None, sender or None, note or None,
         tags or None, lat, lon, taken_at, created_at,
         len(data) if has_file else 0, duration, needs_note))
    clip_id = cur.lastrowid
    c.commit()
    c.close()
    return {"ok": True, "id": clip_id, "needs_note": needs_note,
            "file_url": f"/api/clips/file/{clip_id}" if has_file else None,
            "thumb_url": f"/api/clips/thumb/{clip_id}" if thumb else None}


def _row_to_json(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "sender": r["sender"], "note": r["note"],
            "tags": r["tags"], "lat": r["lat"], "lon": r["lon"],
            "taken_at": r["taken_at"], "created_at": r["created_at"],
            "bytes": r["bytes"], "duration": r["duration"],
            "needs_note": r["needs_note"],
            "file_url": f"/api/clips/file/{r['id']}" if r["path"] else None,
            "thumb_url": f"/api/clips/thumb/{r['id']}" if r["thumb"] else None,
            "is_audio": bool(r["path"]) and not r["thumb"]}


@router.get("/api/clips")
async def clips_list(tag: str = "", needs_note: str = ""):
    c = _conn()
    q = "SELECT * FROM clips"
    clauses, args = [], []
    if tag.strip():
        clauses.append("tags LIKE ?")
        args.append(f"%{tag.strip()}%")
    if needs_note.strip() == "1":
        clauses.append("needs_note = 1")
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY id DESC"
    rows = [_row_to_json(r) for r in c.execute(q, args).fetchall()]
    c.close()
    return {"clips": rows, "count": len(rows)}


def _clip_path(clip_id: int, col: str):
    c = _conn()
    r = c.execute("SELECT path, thumb FROM clips WHERE id = ?", (clip_id,)).fetchone()
    c.close()
    if not r:
        return None
    p = r[col]
    if not p:
        return None
    # Contain to the clips dir: resolved path must stay inside it.
    base = CLIPS_DIR.resolve()
    try:
        rp = __import__("pathlib").Path(p).resolve()
    except Exception:
        return None
    if base not in rp.parents and rp != base:
        return None
    return str(rp) if rp.is_file() else None


@router.get("/api/clips/file/{clip_id}")
async def clips_file(clip_id: int):
    p = _clip_path(clip_id, "path")
    if not p:
        return JSONResponse(status_code=404, content={"error": "clip not found"})
    return FileResponse(p)


@router.get("/api/clips/thumb/{clip_id}")
async def clips_thumb(clip_id: int):
    p = _clip_path(clip_id, "thumb")
    if not p:
        return JSONResponse(status_code=404, content={"error": "thumbnail not found"})
    return FileResponse(p, media_type="image/jpeg")


@router.get("/clips")
async def clips_page():
    """Serve clients/clips.html per-request (same pattern as /app): no restart for HTML edits."""
    try:
        return HTMLResponse((config.CLIENTS_DIR / "clips.html").read_text())
    except FileNotFoundError:
        return JSONResponse(status_code=404, content={"error": "clips client not installed"})


@router.get("/manifest.json")
async def pwa_manifest():
    p = config.CLIENTS_DIR / "manifest.json"
    if p.is_file():
        return FileResponse(p, media_type="application/manifest+json")
    return JSONResponse(status_code=404, content={"error": "manifest not installed"})


@router.get("/sw.js")
async def pwa_sw():
    p = config.CLIENTS_DIR / "sw.js"
    if p.is_file():
        return FileResponse(p, media_type="application/javascript")
    return JSONResponse(status_code=404, content={"error": "service worker not installed"})
