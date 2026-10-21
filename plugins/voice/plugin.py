"""voice – magyar beszédfelismerés helyben (faster-whisper large-v3, GPU-n): beszélj a géphez.

  Diktálás bárhova: Ctrl+Alt+D → csippanás, beszélsz, csend (vagy újra a gyorsbillentyű) → a leirat a kurzorhoz.
  Hangasszisztens: „Figyelj, …kérés…" → a Claude (Hub-eszközökkel) válaszol, hangosan; Ctrl+Alt+D elhallgattatja.
    config.json → "voice": {"dictation": true, "hotkey": "Ctrl+Alt+D", "wake": "figyelj", "backend": "claude"}
    ("wake": "" kikapcsolja; "backend": "local" = helyi Ollama-modell). A Hubbal együtt indul és áll le.
    A „Figyelj"-hallgatás menet közben ki/be kapcsolható (a mikrofont is elengedi): Ctrl+Alt+F ("wake_hotkey"),
    tálcamenü pipa, voice_dictation(action="wake_off"/"wake_on"); az állapot újraindítás után is megmarad.
  voice_assistant  – ugyanez szövegből (próbához; speak=true esetén fel is olvassa)
  voice_listen     – Claude kér: „mondd el" → a mikrofonról csendig felvesz, visszaadja a szöveget
  voice_transcribe – rövid hangfájl (≤ 10 perc) leirata azonnal; hosszabbhoz a media_transcribe való
A felvétel és a leirat külön folyamatban fut; a hang sehova nem megy ki a gépről.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from hubkit import tool

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
STATE = ROOT / "out" / "voice" / "dictate.json"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DEFAULT_HOTKEY = "Ctrl+Alt+D"
LISTEN = ROOT / "out" / "voice" / "_hallgat.json"   # a „Figyelj"-hallgatás kapcsolója (a diktáló és a tálca is olvassa)


def _listening() -> bool:
    try:
        return bool(json.loads(LISTEN.read_text("utf-8")).get("on", True))
    except (OSError, ValueError, AttributeError):
        return True


def _cfg() -> dict:
    try:
        return json.loads((ROOT / "config.json").read_text("utf-8")).get("voice", {})
    except (OSError, ValueError):
        return {}


def _engine(args: list[str], timeout: float) -> dict:
    try:
        r = subprocess.run([sys.executable, str(HERE / "engine.py"), *args], capture_output=True, timeout=timeout,
                           creationflags=NOWIN, cwd=str(HERE))
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "időtúllépés"}
    try:
        return json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"ok": False, "reason": "a motor nem válaszolt", "stderr": r.stderr.decode("utf-8", "replace")[-300:]}


def _state() -> dict:
    try:
        return json.loads(STATE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _alive(pid: int) -> bool:
    if not pid:
        return False
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, int(pid))
    if not h:
        return False
    code = ctypes.c_ulong()
    k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    return code.value == 259


def _start(hotkey: str) -> dict:
    st = _state()
    if _alive(st.get("pid", 0)) and st.get("state") not in ("hiba", "leállt"):
        return {"ok": True, "running": True, "hotkey": st.get("hotkey"), "wake": st.get("wake"), "already": True}
    cfg = _cfg()
    wake = str(cfg.get("wake", "figyelj")).strip() or "-"
    backend = cfg.get("backend", "claude") if cfg.get("backend") in ("claude", "local") else "claude"
    exe = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen([str(exe if exe.exists() else sys.executable), str(HERE / "dictate.py"), hotkey, str(os.getpid()),
                      wake, backend], cwd=str(HERE), creationflags=NOWIN, close_fds=True)
    for _ in range(30):
        time.sleep(0.2)
        st = _state()
        if st.get("hotkey") == hotkey and _alive(st.get("pid", 0)) and st.get("state") == "fut":
            return {"ok": True, "running": True, "hotkey": hotkey}
        if st.get("state") == "hiba" and time.time() - st.get("ts", 0) < 10:
            return {"ok": False, "reason": st.get("reason", "nem indult")}
    return {"ok": False, "reason": "a diktáló nem jelentkezett", "log": str(ROOT / "out" / "voice" / "dictate.log")}


def _stop_daemon() -> bool:
    st = _state()
    pid = st.get("pid", 0)
    if not _alive(pid):
        return False
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, creationflags=NOWIN)
    st.update(state="leállt", reason="kikapcsolva")
    STATE.write_text(json.dumps(st, ensure_ascii=False), "utf-8")
    return True


@tool
def voice_listen(max_s: int = 30, silence_s: float = 1.5, wait_s: int = 8) -> dict:
    """Mikrofonról felvesz (csippanás után beszélj; csendig), és visszaadja a magyar leiratot."""
    max_s = max(2, min(int(max_s), 90))
    return _engine(["listen", str(max_s), str(max(0.5, min(float(silence_s), 5))), str(max(2, min(int(wait_s), 30)))],
                   timeout=max_s + 60)


@tool
def voice_transcribe(path: str, lang: str = "hu") -> dict:
    """Rövid hangfájl (≤ 10 perc; wav/mp3/m4a/ogg/videó) leirata azonnal; hosszabbhoz media_transcribe."""
    p = Path(path).expanduser()
    if not p.is_file():
        return {"ok": False, "reason": f"nincs ilyen fájl: {path}"}
    return _engine(["file", str(p.resolve()), lang or "hu"], timeout=110)


@tool
def voice_dictation(action: str = "status", hotkey: str = "") -> dict:
    """Diktálás (Ctrl+Alt+D) és „Figyelj”-hallgatás: action=status|start|stop|wake_on|wake_off (Ctrl+Alt+F is vált)."""
    action = (action or "status").lower()
    if action == "start":
        return _start(hotkey or _cfg().get("hotkey", DEFAULT_HOTKEY))
    if action == "stop":
        return {"ok": True, "stopped": _stop_daemon()}
    if action in ("wake_on", "wake_off"):
        LISTEN.parent.mkdir(parents=True, exist_ok=True)
        LISTEN.write_text(json.dumps({"on": action == "wake_on"}), "utf-8")
        return {"ok": True, "listening": action == "wake_on"}
    if action != "status":
        return {"ok": False, "reason": "action: status, start, stop, wake_on vagy wake_off"}
    st = _state()
    running = _alive(st.get("pid", 0)) and st.get("state") not in ("hiba", "leállt")
    out = {"ok": True, "running": running, "hotkey": st.get("hotkey") or _cfg().get("hotkey", DEFAULT_HOTKEY),
           "listening": _listening()}
    for k in ("state", "wake", "backend", "last", "last_q", "last_a", "reason", "model", "wake_error"):
        if st.get(k):
            out[k] = st[k][:300] if isinstance(st[k], str) else st[k]
    return out


ENROLL = ROOT / "out" / "voice" / "enroll"
PAUSE = ROOT / "out" / "voice" / "_szunet.json"
TRAIN_LOG = ROOT / "out" / "voice" / "train.log"


def _say_sync(text: str) -> None:
    import importlib.util
    import winsound
    spec = importlib.util.spec_from_file_location("tts_plugin", ROOT / "plugins" / "tts" / "plugin.py")
    tts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tts)
    res, err = tts._render(text, "", 1.05, 1.0, 1.0)
    if not err:
        p = ROOT / "out" / "voice" / "_utasitas.wav"
        p.write_bytes(res[0])
        winsound.PlaySound(str(p), winsound.SND_FILENAME)


def _train_running() -> bool:
    st = _state()
    return _alive(st.get("train_pid", 0))


@tool
def voice_enroll(action: str = "record", count: int = 10, word: str = "") -> dict:
    """Saját ébresztőszó-minták: action=record (count-szor mondd a csippanás után) → háttérben újratanít; status."""
    import wave
    sys.path.insert(0, str(HERE))
    word = (word or _cfg().get("wake", "figyelj")).strip().lower()
    have = sorted(ENROLL.glob("*.wav")) if ENROLL.is_dir() else []
    import wake_train
    model = wake_train.model_path(word)
    if action == "status":
        out = {"ok": True, "word": word, "samples": len(have), "training": _train_running(),
               "engine": "gyors" if _state().get("engine") == "gyors" else "whisper"}
        if model.exists():
            import wake_train
            out["model"] = wake_train.Detector(model).meta
        if TRAIN_LOG.exists():
            out["train_log"] = TRAIN_LOG.read_text("utf-8", errors="replace")[-400:]
        return out
    if action != "record":
        return {"ok": False, "reason": "action: record vagy status"}
    if _train_running():
        return {"ok": False, "reason": "épp tanul a modell – várd meg (voice_enroll status)"}
    import engine
    import numpy as np
    import winsound
    count = max(3, min(int(count), 15))
    PAUSE.parent.mkdir(parents=True, exist_ok=True)
    PAUSE.write_text(json.dumps({"until": time.time() + 130}), "utf-8")
    ENROLL.mkdir(parents=True, exist_ok=True)
    from faster_whisper import WhisperModel
    checker = WhisperModel("base", device="cpu", compute_type="int8")  # minden mintát azonnal ellenőriz
    saved, skipped, n0, heard_bad = 0, 0, len(have), []
    try:
        _say_sync(f"Most {count}-szer mondd ki, hogy {word}, mindig közvetlenül a csippanás után, természetes hangon.")
        tries, t_start = 0, time.time()
        while saved < count and tries < count * 2 and time.time() - t_start < 95:
            tries += 1
            winsound.Beep(1046, 90)
            audio, _ = engine.record(max_s=3, silence_s=0.7, wait_s=4)
            if audio is None:
                skipped += 1
                _say_sync("Nem hallottam. Még egyszer.")
                continue
            e = np.sqrt(np.convolve(audio * audio, np.ones(160) / 160, "same"))
            idx = np.where(e > max(e.max() * 0.05, 1e-4))[0]
            clip = audio[max(0, idx[0] - 1600): idx[-1] + 1600] if len(idx) else audio
            segs, _ = checker.transcribe(clip, language="hu", beam_size=1, vad_filter=False,
                                         condition_on_previous_text=False, hotwords=word.capitalize())
            heard = " ".join(s.text for s in segs).strip()
            ok = engine.wake_split(heard, word)[0]
            if not 0.25 <= len(clip) / engine.RATE <= 2.5 or float(np.abs(clip).max()) < 0.05 or not ok:
                skipped += 1
                heard_bad.append(heard[:40])
                _say_sync("Ezt nem értettem tisztán. Még egyszer.")
                continue
            with wave.open(str(ENROLL / f"{n0 + saved + 1:03d}.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(engine.RATE)
                w.writeframes(np.clip(clip * 32767, -32768, 32767).astype(np.int16).tobytes())
            saved += 1
        winsound.Beep(660, 150)
        enough = n0 + saved >= 6
        _say_sync("Köszönöm. Most megtanulom a hangodat, ez két-három perc." if enough else
                  "Köszönöm. Ez még kevés, a tanításhoz legalább hat jó minta kell.")
    finally:
        PAUSE.write_text(json.dumps({"until": 0}), "utf-8")
    if not saved:
        return {"ok": False, "reason": "egy jó mintát sem sikerült felvenni", "skipped": skipped,
                "heard": heard_bad[:10]}
    if n0 + saved < 6:
        return {"ok": True, "saved": saved, "skipped": skipped, "samples_total": n0 + saved,
                "training": False, "heard_bad": heard_bad[:10], "hint": "legalább 6 jó minta kell – voice_enroll még egyszer"}
    with open(TRAIN_LOG, "w", encoding="utf-8") as log:
        pr = subprocess.Popen([sys.executable, str(HERE / "wake_train.py"), word], cwd=str(HERE), stdout=log,
                              stderr=subprocess.STDOUT, creationflags=NOWIN)
    st = _state()
    st["train_pid"] = pr.pid
    STATE.write_text(json.dumps(st, ensure_ascii=False), "utf-8")
    return {"ok": True, "saved": saved, "skipped": skipped, "samples_total": n0 + saved, "training": True,
            "heard_bad": heard_bad[:10],
            "hint": "kb. 2–3 perc; utána a diktáló magától a gyors detektorra vált (6+ saját mintánál) – "
                    "voice_enroll status / voice_dictation status"}


@tool
def voice_assistant(text: str, speak: bool = False, backend: str = "") -> dict:
    """Hangasszisztens szövegből (mint „Figyelj, …"): rövid, felolvasható magyar válasz; speak=true fel is olvassa."""
    if not (text or "").strip():
        return {"ok": False, "reason": "üres kérés"}
    sys.path.insert(0, str(HERE))
    import assistant
    b = backend or _cfg().get("backend", "claude")
    if b not in ("claude", "local"):
        return {"ok": False, "reason": "backend: claude vagy local"}
    res = assistant.ask(text.strip(), b)
    if speak:
        import importlib.util
        spec = importlib.util.spec_from_file_location("tts_plugin", ROOT / "plugins" / "tts" / "plugin.py")
        tts = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tts)
        tts.tts_speak((res.get("note", "") + " " + res["answer"]).strip(), rate=1.1)
    return {"ok": True, **res}


# A Hubbal együtt indul (a tesztfuttatóban nem): config.json "voice": {"dictation": false} kikapcsolja.
if __name__.startswith("hubplugin_") and _cfg().get("dictation", True):
    import threading

    def _autostart():
        # újratöltéskor a Hub előbb ezt importálja, és csak UTÁNA hívja a régi modul __hub_unload__-ját,
        # ami leállítja a régi diktálót – ezért várunk, különben a régit „már futónak" látnánk
        time.sleep(3)
        try:
            _start(_cfg().get("hotkey", DEFAULT_HOTKEY))
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_autostart, name="voice-autostart", daemon=True).start()


def __hub_unload__() -> None:
    if __name__.startswith("hubplugin_"):  # a tesztfuttató is hívja – a futó diktálót az ne állítsa le
        _stop_daemon()
