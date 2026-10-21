"""A gyors ébresztőszó-ciklus (dictate.fast_loop) próbája mikrofon nélkül: hamis „mikrofon" adja a hangot
(beszéd, „Figyelj", szünet, kérés), és megnézzük, milyen kérés jut el az asszisztenshez és mennyi idő alatt.
    python plugins\\voice\\tests\\fast_loop_check.py
"""
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[2]), str(HERE.parent)]
import dictate  # noqa: E402
import sounddevice as sd  # noqa: E402
import wake_train as wt  # noqa: E402

dictate.big_model(quiet=True)  # a GPU-s modell ELŐBB, mint a WinRT-s hangkészítés (különben összeomlik)

SR = 16000
parts = [np.zeros(SR, np.float32), wt._synth("A fűtést le kellene venni egy kicsit.", "hu", 1.0, 1.0) * 0.3,
         np.zeros(int(0.8 * SR), np.float32), wt._synth("Figyelj!", "hu", 1.0, 1.0) * 0.8,
         np.zeros(int(0.4 * SR), np.float32), wt._synth("Hány óra van most?", "hu", 1.0, 1.0) * 0.8,
         np.zeros(3 * SR, np.float32)]
audio = np.concatenate(parts) + np.random.default_rng(0).standard_normal(sum(map(len, parts))).astype(np.float32) * 0.001
word_end = sum(map(len, parts[:4])) / SR


class FakeStream:
    def __init__(self, **kw):
        self.pos, self.t0 = 0, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n):
        if self.pos + n > len(audio):
            raise StopIteration("vége")
        x = audio[self.pos:self.pos + n]
        self.pos += n
        if self.pos / SR >= word_end and FakeStream.word_seen is None:
            FakeStream.word_seen = time.time()
        return x.reshape(-1, 1), False


FakeStream.word_seen = None
sd.query_devices = lambda kind=None: {"default_samplerate": SR, "name": "hamis"}
sd.InputStream = FakeStream
got = {}


def fake_answer(command, backend):
    got.update(command=command, t=time.time())


dictate.answer = fake_answer
dictate.show = lambda *a, **k: None
dictate.log = lambda m: print("  napló:", m)
try:
    dictate.fast_loop("figyelj", "claude")
except StopIteration:
    pass
for _ in range(100):
    if got or not dictate._busy.locked():
        break
    time.sleep(0.1)
time.sleep(0.5)
ok = "óra" in got.get("command", "").lower()
print(f"kérés: {got.get('command')!r}; {'OK' if ok else 'HIBA'}")
sys.exit(0 if ok else 1)
