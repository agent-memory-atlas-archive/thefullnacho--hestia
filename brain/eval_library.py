"""Score library search against the operator's questions: keyword, vectors, and both.

The questions live in data/library/questions.md (gitignored, never tuned against):

    1. My zucchini wilted overnight and there's orange mush at the base of the stem
       -> Squash Vine Borer

`A or B` means either document is a right answer; `A | B` means a good answer needs both.
Titles are matched to the indexed documents loosely, so typos are fine; the mapping is
printed so a wrong match can't hide. Vectors come from EmbeddingGemma through Ollama and are
cached beside the questions.

    cd brain && uv run python eval_library.py [--k 5]
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import re
from pathlib import Path

import httpx

import library_store as lib

QUESTIONS = lib.LIBRARY_DIR / "questions.md"
CACHE = lib.LIBRARY_DIR / ".embeddings.json"
OLLAMA = "http://127.0.0.1:11434"
EMBED_MODEL = lib.EMBED_MODEL
QUERY, DOC = lib.QUERY, lib.DOC   # the same prompts production search uses
RRF_K = 60
POOL = 50     # passages each ranker contributes before fusion


def parse(text: str) -> list[dict]:
    out, q = [], None
    for line in text.splitlines():
        if m := re.match(r"\s*\d+\.\s*(.+)", line):
            q = m.group(1).strip()
        elif (m := re.match(r"\s*->\s*(.+)", line)) and q:
            raw = m.group(1).strip()
            if raw.lower() == "none":
                out.append({"q": q, "mode": "none", "want": []})
            elif " | " in raw:
                out.append({"q": q, "mode": "all", "want": [t.strip() for t in raw.split(" | ")]})
            else:
                out.append({"q": q, "mode": "any", "want": [t.strip() for t in re.split(r"\s+or\s+", raw)]})
            q = None
    return out


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s.lower()).split())


def match_title(name: str, docs: list[dict]) -> tuple[str, float]:
    """The indexed file a written title means, and how sure the match is."""
    want = _norm(name)

    def sim(candidate):
        c = _norm(candidate)
        # A short written title against a long real one: also compare with the real title's
        # opening words, so "Squash Vine Borer" isn't penalised for the subtitle it left out.
        head = " ".join(c.split()[:len(want.split())])
        return max(difflib.SequenceMatcher(None, want, c).ratio(),
                   difflib.SequenceMatcher(None, want, head).ratio())
    best = max((max(sim(d["title"]), sim(Path(d["file"]).stem)), d["file"]) for d in docs)
    return best[1], best[0]


def _embed(texts: list[str]) -> list[list[float]]:
    r = httpx.post(f"{OLLAMA}/api/embed", json={"model": EMBED_MODEL, "input": texts}, timeout=300)
    r.raise_for_status()
    return r.json()["embeddings"]


def _unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def passage_vectors() -> dict[int, list[float]]:
    rows = lib._conn().execute(
        "SELECT ch.id, ch.text, d.title FROM library_chunks ch JOIN library_docs d ON d.id = ch.doc_id"
    ).fetchall()
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    keyed = {r["id"]: (hashlib.sha1((r["title"] + r["text"]).encode()).hexdigest(),
                       DOC.format(title=r["title"]) + r["text"]) for r in rows}
    todo = [(h, t) for h, t in keyed.values() if h not in cache]
    for i in range(0, len(todo), 32):
        batch = todo[i:i + 32]
        for (h, _), vec in zip(batch, _embed([t for _, t in batch])):
            cache[h] = _unit(vec)
    CACHE.write_text(json.dumps(cache))
    return {cid: cache[h] for cid, (h, _) in keyed.items()}


def keyword_rank(q: str) -> list[int]:
    fq = lib.fts_query(q)
    if not fq:
        return []
    rows = lib._conn().execute(
        "SELECT chunk_id FROM library_fts WHERE library_fts MATCH ? "
        "ORDER BY bm25(library_fts, 0.0, 2.0, 1.0) LIMIT ?", (fq, POOL)).fetchall()
    return [int(r["chunk_id"]) for r in rows]


def vector_rank(q: str, vecs: dict[int, list[float]], qvec=None) -> tuple[list[int], float]:
    """Best passages for the question, and the top cosine similarity."""
    qv = qvec or _unit(_embed([QUERY + q])[0])
    sims = {cid: sum(a * b for a, b in zip(qv, v)) for cid, v in vecs.items()}
    scored = sorted(sims, key=lambda cid: -sims[cid])
    return scored[:POOL], sims[scored[0]] if scored else 0.0


def none_report(rows: list[dict]) -> None:
    """Can a similarity floor tell "not in the library" from a real question? A `none`
    question counts as caught when its best match scores under the floor; a real question
    is lost when its best match does. Every floor is shown so the choice stays visible."""
    real = sorted(r["top"] for r in rows if r["mode"] != "none")
    none = sorted(r["top"] for r in rows if r["mode"] == "none")
    if not none:
        print("\nNo `-> none` questions yet, so the not-in-the-library floor is untested.")
        return
    print(f"\nBest-match similarity: real questions min {real[0]:.3f} median {real[len(real)//2]:.3f}; "
          f"none questions max {none[-1]:.3f} median {none[len(none)//2]:.3f}")
    for floor in sorted({round(x, 2) for x in none + real[:10]}):
        caught = sum(x < floor for x in none)
        lost = sum(x < floor for x in real)
        print(f"  floor {floor:.2f}: none caught {caught}/{len(none)}, real questions lost {lost}/{len(real)}")


def fuse(*ranks: list[int]) -> list[int]:
    score: dict[int, float] = {}
    for rank in ranks:
        for i, cid in enumerate(rank):
            score[cid] = score.get(cid, 0) + 1 / (RRF_K + i + 1)
    return sorted(score, key=lambda c: -score[c])


def doc_order(chunk_ids: list[int]) -> list[str]:
    if not chunk_ids:
        return []
    files = dict(lib._conn().execute(
        f"SELECT ch.id, d.file FROM library_chunks ch JOIN library_docs d ON d.id = ch.doc_id "
        f"WHERE ch.id IN ({','.join('?' * len(chunk_ids))})", chunk_ids).fetchall())
    return list(dict.fromkeys(files[c] for c in chunk_ids if c in files))


def score(results: list[dict], k: int) -> dict:
    graded = [r for r in results if r["mode"] != "none"]
    def hit(r, n):
        return any(w in r["docs"][:n] for w in r["want"])
    rr = [1 / (min(r["docs"].index(w) for w in r["want"] if w in r["docs"]) + 1)
          if any(w in r["docs"] for w in r["want"]) else 0 for r in graded]
    multi = [r for r in graded if r["mode"] == "all"]
    return {"n": len(graded), "hit@1": sum(hit(r, 1) for r in graded), "hit@3": sum(hit(r, 3) for r in graded),
            f"hit@{k}": sum(hit(r, k) for r in graded), "mrr": round(sum(rr) / max(1, len(graded)), 3),
            "all_found": f"{sum(all(w in r['docs'][:k] for w in r['want']) for r in multi)}/{len(multi)}"}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--misses", action="store_true", help="list each method's misses")
    a = ap.parse_args()
    docs = lib.docs()
    qs = parse(QUESTIONS.read_text())
    seen = {}
    for q in qs:
        q["want"] = [seen.setdefault(w, match_title(w, docs)) [0] for w in q["want"]]
    print("Title matches (lowest confidence first):")
    for w, (f, conf) in sorted(seen.items(), key=lambda x: x[1][1])[:12]:
        print(f"  {conf:.2f}  {w!r} -> {f}")
    vecs = passage_vectors()
    qvecs = [_unit(v) for v in _embed([QUERY + q["q"] for q in qs])]
    methods = {"keyword": [], "vector": [], "hybrid": []}
    for q, qv in zip(qs, qvecs):
        kw, (vr, top) = keyword_rank(q["q"]), vector_rank(q["q"], vecs, qv)
        for name, rank in (("keyword", kw), ("vector", vr), ("hybrid", fuse(kw, vr))):
            methods[name].append({**q, "docs": doc_order(rank), "top": top})
    print(f"\n{len(qs)} questions")
    for name, rows in methods.items():
        print(f"{name:8} {score(rows, a.k)}")
        if a.misses:
            for r in rows:
                if r["mode"] != "none" and not any(w in r["docs"][:a.k] for w in r["want"]):
                    print(f"    miss: {r['q'][:70]!r} wanted {r['want'][0][:40]} got {r['docs'][:1]}")
    none_report(methods["vector"])


if __name__ == "__main__":
    main()
