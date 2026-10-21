"""Saját ébresztőszó-modell tanítása az openWakeWord hangbeágyazásain (a „Hey Google"-féle megoldás kicsiben).

    python wake_train.py [szó=figyelj]      → out/voice/models/<szó>.npz (+ kiértékelés a kimeneten)
    (saját hangminták nélkül – az általános, repóban tartott modell – plugins/voice/models/<szó>.npz)

Pozitív minták: a szó gépi hangon (Windows Szabolcs) sokféle tempóval/hangmagassággal, és ha vannak,
a felhasználó saját felvételei (out/voice/enroll/*.wav – voice_enroll készíti) – ezek a legértékesebbek.
Mind dúsítva: tempó, hangerő, zaj, háttérbeszéd (tévé), visszhang, mikrofon-színezet.
Negatív minták: magyar mondatok és a HASONLÓ szavak (figyelem, vigyázz…), angol hang, zaj, csend,
a szó csonkja, és az az ablak, ahol a szó már régen elhangzott (így a jelzés a szó VÉGÉN jön).
Osztályozó: kis neurális háló (sklearn MLP), numpy-ban futtatható súlyokként mentve (nincs pickle).
"""
import asyncio
import io
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
MODELS = HERE / "models"                      # az általános modell (a repóban, saját hang nélkül)
USER_MODELS = ROOT / "out" / "voice" / "models"  # a felhasználó hangjára tanított modell (nem kerül a repóba)
ENROLL = ROOT / "out" / "voice" / "enroll"
SR, WIN = 16000, 32000            # 2 s-os ablak = 16 beágyazási keret
rng = np.random.default_rng(7)

NEG_SENTENCES = [
    "Ma nagyon szép idő van odakint.", "Holnap megyek a boltba kenyérért.", "Figyelem, a vonat késik.",
    "Figyeljetek a gyerekekre!", "Vigyázz, jön az autó!", "Fizetés után kérem a blokkot.", "Ez egy fiatal fa.",
    "A figura jól sikerült.", "Egyél még egy kicsit.", "Figyel rám valaki?", "A fejlesztés jól halad.",
    "Hé, gyere ide!", "Igen, rendben van.", "Nem tudom, majd meglátjuk.", "Mennyi az idő?", "Kapcsold be a tévét.",
    "A híradó nyolc órakor kezdődik.", "Az időjárás holnap esős lesz.", "Kérek egy kávét tejjel.",
    "A meccs döntetlennel végződött.", "Ez a film nagyon izgalmas volt.", "Hívd fel anyukádat este.",
    "Figyelni kell a részletekre.", "Fiú vagy lány?", "A fülke foglalt.", "Fogd meg a kezem.",
    "Felejtsd el, nem fontos.", "Egy pillanat, mindjárt jövök.", "Hol van a távirányító?",
    "A tervrajz holnapra kész lesz.", "Az ablak mérete ezerkétszáz milliméter.", "Küldd el az árajánlatot.",
    "A hűtőben van még sajt.", "Szép napot kívánok!", "Köszönöm szépen a segítséget.", "Viszontlátásra!",
    "Gyere, menjünk sétálni.", "Fél hétkor indul a busz.", "Figyelemre méltó eredmény.", "Filmet nézünk.",
    "Fiókban van a kulcs.", "Fél kiló kenyeret kérek.", "Vigyél magaddal esernyőt.", "Figyelő szemek kísérik.",
    "Vegyél tejet hazafelé.", "Higgadj le, nincs semmi baj.", "Fejjel lefelé lóg a kép.", "Igyál egy pohár vizet.",
]
NEG_WORDS = ["figyelem", "figyeljetek", "figyelni", "figyel", "figyelő", "vigyázz", "fizetés", "fiatal",
             "figura", "fejlesztés", "fegyelem", "fillér", "fiókban", "egyél", "nyelj", "hé", "igen", "nem",
             "gyere", "jó", "persze", "vigyél", "higgy", "felejtsd", "fejjel", "fegyver"]
# a szó rokonai MONDATBAN – ezek a legnehezebbek (a próba mondatai szándékosan nincsenek köztük)
CONFUSABLE = ["Figyelem, a bolt tíz perc múlva bezár.", "Köszönöm a figyelmet.", "Figyelmeztetés: alacsony az akkumulátor.",
              "Figyelmesen olvasd el a szerződést.", "A figyelő ott ül a sarokban.", "Figyeljetek ide, gyerekek!",
              "Nem figyeltem oda, bocsánat.", "Figyelemre méltó munkát végeztél.", "Ő mindig nagyon figyelmes.",
              "Figyelni fogok rá, ígérem.", "Figyeltek valamit a kertben.", "Figyelmen kívül hagyta a szabályt.",
              "A figyelmetlenség balesethez vezetett.", "Figyelemmel kísérjük az eseményeket.", "Fegyelmezetten álltak sorban.",
              "Fegyelem nélkül nem megy.", "Figyelő szemmel nézte a tájat.", "Vigyél egy kis figyelmet a munkába.",
              "Figyelmeztettelek, hogy esni fog.", "Hé, figyeltek rám egy percre?", "A figyelem a legfontosabb.",
              "Figyeld meg, milyen szép!", "Figyeld a jeleket az úton.", "Figyelt a tanárra az órán."]
EN_SENTENCES = ["Hey, what time is it?", "The weather is nice today.", "Turn off the lights, please.",
                "Breaking news from the capital.", "I will call you tomorrow.", "Let me think about it."]


# ---- hangforrások ----------------------------------------------------------------------

def _synth(text: str, voice_like: str, rate: float, pitch: float) -> np.ndarray:
    from winrt.windows.media.speechsynthesis import SpeechSynthesizer
    from winrt.windows.storage.streams import DataReader

    async def go():
        s = SpeechSynthesizer()
        s.voice = next(v for v in SpeechSynthesizer.all_voices if v.language.lower().startswith(voice_like))
        s.options.speaking_rate, s.options.audio_pitch = rate, pitch
        st = await s.synthesize_text_to_stream_async(text)
        r = DataReader(st.get_input_stream_at(0))
        await r.load_async(st.size)
        buf = bytearray(st.size)
        r.read_bytes(buf)
        return bytes(buf)

    with wave.open(io.BytesIO(asyncio.run(go()))) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
        sr = w.getframerate()
    n = int(len(x) * SR / sr)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def _trim(x: np.ndarray) -> np.ndarray:
    """A szó eleji/végi csend levágása (energia alapján: a csúcs 5%-a alatt csendnek számít)."""
    fr = 160
    e = np.sqrt(np.convolve(x * x, np.ones(fr) / fr, "same"))
    idx = np.where(e > max(e.max() * 0.05, 1e-4))[0]
    return x[idx[0]:idx[-1] + 1] if len(idx) else x


def _load_wav(p: Path) -> np.ndarray:
    with wave.open(str(p)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
        sr = w.getframerate()
        if w.getnchannels() > 1:
            x = x.reshape(-1, w.getnchannels()).mean(axis=1)
    n = int(len(x) * SR / sr)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


# ---- dúsítás ----------------------------------------------------------------------------

def _speed(x, f):
    n = max(10, int(len(x) / f))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def _noise(n, kind):
    w = rng.standard_normal(n).astype(np.float32)
    if kind == "pink":
        w = np.cumsum(w)
        w -= np.convolve(w, np.ones(400) / 400, "same")
    return w / (np.abs(w).max() + 1e-6)


def _reverb(x):
    L = int(SR * rng.uniform(0.05, 0.35))
    ir = rng.standard_normal(L) * np.exp(-np.linspace(0, rng.uniform(4, 9), L))
    ir[0] = 1.0
    y = np.convolve(x, ir)[: len(x)]
    return (y / (np.abs(y).max() + 1e-6) * np.abs(x).max()).astype(np.float32)


def _color(x):
    from scipy.signal import butter, lfilter
    lo, hi = rng.uniform(80, 300), rng.uniform(3000, 7500)
    b, a = butter(2, [lo / (SR / 2), hi / (SR / 2)], "band")
    return lfilter(b, a, x).astype(np.float32)


def _mix(x, bg, snr_db):
    if len(bg) < len(x):
        bg = np.tile(bg, len(x) // max(len(bg), 1) + 1)
    s = rng.integers(0, len(bg) - len(x) + 1)
    bg = bg[s:s + len(x)]
    px, pb = np.mean(x * x) + 1e-9, np.mean(bg * bg) + 1e-9
    return x + bg * np.sqrt(px / (pb * 10 ** (snr_db / 10)))


def augment(x, babble):
    if rng.random() < 0.7:
        x = _speed(x, rng.uniform(0.85, 1.18))
    if rng.random() < 0.4:
        x = _reverb(x)
    if rng.random() < 0.5:
        x = _color(x)
    return x * rng.uniform(0.15, 1.0)


def place(word, babble, total=WIN, end_at=None, bg_snr=None):
    """A szót egy 2 s-os ablakba teszi úgy, hogy end_at mintánál érjen véget; háttér: zaj/beszéd/csend."""
    out = np.zeros(total, np.float32)
    end_at = end_at if end_at is not None else int(total - rng.uniform(0.0, 0.25) * SR)
    start = max(0, end_at - len(word))
    seg = word[-(end_at - start):]
    out[start:start + len(seg)] = seg
    r = rng.random()
    if r < 0.35:
        out = _mix(out, babble[rng.integers(len(babble))], bg_snr if bg_snr is not None else rng.uniform(3, 20))
    elif r < 0.7:
        out = _mix(out, _noise(total, rng.choice(["white", "pink"])), rng.uniform(5, 35))
    else:
        out += _noise(total, "white") * rng.uniform(0.0005, 0.004)
    return out


def windows(x, n, babble):
    """Véletlen 2 s-os ablakok egy hosszabb hangból (negatív minták), háttérrel."""
    if len(x) < WIN:
        x = np.concatenate([np.zeros(WIN - len(x) + rng.integers(0, SR), np.float32), x])
    out = []
    for _ in range(n):
        s = rng.integers(0, len(x) - WIN + 1)
        w = x[s:s + WIN] * rng.uniform(0.15, 1.0)
        if rng.random() < 0.5:
            w = _mix(w, _noise(WIN, "pink"), rng.uniform(10, 35))
        out.append(w.astype(np.float32))
    return out


# ---- tanítás ----------------------------------------------------------------------------

def build(word: str):
    t0 = time.time()
    print("hangok készítése…", flush=True)
    phr = [word.capitalize(), word.capitalize() + "!", word.capitalize() + ".", "Na, " + word, "Hé, " + word,
           word.capitalize() + "?"]
    tts_words = []
    for p in phr:
        for _ in range(50):
            tts_words.append(_trim(_synth(p, "hu", rng.uniform(0.7, 1.6), rng.uniform(0.5, 1.5))))
    user_words = [_trim(_load_wav(p)) for p in sorted(ENROLL.glob("*.wav"))] if ENROLL.is_dir() else []
    neg_speech = [_synth(s, "hu", rng.uniform(0.8, 1.4), rng.uniform(0.6, 1.4)) for s in NEG_SENTENCES for _ in range(2)]
    neg_speech += [_synth(s, "hu", rng.uniform(0.8, 1.5), rng.uniform(0.5, 1.5)) for s in CONFUSABLE for _ in range(5)]
    n_sent = len(neg_speech)
    neg_speech += [_synth(w, "hu", rng.uniform(0.8, 1.5), rng.uniform(0.6, 1.4)) for w in NEG_WORDS for _ in range(4)]
    from winrt.windows.media.speechsynthesis import SpeechSynthesizer
    if any(v.language.lower().startswith("en") for v in SpeechSynthesizer.all_voices):  # ha van angol hang is
        neg_speech += [_synth(s, "en", rng.uniform(0.8, 1.3), rng.uniform(0.7, 1.3)) for s in EN_SENTENCES for _ in range(2)]
    babble = [np.concatenate(neg_speech[i:i + 4]) for i in range(0, len(neg_speech) - 4, 4)]
    print(f"  {len(tts_words)} gépi + {len(user_words)} saját „{word}”, {len(neg_speech)} negatív hang "
          f"({time.time() - t0:.0f} s)", flush=True)

    pos, neg, kinds = [], [], []
    for w in tts_words:
        for _ in range(4):
            pos.append(place(augment(w, babble), babble))
    for w in user_words:  # a saját hang a legértékesebb: többszörösen, erősebben dúsítva
        for _ in range(40):
            pos.append(place(augment(w, babble), babble))
    for i, x in enumerate(neg_speech):
        neg += windows(x, 3, babble)
        neg.append(place(augment(x[-int(1.9 * SR):], babble), babble))
        kinds += [f"beszéd:{i}"] * 4
    for w in tts_words[::2]:
        cut = w[: int(len(w) * rng.uniform(0.3, 0.6))]          # a szó csonkja: még nem az
        neg.append(place(augment(cut, babble), babble))
        kinds.append("csonk")
        late = place(augment(w, babble), babble, end_at=int(rng.uniform(0.5, 0.9) * SR))  # rég elhangzott,
        b = babble[rng.integers(len(babble))]                                              # utána más beszéd
        tail = np.resize(b, WIN - SR) * (np.abs(late).max() / (np.abs(b).max() + 1e-6)) * rng.uniform(0.3, 1.0)
        late[SR:] += tail
        neg.append(late)
        kinds.append("régen elhangzott")
    for _ in range(300):
        neg.append(_noise(WIN, rng.choice(["white", "pink"])) * rng.uniform(0.0005, 0.2))
        kinds.append("zaj")
    print(f"  ablakok: {len(pos)} pozitív, {len(neg)} negatív", flush=True)

    from openwakeword.utils import AudioFeatures
    feat = AudioFeatures(inference_framework="onnx", ncpu=4)

    def emb(clips):
        a = np.clip(np.stack(clips) * 32767, -32768, 32767).astype(np.int16)
        return feat.embed_clips(a, batch_size=64, ncpu=4).reshape(len(clips), -1)

    X = np.concatenate([emb(pos), emb(neg)])
    y = np.concatenate([np.ones(len(pos)), np.zeros(len(neg))])
    print(f"  beágyazások: {X.shape} ({time.time() - t0:.0f} s)", flush=True)
    return X, y, ["poz"] * len(pos) + kinds


CORPUS_FILES = [ROOT / "hub-spec.md", ROOT / "look-spec.md", ROOT / "README.md",
                Path.home() / "Documents" / "programok" / "Alnico" / "UJDONSAGOK.md",
                Path.home() / "Documents" / "programok" / "Alnico" / "FUNKCIOK.md"]


def corpus_sentences(word: str, n: int = 450) -> list[str]:
    """Sok, változatos magyar mondat a gépen lévő magyar leírásokból (nehéz negatív mintákhoz)."""
    import re
    out = set()
    for f in CORPUS_FILES:
        try:
            text = f.read_text("utf-8")
        except OSError:
            continue
        text = re.sub(r"`[^`]*`|\*\*|[#>|*_\[\]()]", " ", text)
        for s in re.split(r"(?<=[.!?])\s+|\n", text):
            s = re.sub(r"\s+", " ", s).strip()
            if 20 <= len(s) <= 140 and re.search(r"[áéíóöőúüű]", s) and word not in s.lower():
                out.add(s)
    out = sorted(out)
    rng.shuffle(out)
    return out[:n]


def _all_windows(feat, audio: np.ndarray) -> np.ndarray:
    """Egy hosszú hang minden 80 ms-os lépésű, 16 keretes ablaka (mint élőben a mikrofonon)."""
    x = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
    e = feat._get_embeddings(x)
    if len(e) < 16:
        return np.zeros((0, 16 * e.shape[-1]), np.float32)
    idx = np.arange(16)[None, :] + np.arange(len(e) - 15)[:, None]
    return e[idx].reshape(len(idx), -1)


def _fit(X, y):
    from sklearn.neural_network import MLPClassifier
    from sklearn.preprocessing import StandardScaler
    sc = StandardScaler().fit(X)
    clf = MLPClassifier(hidden_layer_sizes=(96,), alpha=3e-3, max_iter=400, early_stopping=True,
                        random_state=1).fit(sc.transform(X), y)
    return sc, clf


def train(word: str, rounds: int = 2) -> dict:
    from openwakeword.utils import AudioFeatures
    from sklearn.model_selection import train_test_split

    X, y, kinds = build(word)
    Xtr, Xte, ytr, yte, _, kte = train_test_split(X, y, np.array(kinds), test_size=0.2, random_state=1, stratify=y)
    sc, clf = _fit(Xtr, ytr)

    # NEHÉZ NEGATÍVOK: sok további magyar beszéd, háttérrel; amit a modell tévesen „szónak" hall, az tanítóanyag lesz
    t0 = time.time()
    feat = AudioFeatures(inference_framework="onnx", ncpu=4)
    sents = corpus_sentences(word)
    corpus = []
    for s in sents:
        a = _synth(s, "hu", rng.uniform(0.8, 1.45), rng.uniform(0.55, 1.45)) * rng.uniform(0.15, 1.0)
        if rng.random() < 0.5:
            a = _mix(a, _noise(len(a), "pink"), rng.uniform(8, 30))
        corpus.append(np.concatenate([np.zeros(int(rng.uniform(0.2, 1.0) * SR), np.float32), a.astype(np.float32)]))
    Wneg = np.concatenate([_all_windows(feat, a) for a in corpus])
    print(f"  nehéz negatívok forrása: {len(sents)} mondat, {len(Wneg)} ablak ({time.time() - t0:.0f} s)", flush=True)
    extra = np.zeros((0, Xtr.shape[1]), np.float32)
    for r in range(rounds):
        p = clf.predict_proba(sc.transform(Wneg))[:, 1]
        hard = Wneg[p > 0.2]
        easy = Wneg[rng.choice(len(Wneg), size=min(1500, len(Wneg)), replace=False)]
        extra = np.concatenate([extra, hard, easy])
        print(f"  {r + 1}. kör: {int((p > 0.5).sum())} ablak hamisan pozitív (>{0.5}), {len(hard)} nehéz negatív hozzáadva",
              flush=True)
        sc, clf = _fit(np.concatenate([Xtr, extra]), np.concatenate([ytr, np.zeros(len(extra))]))
    p_end = clf.predict_proba(sc.transform(Wneg))[:, 1]
    print(f"  a végén: {int((p_end > 0.5).sum())} hamis ablak a {len(Wneg)}-ből", flush=True)
    p = clf.predict_proba(sc.transform(Xte))[:, 1]
    best = None
    print("  küszöb  felismerés  téves (db)", flush=True)
    for thr in np.arange(0.3, 0.991, 0.05):
        tp = int(((p >= thr) & (yte == 1)).sum())
        fp = int(((p >= thr) & (yte == 0)).sum())
        rec, far = tp / max(1, (yte == 1).sum()), fp / max(1, (yte == 0).sum())
        print(f"   {thr:.2f}    {rec:6.1%}     {fp}", flush=True)
        if far <= 0.01 and (best is None or rec > best[1]):
            best = (float(thr), rec, far)
    wrong = np.where((p >= (best or (0.9,))[0]) & (yte == 0))[0]
    if len(wrong):
        print(f"  téves: {[(kte[i], round(float(p[i]), 2)) for i in wrong]}", flush=True)
    thr, rec, far = best or (0.9, None, None)
    n_user = len(list(ENROLL.glob("*.wav"))) if ENROLL.is_dir() else 0
    folder = USER_MODELS if n_user else MODELS
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{word}.npz"
    np.savez(out, mean=sc.mean_, scale=sc.scale_, W0=clf.coefs_[0], b0=clf.intercepts_[0], W1=clf.coefs_[1],
             b1=clf.intercepts_[1], threshold=np.array(thr),
             meta=np.array(json.dumps({"word": word, "trained": time.strftime("%Y-%m-%d %H:%M"),
                                       "positives": int((y == 1).sum()), "negatives": int((y == 0).sum()),
                                       "user_samples": n_user})))
    return {"model": str(out), "threshold": round(thr, 2), "recall": round(rec, 3) if rec is not None else None,
            "false_accept": round(far, 4) if far is not None else None, "test_pos": int((yte == 1).sum()),
            "test_neg": int((yte == 0).sum())}


def model_path(word: str) -> Path:
    """A használandó modell: a saját hangra tanított, ha van, különben az általános."""
    user = USER_MODELS / f"{word}.npz"
    return user if user.exists() else MODELS / f"{word}.npz"


class Detector:
    """A betanított modell futtatása: 80 ms-onként egy valószínűség (numpy, kis háló)."""

    def __init__(self, path: Path):
        m = np.load(path, allow_pickle=False)
        self.mean, self.scale = m["mean"], m["scale"]
        self.W0, self.b0, self.W1, self.b1 = m["W0"], m["b0"], m["W1"], m["b1"]
        self.threshold = float(m["threshold"])
        self.meta = json.loads(str(m["meta"]))

    def score(self, feats: np.ndarray) -> float:
        x = (feats.reshape(-1) - self.mean) / self.scale
        h = np.maximum(0, x @ self.W0 + self.b0)
        z = float((h @ self.W1 + self.b1)[0])
        return 1 / (1 + np.exp(-z))


if __name__ == "__main__":
    print(json.dumps(train(sys.argv[1] if len(sys.argv) > 1 else "figyelj"), ensure_ascii=False))
