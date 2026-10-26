"""Vision species ID: a resident VLM vs the forager_ml EfficientNet experts, closed set.

Per expert: N images per class (seeded), Gemma sees the image plus the expert's class
list and must answer with one class key (Ollama format enum). Scores top-1 and the
toxic-as-edible false accept rate (DEADLY/CAUTION truth predicted as a SAFE class).

Usage:
  python benchmarks/vision_id.py 10              # run, 10 images per class
  python benchmarks/vision_id.py --score FILE    # score a results file

Needs the forager_ml checkout (FORAGER_ML, default ~/Documents/Forager/forager_ml).
Results land in benchmarks/results/ (gitignored).
"""
import base64, json, os, random, sys, time, urllib.request
from collections import defaultdict
from pathlib import Path

FML = Path(os.environ.get("FORAGER_ML", Path.home() / "Documents/Forager/forager_ml"))
sys.path.insert(0, str(FML / "inference/pipeline"))
from safety import SPECIES_METADATA  # noqa: E402

OLLAMA = os.environ.get("HESTIA_OLLAMA", "http://127.0.0.1:11434")
MODEL = os.environ.get("VISION_MODEL", "gemma4:12b")
EXPERTS = {"berry_expert": "berry", "highvalue_expert": "high_value",
           "medicinals_expert": "medicinals", "psychedelics_expert": "psychedelics"}
CATCHALL = {"high_value_toxics": "a toxic mushroom or plant that is not one of the others",
            "other_mushroom": "some other mushroom, none of the listed species"}


def label(c):
    m = SPECIES_METADATA.get(c)
    if c in CATCHALL:
        return f"{c}: {CATCHALL[c]}"
    return f"{c}: {c.replace('_', ' ')}" + (f" ({m['scientific']})" if m else "")


def ask(img, classes):
    prompt = ("Identify the species in this photo. Choose exactly one class from this list "
              "and answer with its key only.\n" + "\n".join(f"- {label(c)}" for c in classes))
    body = {"model": MODEL, "stream": False, "think": False,
            "format": {"type": "object", "properties": {"species": {"type": "string", "enum": classes}},
                       "required": ["species"]},
            "options": {"temperature": 0, "num_ctx": 32768, "num_predict": 64},
            "messages": [{"role": "user", "content": prompt,
                          "images": [base64.b64encode(img.read_bytes()).decode()]}]}
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        content = json.load(r)["message"]["content"]
    try:
        return json.loads(content)["species"]
    except (ValueError, KeyError):
        return "?"


def main(n, out):
    OUT = out
    done = set()
    if OUT.exists():
        done = {json.loads(l)["img"] for l in OUT.open()}
    rng = random.Random(0)
    for expert, ds in EXPERTS.items():
        classes = sorted(p.name for p in (FML / f"{ds}_dataset_split/val").iterdir() if p.is_dir())
        for c in classes:
            imgs = sorted((FML / f"{ds}_dataset_split/val" / c).iterdir())
            for img in rng.sample(imgs, min(n, len(imgs))):
                if str(img) in done:
                    continue
                t0 = time.time()
                try:
                    pred = ask(img, classes)
                except Exception as e:  # noqa: BLE001
                    pred = f"ERR:{str(e)[:60]}"
                rec = {"expert": expert, "truth": c, "pred": pred, "img": str(img),
                       "secs": round(time.time() - t0, 2)}
                with OUT.open("a") as f:
                    f.write(json.dumps(rec) + "\n")
        print(expert, "done", flush=True)


def score(path):
    rows = [json.loads(l) for l in open(path)]
    safety = lambda c: SPECIES_METADATA.get(c, {}).get("safety", "UNKNOWN")
    by = defaultdict(list)
    for r in rows:
        by[r["expert"]].append(r)

    print(f"{'expert':<22}{'n':>5}{'vlm macro':>13}{'effnet macro':>14}{'vlm tox→safe':>16}")
    for expert, rs in by.items():
        bench = json.load(open(FML / f"runs/efficientnet/{expert}/benchmark.json"))["per_class"]
        cls = defaultdict(list)
        for r in rs:
            cls[r["truth"]].append(r["pred"] == r["truth"])
        gm = sum(sum(v) / len(v) for v in cls.values()) / len(cls)
        em = sum(bench[c]["recall"] for c in cls) / len(cls)
        tox = [r for r in rs if safety(r["truth"]) in ("DEADLY", "CAUTION")]
        fa = [r for r in tox if safety(r["pred"]) == "SAFE"]
        print(f"{expert:<22}{len(rs):>5}{gm:>12.1%}{em:>14.1%}{len(fa):>9}/{len(tox)} ({len(fa)/max(1,len(tox)):.0%})")
        worst = sorted(cls.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))[:4]
        print("   weakest: " + ", ".join(f"{c} {sum(v)}/{len(v)} (effnet {bench[c]['recall']:.0%})" for c, v in worst))
        for r in fa:
            print(f"   ⚠ {r['truth']} called {r['pred']}")
    print(f"mean secs/img {sum(r['secs'] for r in rows)/len(rows):.2f}   errors {sum(r['pred'].startswith('ERR') for r in rows)}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--score"]:
        score(sys.argv[2])
    else:
        n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
        out = Path(__file__).parent / "results" / f"vision-{MODEL.replace(':', '_')}-n{n}.jsonl"
        out.parent.mkdir(exist_ok=True)
        main(n, out)
        score(out)
