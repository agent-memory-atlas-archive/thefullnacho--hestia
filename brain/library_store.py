"""Library: extension publications, split into page-tagged passages, searched by keyword.

The operator downloads the PDFs by hand into data/library/ (gitignored, see LIBRARY-DESIGN.md).
Nothing here fetches. Ingest is offline and repeatable: an unchanged file (same size and mtime)
is skipped, a changed one is re-indexed, a removed one is dropped. Search is SQLite FTS5 (BM25,
porter stemming) is kept as the fallback; the brain searches by meaning with EmbeddingGemma
vectors, which beat keyword on the operator's 128 questions (88% vs 70% right document first;
LIBRARY-DESIGN.md). Vectors are made at ingest through the local Ollama and stored beside the
passages, so a question costs one small embedding call and an in-memory cosine pass.

    cd brain && uv run python library_store.py ingest
    cd brain && uv run python library_store.py search "squash vine borer"
    cd brain && uv run python library_store.py docs
"""
from __future__ import annotations

import argparse
import array
import math
import os
import re
import subprocess
import tomllib
from collections import Counter
from pathlib import Path

import httpx

import config
import records_store as store

LIBRARY_DIR = Path(os.environ.get("HESTIA_LIBRARY_DIR") or config.DATA_DIR / "library")
# Per-file corrections, kept beside the PDFs (gitignored; format in deploy/library.example.toml):
# a title and year when the PDF's own are missing or wrong, and skip = true for a file whose
# text layer is unreadable.
CHUNK_WORDS = 220
OVERLAP_WORDS = 40
MIN_TAIL_WORDS = 60   # a page's last scrap shorter than this joins the chunk before it
PER_DOC = 2           # most passages one document may contribute to a result
REFS_FROM = 0.6       # a reference-list heading only ends the text in the last 40% of pages
OLLAMA = os.environ.get("HESTIA_OLLAMA", "http://127.0.0.1:11434").rstrip("/")
EMBED_MODEL = os.environ.get("HESTIA_EMBED_MODEL", "embeddinggemma")
# EmbeddingGemma's retrieval prompts; the router bench found its task prefixes are required.
QUERY = "task: search result | query: "
DOC = "title: {title} | text: "

_SCHEMA = """
CREATE TABLE IF NOT EXISTS library_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, file TEXT NOT NULL UNIQUE, size INTEGER, mtime REAL,
    title TEXT NOT NULL, pub TEXT, year INTEGER, pages INTEGER);
CREATE TABLE IF NOT EXISTS library_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id INTEGER NOT NULL, page INTEGER NOT NULL,
    text TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS library_fts USING fts5(
    chunk_id UNINDEXED, title, text, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS library_vecs (chunk_id INTEGER PRIMARY KEY, vec BLOB NOT NULL);
"""
_ready: set[str] = set()
_vectors: dict = {}   # (db path, passage count) -> {chunk_id: unit vector}

# Browser print furniture that changes per page, so the repeated-line pass can't catch it.
_STAMP = re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4},? \d{1,2}:\d{2}( ?[AP]M)?$|^\d+ of \d+$|^https?://\S+$")
# Everything from a reference list on is citations, which match keywords without answering.
_REFS = re.compile(r"^(references|literature cited|works cited|sources|bibliography|resources)\s*:?$", re.I)
_PUB = re.compile(r"Publication\s*#?\s*([A-Z]{2,}[\s-]?\d+)")
_MONTH_YEAR = re.compile(r"\b(?:January|February|March|April|May|June|July|August|September|"
                         r"October|November|December)\s+((?:19|20)\d{2})\b")
_COPYRIGHT = re.compile(r"(?:©|\(c\)|Copyright)\s*((?:19|20)\d{2})", re.I)
# PDF Title fields that name the authoring tool or file instead of the document.
_JUNK_TITLE = re.compile(r"\.(pdf|docx?|pptx?)$|^(powerpoint presentation|microsoft (word|powerpoint)\b)", re.I)
_STOP = set("a an and are as at be but by can do does for from how i if in into is it its my "
            "of on or so that the their there these this to was what when where which who why "
            "will with you your should would could me we our".split())


def _conn():
    c = store._conn()
    key = str(store.DB_PATH)
    if key not in _ready:
        c.executescript(_SCHEMA)
        _ready.add(key)
    return c


def clean_pages(pages: list[str]) -> list[str]:
    """Drop running headers/footers (a line on at least half the pages), print stamps, and
    everything from the reference list on. Page numbering is kept, empty pages and all.

    A reference list only counts in the back of the document: a "Resources" heading in a table
    of contents or an early section once cut a 102-page guide down to 3 passages."""
    lines = [[ln.strip() for ln in p.splitlines()] for p in pages]
    seen = Counter(ln for page in lines for ln in set(page) if ln)
    repeated = {ln for ln, n in seen.items() if len(pages) > 1 and n >= max(2, len(pages) / 2)}
    back = int(len(pages) * REFS_FROM) if len(pages) > 2 else 0
    out, done = [], False
    for n, page in enumerate(lines):
        keep = []
        for ln in page:
            if done or (n >= back and ln and _REFS.match(ln)):
                done = True
                continue
            if ln in repeated or _STAMP.match(ln):
                continue
            keep.append(ln)
        out.append(" ".join(" ".join(keep).split()))
    return out


def chunks(pages: list[str]) -> list[tuple[int, str]]:
    """(page, text) windows of CHUNK_WORDS with overlap, never crossing a page, so every
    passage cites one page."""
    out = []
    for n, text in enumerate(pages, 1):
        words = text.split()
        if not words:
            continue
        step = CHUNK_WORDS - OVERLAP_WORDS
        start, page_chunks = 0, []
        while start < len(words):
            page_chunks.append(words[start:start + CHUNK_WORDS])
            if start + CHUNK_WORDS >= len(words):
                break
            start += step
        if len(page_chunks) > 1 and len(page_chunks[-1]) < MIN_TAIL_WORDS:
            tail = page_chunks.pop()
            page_chunks[-1] = page_chunks[-1] + tail[OVERLAP_WORDS:]
        out += [(n, " ".join(c)) for c in page_chunks]
    return out


def title_of(file: str, pdf_title: str | None) -> str:
    """The PDF's own title when it has a real one (a saved web page's is 'Title | Site | ...'),
    otherwise the file name, de-slugged."""
    t = (pdf_title or "").split(" | ")[0].strip()
    if t and " " in t and not _JUNK_TITLE.search(t):
        return t.title() if t.isupper() else t
    t = Path(file).stem.split(" _ ")[0].split(" - ")[0]
    t = re.sub(r"pdf$", "", t, flags=re.I)
    t = " ".join(t.replace("-", " ").replace("_", " ").split())
    return t.title() if t.isupper() else t


def meta_of(first_pages: str) -> tuple[str | None, int | None]:
    """Publication number and year from the opening text, when the document states them."""
    pub = _PUB.search(first_pages)
    year = _MONTH_YEAR.search(first_pages) or _COPYRIGHT.search(first_pages)
    return (pub.group(1).replace(" ", "") if pub else None, int(year.group(1)) if year else None)


def manifest(folder: Path | None = None) -> dict:
    path = (folder or LIBRARY_DIR) / "library.toml"
    return tomllib.loads(path.read_text()) if path.exists() else {}


def index_doc(file: str, pages: list[str], pdf_title: str | None = None,
              size: int | None = None, mtime: float | None = None, fix: dict | None = None) -> int:
    """Replace one document's passages. Returns the number of passages stored."""
    fix = fix or {}
    pub, year = meta_of(" ".join(pages[:2]))
    title = fix.get("title") or title_of(file, pdf_title)
    year = fix.get("year", year)
    parts = chunks(clean_pages(pages))
    with _conn() as c:
        _drop(c, file)
        doc_id = c.execute(
            "INSERT INTO library_docs (file, size, mtime, title, pub, year, pages) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (file, size, mtime, title, pub, year, len(pages))).lastrowid
        for page, text in parts:
            cid = c.execute("INSERT INTO library_chunks (doc_id, page, text) VALUES (?, ?, ?)",
                            (doc_id, page, text)).lastrowid
            c.execute("INSERT INTO library_fts (chunk_id, title, text) VALUES (?, ?, ?)",
                      (cid, title, text))
    return len(parts)


def _drop(c, file: str) -> None:
    row = c.execute("SELECT id FROM library_docs WHERE file = ?", (file,)).fetchone()
    if not row:
        return
    c.execute("DELETE FROM library_fts WHERE chunk_id IN "
              "(SELECT id FROM library_chunks WHERE doc_id = ?)", (row["id"],))
    c.execute("DELETE FROM library_vecs WHERE chunk_id IN "
              "(SELECT id FROM library_chunks WHERE doc_id = ?)", (row["id"],))
    c.execute("DELETE FROM library_chunks WHERE doc_id = ?", (row["id"],))
    c.execute("DELETE FROM library_docs WHERE id = ?", (row["id"],))


def _read_pdf(path: Path) -> tuple[list[str], str | None]:
    """Page texts (pdftotext separates pages with form feeds) and the PDF's Title field."""
    text = subprocess.run(["pdftotext", "-q", "-enc", "UTF-8", str(path), "-"],
                          capture_output=True, text=True, timeout=120).stdout
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    info = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=30).stdout
    title = next((ln.split(":", 1)[1].strip() for ln in info.splitlines()
                  if ln.startswith("Title:")), None)
    return pages, title


def embed(texts: list[str]) -> list[list[float]]:
    """Unit vectors from the local Ollama. Raises when the embedder is unreachable."""
    r = httpx.post(f"{OLLAMA}/api/embed", json={"model": EMBED_MODEL, "input": texts}, timeout=120)
    r.raise_for_status()
    out = []
    for v in r.json()["embeddings"]:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


def embed_missing(batch: int = 32) -> int:
    """Embed every passage that has no vector yet. Returns how many were embedded."""
    rows = _conn().execute(
        "SELECT ch.id, ch.text, d.title FROM library_chunks ch JOIN library_docs d ON d.id = ch.doc_id "
        "WHERE ch.id NOT IN (SELECT chunk_id FROM library_vecs)").fetchall()
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        vecs = embed([DOC.format(title=r["title"]) + r["text"] for r in part])
        with _conn() as c:
            c.executemany("INSERT OR REPLACE INTO library_vecs (chunk_id, vec) VALUES (?, ?)",
                          [(r["id"], array.array("f", v).tobytes()) for r, v in zip(part, vecs)])
    return len(rows)


def _load_vectors() -> dict[int, list[float]]:
    """All passage vectors, cached per process until the passage count changes. Empty when
    any passage lacks a vector: a partial index would silently hide documents."""
    c = _conn()
    chunks = c.execute("SELECT COUNT(*) FROM library_chunks").fetchone()[0]
    key = (str(store.DB_PATH), chunks)
    if key not in _vectors:
        rows = c.execute("SELECT chunk_id, vec FROM library_vecs").fetchall()
        _vectors.clear()
        _vectors[key] = ({r["chunk_id"]: array.array("f", r["vec"]).tolist() for r in rows}
                         if rows and len(rows) == chunks else {})
    return _vectors[key]


def ingest(folder: Path = LIBRARY_DIR, force: bool = False) -> dict:
    """Index new and changed PDFs in the folder, drop ones that are gone."""
    report = {"indexed": 0, "unchanged": 0, "removed": 0, "passages": 0, "empty": [], "skipped": []}
    fixes = manifest(folder)
    files = {p.name: p for p in sorted(folder.glob("*.pdf"))}
    for name, fix in fixes.items():
        if fix.get("skip") and name in files:
            del files[name]
            report["skipped"].append(name)
    with _conn() as c:
        known = {r["file"]: (r["size"], r["mtime"])
                 for r in c.execute("SELECT file, size, mtime FROM library_docs")}
        for gone in set(known) - set(files):
            _drop(c, gone)
            report["removed"] += 1
    for name, path in files.items():
        st = path.stat()
        if not force and known.get(name) == (st.st_size, st.st_mtime):
            report["unchanged"] += 1
            continue
        pages, pdf_title = _read_pdf(path)
        n = index_doc(name, pages, pdf_title, st.st_size, st.st_mtime, fixes.get(name))
        report["indexed"] += 1
        report["passages"] += n
        if n == 0:
            report["empty"].append(name)   # a scan with no text layer: needs OCR to be searchable
    try:
        report["embedded"] = embed_missing()
    except Exception as e:  # noqa: BLE001 — keyword search still works; the next ingest retries
        report["embed_error"] = f"{type(e).__name__}: {e}"
    return report


def fts_query(question: str) -> str:
    """Plain words, OR'd and quoted, so user text can never be read as FTS5 syntax."""
    words = [w for w in re.findall(r"[a-z0-9]+", question.lower()) if w not in _STOP and len(w) > 1]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


def search(question: str, k: int = 5) -> list[dict]:
    """Best passages for a question by meaning, at most PER_DOC from any one document. Falls
    back to keyword search when there are no vectors or the embedder is down; every result
    says which (`mode`), and vector results carry their cosine similarity (`score`)."""
    vecs = _load_vectors()
    if vecs:
        try:
            qv = embed([QUERY + question])[0]
        except Exception:  # noqa: BLE001 — an Ollama restart must not cost the answer
            qv = None
        if qv:
            sims = {cid: sum(a * b for a, b in zip(qv, v)) for cid, v in vecs.items()}
            ranked = sorted(sims, key=lambda cid: -sims[cid])[:k * 6]
            return _passages(ranked, k, "vector", sims)
    return keyword_search(question, k)


def keyword_search(question: str, k: int = 5) -> list[dict]:
    q = fts_query(question)
    if not q:
        return []
    rows = _conn().execute(
        "SELECT f.chunk_id FROM library_fts f WHERE library_fts MATCH ? "
        "ORDER BY bm25(library_fts, 0.0, 2.0, 1.0) LIMIT ?", (q, k * 6)).fetchall()
    return _passages([int(r["chunk_id"]) for r in rows], k, "keyword", {})


def _passages(ranked: list[int], k: int, mode: str, sims: dict) -> list[dict]:
    if not ranked:
        return []
    found = {r["id"]: r for r in _conn().execute(
        "SELECT ch.id, ch.page, ch.text, d.title, d.pub, d.year, d.file FROM library_chunks ch "
        f"JOIN library_docs d ON d.id = ch.doc_id WHERE ch.id IN ({','.join('?' * len(ranked))})",
        ranked).fetchall()}
    rows = [found[c] for c in ranked if c in found]
    out, per = [], Counter()
    for r in rows:
        if per[r["file"]] >= PER_DOC:
            continue
        per[r["file"]] += 1
        out.append({"title": r["title"], "pub": r["pub"], "year": r["year"], "page": r["page"],
                    "text": r["text"], "file": r["file"], "mode": mode,
                    "score": round(sims[r["id"]], 4) if r["id"] in sims else None})
        if len(out) == k:
            break
    return out


def docs() -> list[dict]:
    rows = _conn().execute(
        "SELECT d.title, d.pub, d.year, d.pages, d.file, COUNT(ch.id) AS passages "
        "FROM library_docs d LEFT JOIN library_chunks ch ON ch.doc_id = d.id "
        "GROUP BY d.id ORDER BY d.title").fetchall()
    return [dict(r) for r in rows]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest", help=f"index new and changed PDFs in {LIBRARY_DIR}")
    i.add_argument("--all", action="store_true", help="re-index every PDF, changed or not")
    s = sub.add_parser("search", help="print the best passages for a question")
    s.add_argument("question")
    s.add_argument("-k", type=int, default=5)
    sub.add_parser("docs", help="list indexed documents")
    a = ap.parse_args()
    if a.cmd == "ingest":
        print(ingest(force=a.all))
    elif a.cmd == "search":
        for r in search(a.question, a.k):
            print(f"\n## {r['title']} ({r['year'] or 'n.d.'}), p.{r['page']}\n{r['text'][:600]}")
    else:
        for d in docs():
            print(f"{d['passages']:3} passages  {d['pages']:2}p  {d['year'] or '----'}  "
                  f"{d['pub'] or '':8}  {d['title']}")


if __name__ == "__main__":
    main()
