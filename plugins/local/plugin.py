"""local – Helyi LLM (Ollama): ingyenes kérdés-válasz, hosszú szövegek háttér-összefoglalása

Ingyenes előszűrésre jó (vázlat, összefoglaló, fordítás, osztályozás), mielőtt Claude dolgozna vele.
Az első hívás betölti a modellt (~1 perc), utána gyors; a modell 15 percig betöltve marad.
"""
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from hubkit import secret, tool  # noqa: F401

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import local_ollama as ollama  # noqa: E402

OUT = HERE.parents[1] / "out" / "local"
JOBS = OUT / "jobs"
VRAM_GB = 6.0
TEXT_EXT = {".txt", ".md", ".srt", ".csv", ".json", ".docx", ".pdf"}


def _down(e: Exception) -> dict:
    return {"ok": False, "reason": f"az Ollama nem érhető el ({type(e).__name__}) – fut az Ollama alkalmazás?"}


@tool
def local_models() -> dict:
    """Telepített helyi modellek mérettel; fits_gpu = belefér-e a 6 GB-os GPU-ba."""
    try:
        ms = ollama.models()
    except Exception as e:  # noqa: BLE001
        return _down(e)
    return {"ok": True, "default": ollama.DEFAULT_MODEL,
            "models": [{"name": m["name"], "gb": round(m["size"] / 1e9, 1), "fits_gpu": m["size"] / 1e9 < VRAM_GB}
                       for m in sorted(ms, key=lambda m: m["size"])]}


@tool
def local_ask(prompt: str, model: str = "", system: str = "", max_tokens: int = 800, temperature: float = 0.3) -> dict:
    """Kérdés a helyi modellnek (ingyenes, a gépen fut); rövid feladatokra – hosszú szöveghez local_summarize."""
    if not prompt.strip():
        return {"ok": False, "reason": "üres prompt"}
    if len(prompt) > 30000:
        return {"ok": False, "reason": "túl hosszú prompt (>30 000 karakter) – használd a local_summarize-t"}
    try:
        r = ollama.chat(prompt, model or ollama.DEFAULT_MODEL, system, max(1, min(int(max_tokens), 4000)), temperature)
    except LookupError as e:
        return {"ok": False, "reason": str(e), "hint": "local_models"}
    except Exception as e:  # noqa: BLE001
        return _down(e)
    return {"ok": True, "model": model or ollama.DEFAULT_MODEL, **r}


@tool
def local_summarize(path: str, instructions: str = "", model: str = "", chunk_chars: int = 8000, points: int = 8,
                    out: str = "") -> dict:
    """Hosszú szöveg (txt/md/srt/docx/pdf, pl. media-leirat) összefoglalása háttérben szakaszonként + áttekintés → .md; job-ot ad."""
    p = Path(path).expanduser()
    if not p.is_file():
        return {"ok": False, "reason": f"nincs ilyen fájl: {path}"}
    if p.suffix.lower() not in TEXT_EXT:
        return {"ok": False, "reason": f"nem támogatott formátum: {p.suffix}"}
    try:
        names = {m["name"] for m in ollama.models()}
    except Exception as e:  # noqa: BLE001
        return _down(e)
    model = model or ollama.DEFAULT_MODEL
    if model not in names:
        return {"ok": False, "reason": f"nincs ilyen modell: {model}", "hint": "local_models"}
    job = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    jdir = JOBS / job
    jdir.mkdir(parents=True, exist_ok=True)
    target = Path(out) if out else OUT / f"{p.stem[:80]}_osszefoglalo.md"
    spec = {"job": job, "src": str(p.resolve()), "out": str(target),
            "params": {"model": model, "instructions": instructions, "chunk_chars": max(2000, min(int(chunk_chars), 20000)),
                       "points": max(3, min(int(points), 20))}}
    (jdir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), "utf-8")
    (jdir / "status.json").write_text(json.dumps({"state": "queued"}), "utf-8")
    with open(jdir / "worker.log", "w", encoding="utf-8") as log:
        subprocess.Popen([sys.executable, str(HERE / "summarize.py"), str(jdir)], stdout=log, stderr=subprocess.STDOUT,
                         creationflags=0x08000000, cwd=str(jdir))
    return {"ok": True, "job": job, "out": str(target)}


@tool
def local_job(job: str = "last", wait_s: int = 0) -> dict:
    """Összefoglaló-job állapota (progress 0–1, eredmény .md útvonala); job='last' a legutóbbi; wait_s ≤ 100."""
    if job == "last":
        jobs = sorted(JOBS.glob("*")) if JOBS.exists() else []
        if not jobs:
            return {"ok": False, "reason": "nincs még job"}
        job = jobs[-1].name
    jdir = JOBS / job
    if not re.fullmatch(r"[\w-]+", job) or not (jdir / "status.json").exists():
        return {"ok": False, "reason": f"nincs ilyen job: {job}"}
    deadline = time.time() + max(0, min(int(wait_s), 100))
    while True:
        try:
            st = json.loads((jdir / "status.json").read_text("utf-8"))
        except (OSError, ValueError):
            st = {"state": "running"}
        if st.get("state") in ("done", "error") or time.time() >= deadline:
            break
        time.sleep(1)
    if st.get("state") == "error":
        return {"ok": False, "job": job, "reason": st.get("error", "hiba"),
                "log": (jdir / "worker.log").read_text("utf-8", errors="replace")[-400:]}
    return {"ok": True, "job": job, **st}
