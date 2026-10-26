#!/usr/bin/env python3
"""Coding benchmark: replay real hestia commits as tasks for any OpenAI-compatible model.

Per commit: a scratch worktree at the parent, the commit's own tests copied in. The model
gets the commit message, the test diff, and the full source files the commit touched, and
answers in SEARCH/REPLACE blocks. We apply them, run the commit's tests, then the whole
suite, because passing the targeted tests is not the bar: one model change passed its own
tests and broke an unrelated caller (2026-10-25, see RESULTS.md). Temperature 0, one pass.

Usage:
  BENCH_BASE=http://127.0.0.1:8080 BENCH_MODEL=name BENCH_KEY=... python benchmarks/codebench.py
  python benchmarks/codebench.py 44ffd37 8a01e84     # specific commits

Uses brain/.venv for pytest. Answers and diffs land in benchmarks/results/ (gitignored).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "brain/.venv/bin/python"
BASE = os.environ.get("BENCH_BASE", "http://127.0.0.1:8080").rstrip("/").removesuffix("/v1")
MODEL = os.environ.get("BENCH_MODEL", "local")
KEY = os.environ.get("BENCH_KEY", "not-needed")
MAX_TOKENS = int(os.environ.get("BENCH_MAX_TOKENS", "12000"))

# Small, recent fixes with tests: one or two source files, under ~80 changed lines.
DEFAULT_COMMITS = ["44ffd37", "f6865fb", "8061f6a", "2aca3e0",
                   "8a01e84", "eb3c1b7", "7ff4677", "fccacdb"]

SYSTEM = """You are a senior Python engineer making a focused change to an existing codebase.
Reply with edits only, as one or more SEARCH/REPLACE blocks, exactly this format:

path/to/file.py
<<<<<<< SEARCH
exact lines copied from the current file
=======
the replacement lines
>>>>>>> REPLACE

The SEARCH text must match the file exactly, including indentation, and be unique.
Edit only the source files given. Do not edit tests. No explanation outside the blocks."""

BLOCK = re.compile(r"^(\S+\.py)\s*\n(?:```\w*\n)?<<<<<<< SEARCH\n(.*?)\n?=======\n(.*?)\n?>>>>>>> REPLACE",
                   re.S | re.M)


def sh(*args, cwd=REPO, check=True):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=check).stdout


def pytest(wt, tests=()):
    r = subprocess.run([str(PY), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        *[t.removeprefix("brain/") for t in tests]],
                       cwd=wt / "brain", capture_output=True, text=True, timeout=900)
    failed = [l.split()[1] for l in r.stdout.splitlines() if l.startswith("FAILED ")]
    return r.returncode == 0, failed


def chat(prompt):
    body = json.dumps({"model": MODEL, "temperature": 0, "max_tokens": MAX_TOKENS,
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(f"{BASE}/v1/chat/completions", data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=1800) as r:
        data = json.load(r)
    return data, time.time() - t0


def apply(wt, text, allowed):
    blocks, errors = BLOCK.findall(text), []
    for path, search, replace in blocks:
        path = path.strip("`*")
        if path not in allowed:
            errors.append(f"edit outside allowed files: {path}")
            continue
        f = wt / path
        src = f.read_text()
        n = src.count(search)
        if n != 1:
            errors.append(f"{path}: search matched {n}x")
            continue
        f.write_text(src.replace(search, replace, 1))
    return len(blocks), errors


def run(c, out):
    files = sh("git", "show", "--name-only", "--format=", c).split()
    tests = [f for f in files if f.startswith("brain/tests/")]
    src = [f for f in files if f.endswith(".py") and f not in tests]
    wt = out / f"wt_{c}"
    sh("git", "worktree", "add", "--detach", str(wt), f"{c}~1")
    try:
        # All tests as of the commit, so the full-suite gate checks against its intent.
        sh("git", "checkout", c, "--", "brain/tests", cwd=wt)
        before, _ = pytest(wt, tests)
        msg = sh("git", "log", "-1", "--format=%B", c).split("Co-Authored-By")[0].strip()
        test_diff = sh("git", "show", "--format=", c, "--", *tests)
        sources = "\n\n".join(f"### {p}\n```python\n{(wt / p).read_text()}```" for p in src)
        prompt = (f"## Task (the commit message for this change)\n{msg}\n\n"
                  f"## Tests that must pass (already added to the repo)\n```diff\n{test_diff}```\n\n"
                  f"## Source files you may edit\n{sources}")
        data, secs = chat(prompt)
        choice = data["choices"][0]
        answer = choice["message"].get("content") or ""
        nblocks, errors = apply(wt, answer, set(src))
        targeted, _ = pytest(wt, tests)
        full, failed = pytest(wt)
        (out / f"{c}.answer.md").write_text(answer)
        (out / f"{c}.diff").write_text(sh("git", "diff", cwd=wt))
        return {"commit": c, "subject": msg.splitlines()[0], "fails_before": not before,
                "targeted": targeted, "full_suite": full, "regressions": failed,
                "blocks": nblocks, "apply_errors": errors, "secs": round(secs, 1),
                "usage": data.get("usage"), "finish": choice.get("finish_reason")}
    finally:
        sh("git", "worktree", "remove", "--force", str(wt), check=False)


def main():
    commits = sys.argv[1:] or DEFAULT_COMMITS
    out = REPO / "benchmarks/results" / f"codebench-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True)
    print(f"model={MODEL} base={BASE} -> {out}")
    passed = clean = 0
    for c in commits:
        try:
            rec = run(c, out)
        except Exception as e:  # noqa: BLE001
            rec = {"commit": c, "error": repr(e)[:300]}
        passed += bool(rec.get("targeted"))
        clean += bool(rec.get("full_suite"))
        toks = (rec.get("usage") or {}).get("completion_tokens", "?")
        verdict = "CLEAN" if rec.get("full_suite") else "TARGETED" if rec.get("targeted") else "FAIL"
        print(f"  {c} {verdict:<8} {rec.get('secs', '?')}s {toks} tok  "
              f"{rec.get('subject', rec.get('error', ''))[:70]}", flush=True)
        if rec.get("regressions") or rec.get("apply_errors"):
            print(f"           {rec.get('regressions') or ''} {rec.get('apply_errors') or ''}")
        with open(out / "results.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
    print(f"  targeted {passed}/{len(commits)}   full suite clean {clean}/{len(commits)}")


if __name__ == "__main__":
    main()
