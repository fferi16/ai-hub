"""media – Videó/hang feldolgozás: leirat (faster-whisper, GPU), kulcsképkockák kinyerése tanulási jegyzethez

A hosszú munkák (leirat, képkockák) külön folyamatban futnak (worker.py), mert a Hub
eszköz-időkorlátja 120 s. Az indító eszköz azonnal visszaad egy job-azonosítót;
az állapotot a media_job adja (wait_s-sel meg is várható).
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
OUT = HERE.parent.parent / "out" / "media"
JOBS = OUT / "jobs"


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _probe(path: Path) -> dict:
    r = subprocess.run([_ffmpeg(), "-hide_banner", "-i", str(path)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None
    v = re.search(r"Video: (\w+).*?, (\d{2,5})x(\d{2,5})", r.stderr)
    a = re.search(r"Audio: (\w+)", r.stderr)
    return {"duration_s": round(dur, 1) if dur else None,
            "video": f"{v[1]} {v[2]}x{v[3]}" if v else None,
            "audio": a[1] if a else None}


def _start(kind: str, src: Path, outdir: Path, params: dict) -> dict:
    job = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    jdir = JOBS / job
    jdir.mkdir(parents=True, exist_ok=True)
    spec = {"job": job, "kind": kind, "src": str(src), "out": str(outdir), "params": params}
    (jdir / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), "utf-8")
    (jdir / "status.json").write_text(json.dumps({"state": "queued"}), "utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with open(jdir / "worker.log", "w", encoding="utf-8") as log:
        subprocess.Popen([sys.executable, str(HERE / "worker.py"), str(jdir)], stdout=log,
                         stderr=subprocess.STDOUT, creationflags=flags, cwd=str(jdir))
    return {"ok": True, "job": job, "out": str(outdir)}


def _resolve(path: str):
    p = Path(path).expanduser()
    if not p.is_file():
        return None, {"ok": False, "reason": f"nincs ilyen fájl: {path}"}
    return p.resolve(), None


@tool
def media_info(path: str) -> dict:
    """Videó/hang fájl adatai: hossz (s), videó kodek+felbontás, hang kodek."""
    p, err = _resolve(path)
    if err:
        return err
    return {"ok": True, "file": p.name, "mb": round(p.stat().st_size / 1e6, 1), **_probe(p)}


@tool
def media_transcribe(path: str, lang: str = "hu", model: str = "large-v3", out_dir: str = "") -> dict:
    """Leirat háttérben (faster-whisper, GPU ha van): <név>.txt [óó:pp:mm] sorokkal + .srt; job-ot ad vissza."""
    p, err = _resolve(path)
    if err:
        return err
    out = Path(out_dir) if out_dir else OUT / p.stem
    return _start("transcribe", p, out, {"lang": lang or None, "model": model})


@tool
def media_frames(path: str, every_s: float = 5.0, diff: float = 6.0, width: int = 1280, out_dir: str = "") -> dict:
    """Kulcsképkockák háttérben: every_s mp-enként mintát vesz, csak a változókat (diff küszöb) menti, index.json-nal."""
    p, err = _resolve(path)
    if err:
        return err
    out = Path(out_dir) if out_dir else OUT / p.stem / "frames"
    return _start("frames", p, out, {"every_s": every_s, "diff": diff, "width": width})


@tool
def media_notify(job: str = "running", step_pct: int = 10, dry: bool = False) -> dict:
    """Windows-értesítő a haladásról (step_pct %-onként + kész/hiba); job: azonosító, 'last' vagy 'running' (mind a futók)."""
    jobs = sorted(JOBS.glob("*")) if JOBS.exists() else []
    if job == "running":
        sel = [j for j in jobs if json.loads((j / "status.json").read_text("utf-8")).get("state") in ("queued", "running")]
    elif job == "last":
        sel = jobs[-1:]
    else:
        sel = [JOBS / job] if re.fullmatch(r"[\w-]+", job) and (JOBS / job / "spec.json").exists() else []
    if not sel:
        return {"ok": False, "reason": f"nincs figyelhető job: {job}"}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log = open(sel[-1] / "notify.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(HERE / "notify.py"), str(int(step_pct)), "1" if dry else "0",
                             *map(str, sel)], stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    log.close()
    return {"ok": True, "watching": [j.name for j in sel], "pid": proc.pid}


@tool
def media_job(job: str = "last", wait_s: int = 0) -> dict:
    """Job állapota (state, progress 0–1, eredmény-útvonalak); job='last' a legutóbbi; wait_s ≤ 100 várakozik a végéig."""
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
        except (json.JSONDecodeError, OSError):
            st = {"state": "running"}
        if st.get("state") in ("done", "error") or time.time() >= deadline:
            break
        time.sleep(1)
    if st.get("state") == "error":
        log = (jdir / "worker.log").read_text("utf-8", errors="replace")[-400:] if (jdir / "worker.log").exists() else ""
        return {"ok": False, "job": job, "reason": st.get("error", "hiba"), "log": log}
    return {"ok": True, "job": job, **st}
