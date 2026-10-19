"""A folyamatjelző tesztjei: python tests\\test_progress.py  (kilépési kód 0 = minden átment)

- hubkit.progress: fájlírás, a kezdési idő megmarad, a százalék korlátozása, kulcs-tisztítás
- widget.py --test: elindul a mintaadatokkal, és hiba nélkül bezárul
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hubkit  # noqa: E402

fails = []


def check(name, cond):
    print(("OK   " if cond else "HIBA ") + name)
    if not cond:
        fails.append(name)


with tempfile.TemporaryDirectory() as td:
    hubkit.PROGRESS_DIR = Path(td)
    hubkit.progress("teszt/1 x", "Próba", 40, "2/5", widget=False)
    f = Path(td) / "teszt_1_x.json"
    check("fájl létrejön, a kulcs tisztítva", f.exists())
    r1 = json.loads(f.read_text("utf-8"))
    check("mezők", r1["title"] == "Próba" and r1["pct"] == 40 and r1["state"] == "running" and r1["detail"] == "2/5")
    time.sleep(0.05)
    hubkit.progress("teszt/1 x", "Próba", 150, state="done", widget=False)
    r2 = json.loads(f.read_text("utf-8"))
    check("a kezdési idő megmarad", r2["started"] == r1["started"] and r2["updated"] > r1["updated"])
    check("pct 100-ra korlátozva", r2["pct"] == 100)
    hubkit.progress("nincs-szazalek", "Mozgó csík", None, widget=False)
    check("pct=None megengedett", json.loads((Path(td) / "nincs-szazalek.json").read_text("utf-8"))["pct"] is None)
    check("nincs ott maradt .tmp", not list(Path(td).glob("*.tmp")))

    # widget füstteszt mintaadatokkal (futó, mozgó csíkos, kész, hibás, rejtett, lejárt)
    now = time.time()
    samples = {
        "a": {"title": "Leirat: hosszú videó", "pct": 72, "detail": "leirat · 02:21:40", "state": "running", "started": now - 1200, "updated": now},
        "b": {"title": "Eszköz: look_run", "pct": None, "detail": "fut", "state": "running", "started": now - 12, "updated": now, "show_after_s": 3},
        "c": {"title": "Képkockák: bemutató", "pct": 100, "detail": "31 kép", "state": "done", "started": now - 300, "updated": now - 5},
        "d": {"title": "Eszköz: hub_test", "pct": None, "detail": "hiba", "state": "error", "started": now - 40, "updated": now - 2},
        "e": {"title": "rövid hívás (rejtett)", "pct": None, "state": "running", "started": now - 1, "updated": now, "show_after_s": 3},
        "f": {"title": "régi kész (rejtett)", "pct": 100, "state": "done", "started": now - 4000, "updated": now - 3000, "keep_s": 600},
    }
    for k, v in samples.items():
        (Path(td) / f"{k}.json").write_text(json.dumps(v, ensure_ascii=False), "utf-8")
    p = subprocess.run([sys.executable, str(ROOT / "widget.py"), "--test", td], capture_output=True, text=True, timeout=30)
    check("widget fut és hiba nélkül bezárul", p.returncode == 0 and not p.stderr.strip())
    if p.stderr.strip():
        print(p.stderr[-800:])

print(f"\n{'MINDEN ÁTMENT' if not fails else f'{len(fails)} HIBA'}")
sys.exit(1 if fails else 0)
