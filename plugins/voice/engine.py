"""voice motor: mikrofonfelvétel csendig + magyar leirat (faster-whisper, GPU ha van).

Külön folyamatban fut (a Hub ne tartsa a modellt a videómemóriában):
    python engine.py listen <max_s> <silence_s> <wait_s>   → egy sor JSON: {"ok", "text", ...}
    python engine.py file <útvonal>                         → egy sor JSON
A diktáló (dictate.py) ugyanezt modulként használja, a modellt betöltve tartja.
"""
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

RATE = 16000
MODEL = os.environ.get("HUB_VOICE_MODEL", "large-v3")
PROMPT = "Magyar nyelvű diktálás, helyes írásjelekkel és ékezetekkel."


def cuda_dll_path() -> None:
    """A pip-es nvidia-* csomagok DLL-jei (cuBLAS, cuDNN) a ctranslate2 számára (mint a media pluginban)."""
    try:
        import nvidia
    except ImportError:
        return
    for base in nvidia.__path__:
        for b in Path(base).glob("*/bin"):
            os.environ["PATH"] = str(b) + os.pathsep + os.environ["PATH"]
            os.add_dll_directory(str(b))


def load_model(name: str = MODEL):
    cuda_dll_path()
    from faster_whisper import WhisperModel
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return WhisperModel(name, device="cuda", compute_type="int8_float16"), "cuda"
    except Exception:  # noqa: BLE001
        pass
    return WhisperModel(name, device="cpu", compute_type="int8"), "cpu"


def voice_level(audio) -> float:
    """A beszélő hangereje egy felvételből (a 30 ms-os keretek 95. percentilise) – a közelebbi, hangosabb
    hang (a felhasználó) így elválik a háttérben szóló tévétől/rádiótól."""
    n = 480
    frames = audio[: len(audio) // n * n].reshape(-1, n)
    if not len(frames):
        return 0.0
    return float(np.percentile(np.sqrt((frames * frames).mean(axis=1)), 95))


def record(max_s: float = 30, silence_s: float = 1.5, wait_s: float = 8, stop=None, on_level=None,
           floor: float = 0.0):
    """Felvétel az alap mikrofonról, amíg beszéd után silence_s csend nem jön (vagy stop.is_set()).
    floor: ennél halkabb hang csendnek számít (pl. a beszélő hangerejének egy része – így a tévé nem tartja
    nyitva a felvételt). Vissza: (16 kHz-es mono float32 tömb vagy None, ha nem szólalt meg senki, info)."""
    import sounddevice as sd

    dev = sd.query_devices(kind="input")
    sr = int(dev["default_samplerate"])
    block = int(sr * 0.03)                      # 30 ms-os keretek
    frames, rms_hist = [], []
    started, last_voice, t0 = None, None, time.time()
    noise = None
    with sd.InputStream(samplerate=sr, channels=1, dtype="float32", blocksize=block) as st:
        while True:
            data, _ = st.read(block)
            x = data[:, 0].copy()
            frames.append(x)
            rms = float(np.sqrt(np.mean(x * x)) + 1e-9)
            now = time.time() - t0
            if now < 0.3:                       # az első 0,3 mp a háttérzaj mérése
                rms_hist.append(rms)
                continue
            if noise is None:
                # alsó percentilis, nem medián: ha a csippanás után azonnal beszélni kezd, a mérés ne a beszédet
                # vegye háttérzajnak (különben a szó eleje „csendnek" számít és levágódik)
                noise = float(np.percentile(rms_hist, 20)) if rms_hist else 0.002
            thr = max(noise * 3.0, 0.008, floor)
            if on_level:
                on_level(rms / thr)
            if rms > thr:
                last_voice = now
                started = started or now
            if stop is not None and stop.is_set():
                break
            if started is None and now > wait_s:
                return None, {"reason": "nem hallottam beszédet", "noise": round(noise, 4)}
            if started is not None and now - last_voice > silence_s:
                break
            if now > max_s:
                break
    audio = np.concatenate(frames)
    if started is None:
        return None, {"reason": "nem hallottam beszédet", "noise": round(noise or 0, 4)}
    audio = resample(audio, sr)
    return audio, {"seconds": round(len(audio) / RATE, 1), "mic": dev["name"]}


def resample(x, sr: int):
    """Tetszőleges mintavételezésű mono jel → 16 kHz float32 (beszédhez a lineáris interpoláció elég)."""
    if sr == RATE:
        return x.astype(np.float32)
    n = int(len(x) * RATE / sr)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def decode(path: str):
    """Bármilyen hang/videó → 16 kHz mono float32, ffmpeg-gel (a PyAV-os dekódoló a mostani av-verzióval nem megy)."""
    import subprocess

    import imageio_ffmpeg
    r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-i", path, "-f", "s16le",
                        "-ac", "1", "-ar", str(RATE), "pipe:1"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=600)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg: " + r.stderr.decode("utf-8", "replace")[-200:])
    return np.frombuffer(r.stdout, np.int16).astype(np.float32) / 32768.0


# A whisper csendre / zajra jellemzően ezeket „hallja" (a betanítóanyag feliratainak maradványai).
HALLUCINATIONS = ("amara.org", "feliratok", "felirat:", "köszönöm, hogy megnézted", "köszönöm a figyelmet",
                  "iratkozz fel", "kövess", "nézd meg a többi videó", "a videót", "subtitles", "thanks for watching")


def transcribe(model, audio, lang: str = "hu", hotwords: str | None = None) -> str:
    """Leirat a kitalált szövegek nélkül: rövid hangnál nincs súgószöveg (azt visszamondaná),
    a „nem beszéd" szakaszok és a jól ismert hallucinációk kiesnek."""
    prompt = PROMPT if len(audio) > RATE * 4 else None
    segs, _ = model.transcribe(audio, language=lang, beam_size=5, vad_filter=True, initial_prompt=prompt,
                               condition_on_previous_text=False, hotwords=hotwords)
    keep = []
    for s in segs:
        t = s.text.strip()
        if not t or s.no_speech_prob > 0.6 or any(h in t.lower() for h in HALLUCINATIONS):
            continue
        if s.compression_ratio > 2.4:  # ismétlődő zagyvaság („a különböző a különböző…")
            continue
        keep.append(t)
    return " ".join(keep).strip()


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def wake_split(text: str, word: str, min_ratio: float = 0.70) -> tuple[bool, str]:
    """Az ébresztőszóval kezdődik-e a leirat (kis félrehallást is elfogad)? Vissza: (igen, a maradék kérés)."""
    import difflib
    import re as _re
    words = _re.findall(r"\w+", _norm(text))
    target = _norm(word)
    for i, w in enumerate(words[:2]):  # „Na, figyelj…", „Hé figyelj…" is jó
        # a hossz is számít: a „figyelem", „figyeljem", „figyeljetek" más szó, nem ébreszt
        if len(target) - 1 <= len(w) <= len(target) and difflib.SequenceMatcher(None, w, target).ratio() >= min_ratio:
            raw = _re.findall(r"\S+", text)
            rest = " ".join(raw[i + 1:]).lstrip(" ,.!?:;–-")
            return True, rest
    return False, ""


def main(argv: list[str]) -> dict:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "listen":
        max_s, silence_s, wait_s = (float(v) for v in argv[2:5])
        import winsound
        winsound.Beep(880, 120)                 # jelzés: most beszélj
        audio, info = record(max_s, silence_s, wait_s)
        winsound.Beep(660, 120)
        if audio is None:
            return {"ok": False, **info}
        t = time.time()
        model, device = load_model()
        load_s = round(time.time() - t, 1)
        t = time.time()
        text = transcribe(model, audio)
        return {"ok": True, "text": text, **info, "device": device, "load_s": load_s, "stt_s": round(time.time() - t, 1)}
    if cmd == "file":
        audio = decode(argv[2])
        if len(audio) > RATE * 600:
            return {"ok": False, "reason": "10 percnél hosszabb – ahhoz a media_transcribe való"}
        t = time.time()
        model, device = load_model()
        text = transcribe(model, audio, argv[3] if len(argv) > 3 else "hu")
        return {"ok": True, "text": text, "seconds": round(len(audio) / RATE, 1), "device": device,
                "total_s": round(time.time() - t, 1)}
    return {"ok": False, "reason": f"ismeretlen parancs: {cmd}"}


if __name__ == "__main__":
    import contextlib
    try:
        with contextlib.redirect_stdout(sys.stderr):
            res = main(sys.argv)
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "reason": f"{type(e).__name__}: {e}"[:300]}
    sys.stdout.write(json.dumps(res, ensure_ascii=False))
