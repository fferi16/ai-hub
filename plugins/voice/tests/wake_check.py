"""Ébresztőszó-próba mikrofon nélkül: a tts hangjával mondatokat generál, és megnézi, melyikre ébred
a kis modell + a wake_split (ugyanaz az út, mint a dictate.py-ban).   python plugins\\voice\\tests\\wake_check.py"""
import importlib.util
import io
import sys
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT), str(HERE.parent)]
import dictate  # noqa: E402
import engine  # noqa: E402

spec = importlib.util.spec_from_file_location("tts_plugin", ROOT / "plugins" / "tts" / "plugin.py")
tts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tts)
from faster_whisper import WhisperModel  # noqa: E402

small = WhisperModel("base", device="cpu", compute_type="int8")
CASES = [  # (mondat, ébredjen-e, várt kérés-részlet)
    ("Figyelj! Mennyi az idő?", True, "idő"),
    ("Figyelj, nyisd meg az utolsó rajzot.", True, "rajzot"),
    ("Figyelj.", True, ""),
    ("Na, figyelj, mi a helyzet?", True, "helyzet"),
    ("Ma nagyon szép idő van odakint.", False, ""),
    ("Holnap megyek a boltba kenyérért.", False, ""),
    ("Figyelem, a vonat késik.", False, ""),
]
fails = 0
for text, want, part in CASES:
    res, _ = tts._render(text, "", 1.0, 1.0, 1.0)
    with wave.open(io.BytesIO(res[0])) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
        audio = engine.resample(x, w.getframerate())
    segs, _ = small.transcribe(audio[: engine.RATE * 3], language="hu", beam_size=1, vad_filter=False,
                               condition_on_previous_text=False, hotwords="Figyelj")
    heard = " ".join(s.text for s in segs).strip()
    ok, rest = dictate.wake_split(heard, "figyelj")
    good = ok == want and (not part or part in dictate._norm(rest) or part in rest)
    fails += not good
    print(("OK  " if good else "HIBA"), f"{text!r} → hallott: {heard!r} → ébred: {ok}, kérés: {rest!r}")
print("EREDMÉNY:", "minden rendben" if not fails else f"{fails} hiba")
sys.exit(1 if fails else 0)
