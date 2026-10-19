"""A media plugin háttérfolyamata: python worker.py <job-mappa>  (spec.json → status.json)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

JDIR = Path(sys.argv[1])
SPEC = json.loads((JDIR / "spec.json").read_text("utf-8"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # C:\HUB → hubkit
try:
    from hubkit import progress
except Exception:  # noqa: BLE001 – a folyamatjelző nélkül is működjön
    progress = None
TITLE = {"transcribe": "Leirat", "frames": "Képkockák"}.get(SPEC["kind"], SPEC["kind"]) + ": " + Path(SPEC["src"]).stem
REPORT = progress is not None and "_test" not in SPEC["out"]


def status(**kw) -> None:
    tmp = JDIR / "status.tmp"
    tmp.write_text(json.dumps(kw, ensure_ascii=False), "utf-8")
    os.replace(tmp, JDIR / "status.json")
    if REPORT:
        st = kw.get("state")
        if st == "done":
            detail = f"{kw['segments']} szakasz" if "segments" in kw else f"{kw.get('kept', '?')} kép"
        elif st == "error":
            detail = kw.get("error", "")[:80]
        else:
            detail = " · ".join(str(x) for x in (kw.get("step"), kw.get("at")) if x)
        try:
            progress(f"media-{SPEC['job']}", TITLE, float(kw.get("progress") or 0) * 100, detail,
                     "error" if st == "error" else "done" if st == "done" else "running")
        except Exception:  # noqa: BLE001
            pass


def hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def cuda_dll_path() -> None:
    """A pip-es nvidia-* csomagok DLL-jei (cuBLAS, cuDNN) a ctranslate2 számára."""
    try:
        import nvidia
    except ImportError:
        return
    for base in nvidia.__path__:
        for b in Path(base).glob("*/bin"):
            os.environ["PATH"] = str(b) + os.pathsep + os.environ["PATH"]
            os.add_dll_directory(str(b))


def load_model(name: str):
    from faster_whisper import WhisperModel
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return WhisperModel(name, device="cuda", compute_type="int8_float16"), "cuda"
    except Exception as e:  # noqa: BLE001
        print("CUDA nem elérhető, CPU:", e, flush=True)
    return WhisperModel(name, device="cpu", compute_type="int8"), "cpu"


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


def transcribe(src: Path, out: Path, p: dict) -> dict:
    cuda_dll_path()
    status(state="running", step="modell betöltése", progress=0)
    model, device = load_model(p["model"])
    import numpy as np
    status(state="running", step="hang kinyerése", progress=0, device=device)
    # saját dekódolás (PyAV helyett): 16 kHz mono float32
    raw = subprocess.run([ffmpeg(), "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "f32le", "-"], check=True, capture_output=True).stdout
    audio = np.frombuffer(raw, dtype=np.float32)
    segs, info = model.transcribe(audio, language=p["lang"], vad_filter=True, beam_size=5,
                                  condition_on_previous_text=False)
    dur = info.duration or 1
    txt, srt, t0, n = [], [], time.time(), 0
    for s in segs:
        n += 1
        line = s.text.strip()
        txt.append(f"[{hms(s.start)}] {line}")
        srt.append(f"{n}\n{srt_time(s.start)} --> {srt_time(s.end)}\n{line}\n")
        if n % 20 == 0:
            status(state="running", step="leirat", device=device, progress=round(s.end / dur, 3),
                   at=hms(s.end), elapsed_s=int(time.time() - t0))
    out.mkdir(parents=True, exist_ok=True)
    tf, sf = out / f"{src.stem}.txt", out / f"{src.stem}.srt"
    tf.write_text("\n".join(txt) + "\n", "utf-8")
    sf.write_text("\n".join(srt), "utf-8")
    return {"txt": str(tf), "srt": str(sf), "segments": n, "lang": info.language, "device": device,
            "duration_s": round(dur, 1), "elapsed_s": int(time.time() - t0)}


def frames(src: Path, out: Path, p: dict) -> dict:
    from PIL import Image, ImageChops, ImageStat
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("f_*.jpg"):
        old.unlink()
    every = float(p["every_s"])
    status(state="running", step="mintavétel", progress=0)
    with tempfile.TemporaryDirectory() as td:
        subprocess.run([ffmpeg(), "-v", "error", "-y", "-i", str(src), "-an",
                        "-vf", f"fps=1/{every},scale={int(p['width'])}:-2", "-q:v", "3",
                        str(Path(td) / "s_%06d.jpg")], check=True)
        samples = sorted(Path(td).glob("s_*.jpg"))
        kept, last = [], None
        for i, f in enumerate(samples):
            img = Image.open(f)
            thumb = img.convert("L").resize((96, 64))
            d = 255.0 if last is None else ImageStat.Stat(ImageChops.difference(thumb, last)).mean[0]
            if d >= float(p["diff"]):
                t = i * every
                name = f"f_{hms(t).replace(':', '')}.jpg"
                img.save(out / name, quality=85)
                kept.append({"t": hms(t), "file": name, "diff": round(d, 1)})
                last = thumb
            if i % 50 == 0:
                status(state="running", step="szűrés", progress=round(i / max(len(samples), 1), 3))
    (out / "index.json").write_text(json.dumps(kept, ensure_ascii=False, indent=0), "utf-8")
    return {"dir": str(out), "index": str(out / "index.json"), "samples": len(samples), "kept": len(kept)}


if __name__ == "__main__":
    try:
        fn = {"transcribe": transcribe, "frames": frames}[SPEC["kind"]]
        res = fn(Path(SPEC["src"]), Path(SPEC["out"]), SPEC["params"])
        status(state="done", progress=1, **res)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        status(state="error", error=f"{type(e).__name__}: {e}"[:300])
