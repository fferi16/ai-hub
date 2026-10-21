"""Élethű próba az ébresztőszó-modellhez: egy hosszú, folyamatos hangfolyamot (magyar beszéd, tévészerű
háttér, csend) játszik át a modellen 80 ms-os darabokban – pontosan úgy, ahogy a mikrofonról jönne –,
közben elszórva „Figyelj"-ekkel. Mér: hány ébresztőszót talált el, hányszor ébredt tévesen, mennyi a késés.

    python plugins\\voice\\tests\\wake_stream_check.py [küszöb]
A gépi hang ugyanaz, amivel tanult (Szabolcs), ezért a valós hangodra ez optimista – arra a voice_enroll való.
"""
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[2]), str(HERE.parent)]
import wake_train as wt  # noqa: E402
import dictate  # noqa: E402
import time  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402
from openwakeword.utils import AudioFeatures  # noqa: E402

rng = np.random.default_rng(123)  # más mag, mint a tanításnál
det = wt.Detector(HERE.parent / "models" / "figyelj.npz")
thr = float(sys.argv[1]) if len(sys.argv) > 1 else det.threshold
VERIFY = "--verify" in sys.argv
small = WhisperModel("base", device="cpu", compute_type="int8") if VERIFY else None
ver_t = []
SR = wt.SR

# hangfolyam: más mondatok (nem a tanítóké), tévészerű háttér, és 12 „Figyelj" különböző módon
other = ["A tegnapi meccsen három gól esett.", "Kérlek, hozd ide a mérőszalagot.", "A szomszéd kutyája megint ugat.",
         "Holnap reggel nyolckor találkozunk az irodában.", "A fűtést le kellene venni egy kicsit.",
         "Figyelem, figyelem, a hármas vágányra vonat érkezik.", "Ez a figyelmesség nagyon jólesett.",
         "Vigyázz a lépcsőn, csúszik!", "Fizetés előtt nézd meg a számlát.", "A fiatalok a parkban fociznak.",
         "Most jön az időjárás-jelentés.", "A film főszereplője egy fiatal festő.", "Hé, ide figyeljetek egy kicsit!"]
stream, marks, spans = [], [], []
pos_phr = ["Figyelj", "Figyelj!", "Figyelj, mennyi az idő?", "Na, figyelj", "Figyelj, kapcsold le a villanyt."]
t = 0
for k in range(12):
    for _ in range(2):
        txt = other[rng.integers(len(other))]
        s = wt._synth(txt, "hu", rng.uniform(0.85, 1.3), rng.uniform(0.7, 1.3))
        spans.append((t, t + len(s), txt))
        stream.append(s * rng.uniform(0.2, 0.6)); t += len(s)
        gap = np.zeros(int(rng.uniform(0.3, 1.5) * SR), np.float32); stream.append(gap); t += len(gap)
    p = wt._synth(pos_phr[k % len(pos_phr)], "hu", rng.uniform(0.85, 1.4), rng.uniform(0.6, 1.4)) * rng.uniform(0.4, 0.9)
    marks.append(t + len(wt._trim(wt._synth("Figyelj", "hu", 1.0, 1.0))) )  # kb. a szó vége (a kezdethez képest)
    stream.append(p.astype(np.float32)); t += len(p)
    gap = np.zeros(int(rng.uniform(1.0, 2.0) * SR), np.float32); stream.append(gap); t += len(gap)
audio = np.concatenate(stream)
babble = np.concatenate([wt._synth(s, "hu", 1.1, 1.0) for s in other[:6]])
audio = wt._mix(audio, np.tile(babble, len(audio) // len(babble) + 1)[: len(audio)], 15)  # halk „tévé" alatta
audio = audio + wt._noise(len(audio), "pink") * 0.002

feat = AudioFeatures(inference_framework="onnx")
x16 = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
hits, prev, last_fire = [], 0.0, -10 * SR
for i in range(0, len(x16) - 1280, 1280):
    feat(x16[i:i + 1280])
    sc = det.score(feat.get_features(16))
    pos = i + 1280
    if sc >= thr and prev >= thr and pos - last_fire > 2 * SR:  # két egymást követő keret, 2 s holtidő
        last_fire = pos
        if VERIFY:  # 2. lépcső: a kis whisper a jelzés előtti 2 s-on
            t1 = time.time()
            segs, _ = small.transcribe(audio[max(0, pos - 2 * SR):pos], language="hu", beam_size=1,
                                       vad_filter=False, condition_on_previous_text=False, hotwords="Figyelj")
            heard = " ".join(x.text for x in segs)
            ver_t.append(time.time() - t1)
            if not dictate.wake_split(heard, "figyelj")[0]:
                print(f"  elvetve {pos / SR:6.1f} s: {heard!r}")
                prev = sc
                continue
        hits.append(pos)
    prev = sc
found, false, lat = 0, 0, []
used = set()
for h in hits:
    near = [m for m in marks if -0.2 * SR <= h - m <= 2.5 * SR and m not in used]
    if near:
        used.add(near[0]); found += 1; lat.append((h - near[0]) / SR)
    else:
        false += 1
        where = [txt for a, b, txt in spans if a - 0.3 * SR <= h <= b + 1.0 * SR]
        print(f"  téves ébredés {h / SR:6.1f} s: {where[:1] or ['(háttér / csend)']}")
mins = len(audio) / SR / 60
print(f"küszöb {thr:.2f}: {found}/{len(marks)} ébresztőszó felismerve, {false} téves ébredés "
      f"{mins:.1f} perc hangban; késés a szó végétől: {np.round(lat, 2).tolist()}"
      + (f"; ellenőrzés átlag {np.mean(ver_t):.2f} s" if ver_t else ""))
sys.exit(0 if found >= len(marks) - 1 and false == 0 else 1)
