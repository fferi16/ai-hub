"""local – Helyi LLM (Ollama): ingyenes kérdés-válasz, hosszú szövegek háttér-összefoglalása, képek (képi modell)

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


IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
SEE_PROMPT = "Írd le röviden, magyarul, mi látható a képen. Ha szöveg is van rajta, azt szó szerint add vissza."


def _img_b64(p: Path, max_px: int) -> str:
    import base64
    import io

    from PIL import Image

    with Image.open(p) as im:
        im.seek(0)
        img = im.convert("RGB")
    scale = min(1.0, max_px / max(img.width, img.height, 1))
    if scale < 1:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def _vl_model(model: str) -> tuple[str | None, dict | None]:
    try:
        ms = ollama.models()
    except Exception as e:  # noqa: BLE001
        return None, _down(e)
    if model:
        if model not in {m["name"] for m in ms}:
            return None, {"ok": False, "reason": f"nincs ilyen modell: {model}", "hint": "local_models"}
        return model, None
    vl = ollama.vision_model(ms)
    if not vl:
        return None, {"ok": False, "reason": "nincs képi (vision) modell telepítve", "hint": "ollama pull qwen2.5vl:7b"}
    return vl, None


@tool
def local_see(path: str, question: str = "", model: str = "", max_px: int = 1024, max_tokens: int = 400) -> dict:
    """Kép(ek) a helyi képi modellnek (ingyenes): leírás/kérdés; több kép ';'-vel (max 4, pl. összehasonlítás)."""
    paths = [Path(s.strip()).expanduser() for s in path.split(";") if s.strip()]
    if not paths:
        return {"ok": False, "reason": "nincs megadva kép"}
    if len(paths) > 4:
        return {"ok": False, "reason": "legfeljebb 4 kép adható meg egyszerre"}
    for p in paths:
        if not p.is_file():
            return {"ok": False, "reason": f"nincs ilyen fájl: {p}"}
        if p.suffix.lower() not in IMG_EXT:
            return {"ok": False, "reason": f"nem támogatott képformátum: {p.suffix}", "supported": sorted(IMG_EXT)}
    vl, err = _vl_model(model)
    if err:
        return err
    try:
        imgs = [_img_b64(p, max(256, min(int(max_px), 2048))) for p in paths]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"a kép nem olvasható: {type(e).__name__}: {e}"[:200]}
    q = question.strip() or SEE_PROMPT
    if len(paths) > 1 and question.strip():
        q = f"{len(paths)} képet kapsz a megadott sorrendben. {q}"
    try:
        r = ollama.chat(q, vl, "", max(1, min(int(max_tokens), 2000)), 0, num_ctx=4096 * len(imgs), images=imgs)
    except Exception as e:  # noqa: BLE001
        if "timeout" in type(e).__name__.lower():
            return {"ok": False, "reason": "időtúllépés – a modell valószínűleg még töltődik, próbáld újra"}
        return _down(e)
    return {"ok": True, "model": vl, "text": r["text"], "seconds": r["seconds"]}


@tool
def local_see_folder(folder: str, question: str = "", pattern: str = "*.png;*.jpg;*.jpeg;*.webp", out: str = "",
                     budget_s: int = 90, max_files: int = 200, force: bool = False) -> dict:
    """Mappa képei egyenként a helyi képi modellnek → .md fájlba; gyorsítótáras, időkeret után újrahívással folytatható."""
    import fnmatch

    d = Path(folder).expanduser()
    if not d.is_dir():
        return {"ok": False, "reason": f"nincs ilyen mappa: {folder}"}
    pats = [s.strip().lower() for s in pattern.split(";") if s.strip()]
    files = sorted(f for f in d.iterdir() if f.is_file() and f.suffix.lower() in IMG_EXT
                   and any(fnmatch.fnmatch(f.name.lower(), p) for p in pats))[:max(1, min(int(max_files), 1000))]
    if not files:
        return {"ok": False, "reason": "nincs illeszkedő kép a mappában"}
    vl, err = _vl_model("")
    if err:
        return err
    q = question.strip() or SEE_PROMPT
    target = Path(out).expanduser() if out else OUT / "see" / f"{d.name[:80]}.md"
    cache_p = target.with_suffix(".cache.json")
    try:
        cache = json.loads(cache_p.read_text("utf-8"))
    except (OSError, ValueError):
        cache = {}
    if force or cache.get("_q") != q or cache.get("_m") != vl:
        cache = {"_q": q, "_m": vl}
    t0, done, errors = time.time(), 0, []
    todo = [f for f in files if cache.get(f.name, {}).get("mt") != f.stat().st_mtime]
    for f in todo:
        if time.time() - t0 > max(10, min(int(budget_s), 110)):
            break
        try:
            r = ollama.chat(q, vl, "", 400, 0, num_ctx=4096, images=[_img_b64(f, 1024)])
            cache[f.name] = {"mt": f.stat().st_mtime, "t": r["text"]}
            done += 1
        except Exception as e:  # noqa: BLE001
            errors.append({"file": f.name, "error": f"{type(e).__name__}: {e}"[:150]})
            break  # elérhetetlen / töltődő modellnél ne pörögjön végig
        cache_p.parent.mkdir(parents=True, exist_ok=True)
        cache_p.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
    lines = [f"# {d.name} – képek ({vl})", "", f"Kérdés: {q}", ""]
    for f in files:
        if f.name in cache:
            lines += [f"## {f.name}", "", cache[f.name]["t"], ""]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), "utf-8")
    left = sum(1 for f in files if f.name not in cache or cache[f.name].get("mt") != f.stat().st_mtime)
    res = {"ok": not errors or done > 0, "model": vl, "md": str(target), "images": len(files), "new": done,
           "cached": len(files) - len(todo), "s": round(time.time() - t0, 1)}
    if errors:
        res["errors"] = errors
        if not done:
            res["reason"] = errors[0]["error"]
    if left:
        res.update(incomplete=True, left=left, hint="hívd újra ugyanígy a folytatáshoz")
    return res


@tool
def local_summarize(path: str, instructions: str = "", model: str = "", chunk_chars: int = 8000, points: int = 8,
                    out: str = "") -> dict:
    """Hosszú szöveg (txt/md/srt/docx/pdf) háttér-összefoglalása → .md, job-ot ad; media-leirathoz a media_digest jobb."""
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
