"""Library: extension publications, split into page-tagged passages, searched by keyword.

The operator downloads the PDFs by hand into data/library/ (gitignored, see LIBRARY-DESIGN.md).
Nothing here fetches. Ingest is offline and repeatable: an unchanged file (same size and mtime)
is skipped, a changed one is re-indexed, a removed one is dropped. Search is SQLite FTS5 (BM25,
porter stemming) for now; vectors join only if the eval says they earn it.

    cd brain && uv run python library_store.py ingest
    cd brain && uv run python library_store.py search "squash vine borer"
    cd brain && uv run python library_store.py docs
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
from collections import Counter
from pathlib import Path

import config
import records_store as store

LIBRARY_DIR = Path(os.environ.get("HESTIA_LIBRARY_DIR") or config.DATA_DIR / "library")
CHUNK_WORDS = 220
OVERLAP_WORDS = 40
MIN_TAIL_WORDS = 60   # a page's last scrap shorter than this joins the chunk before it
PER_DOC = 2           # most passages one document may contribute to a result

_SCHEMA = """
CREATE TABLE IF NOT EXISTS library_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, file TEXT NOT NULL UNIQUE, size INTEGER, mtime REAL,
    title TEXT NOT NULL, pub TEXT, year INTEGER, pages INTEGER);
CREATE TABLE IF NOT EXISTS library_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id INTEGER NOT NULL, page INTEGER NOT NULL,
    text TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS library_fts USING fts5(
    chunk_id UNINDEXED, title, text, tokenize='porter unicode61');
"""
_ready: set[str] = set()

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
    everything from the reference list on. Page numbering is kept, empty pages and all."""
    lines = [[ln.strip() for ln in p.splitlines()] for p in pages]
    seen = Counter(ln for page in lines for ln in set(page) if ln)
    repeated = {ln for ln, n in seen.items() if len(pages) > 1 and n >= max(2, len(pages) / 2)}
    out, done = [], False
    for page in lines:
        keep = []
        for ln in page:
            if done or (ln and _REFS.match(ln)):
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


def index_doc(file: str, pages: list[str], pdf_title: str | None = None,
              size: int | None = None, mtime: float | None = None) -> int:
    """Replace one document's passages. Returns the number of passages stored."""
    pub, year = meta_of(" ".join(pages[:2]))
    title = title_of(file, pdf_title)
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


def ingest(folder: Path = LIBRARY_DIR) -> dict:
    """Index new and changed PDFs in the folder, drop ones that are gone."""
    report = {"indexed": 0, "unchanged": 0, "removed": 0, "passages": 0, "empty": []}
    files = {p.name: p for p in sorted(folder.glob("*.pdf"))}
    with _conn() as c:
        known = {r["file"]: (r["size"], r["mtime"])
                 for r in c.execute("SELECT file, size, mtime FROM library_docs")}
        for gone in set(known) - set(files):
            _drop(c, gone)
            report["removed"] += 1
    for name, path in files.items():
        st = path.stat()
        if known.get(name) == (st.st_size, st.st_mtime):
            report["unchanged"] += 1
            continue
        pages, pdf_title = _read_pdf(path)
        n = index_doc(name, pages, pdf_title, st.st_size, st.st_mtime)
        report["indexed"] += 1
        report["passages"] += n
        if n == 0:
            report["empty"].append(name)   # a scan with no text layer: needs OCR to be searchable
    return report


def fts_query(question: str) -> str:
    """Plain words, OR'd and quoted, so user text can never be read as FTS5 syntax."""
    words = [w for w in re.findall(r"[a-z0-9]+", question.lower()) if w not in _STOP and len(w) > 1]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


def search(question: str, k: int = 5) -> list[dict]:
    """Best passages for a question, at most PER_DOC from any one document."""
    q = fts_query(question)
    if not q:
        return []
    rows = _conn().execute(
        "SELECT f.chunk_id, bm25(library_fts, 0.0, 2.0, 1.0) AS score, ch.page, ch.text, "
        "d.title, d.pub, d.year, d.file FROM library_fts f "
        "JOIN library_chunks ch ON ch.id = f.chunk_id JOIN library_docs d ON d.id = ch.doc_id "
        "WHERE library_fts MATCH ? ORDER BY score LIMIT ?", (q, k * 6)).fetchall()
    out, per = [], Counter()
    for r in rows:
        if per[r["file"]] >= PER_DOC:
            continue
        per[r["file"]] += 1
        out.append({"title": r["title"], "pub": r["pub"], "year": r["year"], "page": r["page"],
                    "text": r["text"], "file": r["file"]})
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
    sub.add_parser("ingest", help=f"index new and changed PDFs in {LIBRARY_DIR}")
    s = sub.add_parser("search", help="print the best passages for a question")
    s.add_argument("question")
    s.add_argument("-k", type=int, default=5)
    sub.add_parser("docs", help="list indexed documents")
    a = ap.parse_args()
    if a.cmd == "ingest":
        print(ingest())
    elif a.cmd == "search":
        for r in search(a.question, a.k):
            print(f"\n## {r['title']} ({r['year'] or 'n.d.'}), p.{r['page']}\n{r['text'][:600]}")
    else:
        for d in docs():
            print(f"{d['passages']:3} passages  {d['pages']:2}p  {d['year'] or '----'}  "
                  f"{d['pub'] or '':8}  {d['title']}")


if __name__ == "__main__":
    main()
