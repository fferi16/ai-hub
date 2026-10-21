"""Diktálás gyorsbillentyűvel + hangasszisztens ébresztőszóra (a Hub voice pluginja indítja, háttérben fut).

    pythonw dictate.py <gyorsbillentyű, pl. Ctrl+Alt+D> <hub-pid> [ébresztőszó vagy -] [claude|local]

Diktálás: megnyomod → csippan, beszélsz → elhallgatsz (vagy újra megnyomod) → a leirat a kurzor helyére
kerül (vágólap + Ctrl+V; a szöveg a vágólapon is marad). Közben a lebegő folyamatjelző mutatja az állapotot.
Asszisztens: „Figyelj, …kérés…" (vagy „Figyelj" → csippanás → kérés) → a válasz hangosan szól; ha szól,
a gyorsbillentyű elhallgattatja. Az ébresztőszót egy kis, processzoron futó modell (whisper base) keresi,
csak akkor, ha beszéd hallatszik; a hang nem hagyja el a gépet (a kérés szövege megy a háttérmodellhez).
A modell az első diktáláskor töltődik be, és 10 perc tétlenség után kiürül (a GPU-memória felszabadul).
Ha a Hub leáll, ez is kilép. Egy példány futhat (névvel ellátott mutex).
"""
import ctypes
import ctypes.wintypes as wt
import gc
import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT), str(HERE)]
import engine  # noqa: E402
import hud  # noqa: E402  (a Jarvis-ablak eseményei)
import quick  # noqa: E402  (gyors út: gyakori parancsok a Claude nélkül)

try:
    from hubkit import progress
except Exception:  # noqa: BLE001
    progress = None

STATE = ROOT / "out" / "voice" / "dictate.json"
LOG = ROOT / "out" / "voice" / "dictate.log"
IDLE_UNLOAD_S = 600


def vcfg() -> dict:
    """A config.json "voice" szakasza (minden indításkor frissen olvasva)."""
    try:
        return json.loads((ROOT / "config.json").read_text("utf-8")).get("voice", {})
    except (OSError, ValueError):
        return {}


KEEP_LOADED = bool(vcfg().get("keep_loaded", True))  # a nagy modell végig a GPU-n (~1,5 GB) → nincs 8 mp-es betöltés
WM_HOTKEY, WM_APP = 0x0312, 0x8000
MODS = {"ctrl": 0x2, "alt": 0x1, "shift": 0x4, "win": 0x8}
VK = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "pause": 0x13, "insert": 0x2D, "home": 0x24, "end": 0x23,
      **{f"f{i}": 0x6F + i for i in range(1, 13)}}
user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32

_model, _last_use, _busy = None, 0.0, threading.Lock()
_stop = threading.Event()
_recording = False
_speaking_until = 0.0
_model_lock = threading.Lock()


def big_model(quiet: bool = False):
    """A pontos (large-v3) modell; az első használatkor töltődik be (vagy induláskor, ha keep_loaded)."""
    global _model, _last_use
    with _model_lock:
        if _model is None:
            if not quiet:
                show("a beszédmodell betöltése (első alkalommal ~10 mp)…")
            _model = engine.load_model()[0]
        _last_use = time.time()
        return _model


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def state(**kw) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        cur = json.loads(STATE.read_text("utf-8"))
    except (OSError, ValueError):
        cur = {}
    cur.update(kw, ts=time.time())
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False), "utf-8")
    os.replace(tmp, STATE)


def show(detail: str, st: str = "running", keep: int = 8) -> None:
    if progress:
        try:
            progress("voice-dictate", "Diktálás", None, detail, st, keep_s=keep)
        except Exception:  # noqa: BLE001
            pass


def parse_hotkey(s: str) -> tuple[int, int]:
    mods, vk = 0x4000, None  # MOD_NOREPEAT
    for part in s.lower().replace(" ", "").split("+"):
        if part in MODS:
            mods |= MODS[part]
        elif part in VK:
            vk = VK[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        else:
            raise ValueError(f"ismeretlen billentyű: {part}")
    if vk is None or mods == 0x4000:
        raise ValueError("kell legalább egy módosító (Ctrl/Alt/Shift/Win) és egy billentyű")
    return mods, vk


# ---- beillesztés a kurzor helyére -----------------------------------------------

def set_clipboard(text: str) -> None:
    import win32clipboard
    for _ in range(10):  # más program épp foghatja a vágólapot
        try:
            win32clipboard.OpenClipboard()
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    else:
        raise RuntimeError("a vágólap foglalt")
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def paste() -> None:
    VK_CONTROL, VK_V, UP = 0x11, 0x56, 0x0002
    for vk in (0x12, 0x10, 0x5B):  # Alt, Shift, Win felengedve (a gyorsbillentyűből ott maradhatnak)
        user32.keybd_event(vk, 0, UP, 0)
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    user32.keybd_event(VK_V, 0, UP, 0)
    user32.keybd_event(VK_CONTROL, 0, UP, 0)


# ---- egy diktálás ------------------------------------------------------------------

def dictate_once() -> None:
    global _model, _last_use, _recording
    if not _busy.acquire(blocking=False):
        return
    import winsound
    try:
        _stop.clear()
        _recording = True
        winsound.Beep(880, 120)
        show("beszélj… (csend vagy a gyorsbillentyű = kész)")
        state(state="felvétel")
        audio, info = engine.record(max_s=120, silence_s=2.0, wait_s=8, stop=_stop)
        _recording = False
        winsound.Beep(660, 120)
        if audio is None:
            show(info.get("reason", "nem hallottam beszédet"), "error", 5)
            state(state="kész", last="", reason=info.get("reason"))
            return
        model = big_model()
        show("leirat…")
        t = time.time()
        text = engine.transcribe(model, audio)
        _last_use = time.time()
        if not text:
            show("nem értettem – próbáld újra", "error", 5)
            state(state="kész", last="")
            return
        set_clipboard(text)
        time.sleep(0.05)
        paste()
        show(text[:70] + ("…" if len(text) > 70 else ""), "done", 6)
        state(state="kész", last=text, seconds=info.get("seconds"), stt_s=round(time.time() - t, 1))
        log(f"{info.get('seconds')} s → {len(text)} karakter")
    except Exception as e:  # noqa: BLE001
        _recording = False
        show(f"hiba: {e}"[:80], "error", 10)
        state(state="hiba", reason=f"{type(e).__name__}: {e}"[:200])
        log(traceback.format_exc())
    finally:
        _busy.release()


# ---- ébresztőszó + hangasszisztens -----------------------------------------------------

from engine import _norm, wake_split  # noqa: E402  (a szóillesztő a közös motorban van)


class Speaker:
    """Felolvasási sor: a válasz mondatonként érkezik, és mondatonként szól (az első mondat már a válasz
    vége előtt). Amíg van mit mondani, az ébresztőszó-figyelés szünetel; stop() mindent elhallgattat."""

    def __init__(self):
        import queue
        self.q = queue.Queue()
        self._stop = threading.Event()
        self._tts = None
        self._n = 0
        threading.Thread(target=self._run, name="speaker", daemon=True).start()

    def _module(self):
        if self._tts is None:
            import importlib.util
            spec = importlib.util.spec_from_file_location("tts_plugin", ROOT / "plugins" / "tts" / "plugin.py")
            self._tts = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self._tts)
        return self._tts

    def say(self, text: str) -> None:
        self._stop.clear()
        self.q.put(text)

    def active(self) -> bool:
        return not self.q.empty() or time.time() < _speaking_until

    def stop(self) -> bool:
        global _speaking_until
        was = self.active()
        self._stop.set()
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except Exception:  # noqa: BLE001
                break
        import winsound
        winsound.PlaySound(None, 0)
        _speaking_until = 0.0
        hud.send("hush")
        return was

    def _run(self) -> None:
        global _speaking_until
        import winsound
        while True:
            text = self.q.get()
            if self._stop.is_set():
                continue
            try:
                tts = self._module()
                res, err = tts._render(tts.clean(text), "", 1.1, 1.0, 1.0)
                if err or self._stop.is_set():
                    continue
                wav = res[0]
                self._n += 1  # két fájl váltakozva: a lejátszás alatt lévőt nem írjuk felül
                path = ROOT / "out" / "voice" / f"valasz{self._n % 2}.wav"
                path.write_bytes(wav)
                dur = tts._duration(wav)
                _speaking_until = time.time() + dur + 0.6
                try:
                    hud.send("say", text=text[:300], env=hud.envelope(wav), fps=30)
                except Exception:  # noqa: BLE001 – a kijelzés nem állíthatja meg a felolvasást
                    pass
                winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
                end = time.time() + dur
                while time.time() < end and not self._stop.is_set():
                    time.sleep(0.05)
            except Exception:  # noqa: BLE001
                log(traceback.format_exc())


SPEAKER = Speaker()


def speak(text: str) -> None:
    SPEAKER.say(text)


_ext = {"checked": 0.0, "until": 0.0}


def others_speaking() -> bool:
    """A Hub tts_speak-je épp szól-e (out/tts/_szol.json) – fél másodpercenként nézzük meg."""
    now = time.time()
    if now - _ext["checked"] > 0.5:
        _ext["checked"] = now
        try:
            _ext["until"] = float(json.loads((ROOT / "out" / "tts" / "_szol.json").read_text("utf-8"))["until"])
        except (OSError, ValueError, KeyError, TypeError):
            _ext["until"] = 0.0
    return now < _ext["until"] + 0.6


def stop_speaking() -> bool:
    return SPEAKER.stop() if SPEAKER.active() else False


# a gyakori parancsszavak és nevek súgása a kérés leiratához (gépi hangon pl. „Alnicót" → „alítót" nélküle)
COMMAND_HOTWORDS = "Nyisd meg, Zárd be, Kapcsold, böngésző, Alnico, Hub, Claude, Excel, PDF, rajz"


def beep_during(freq: int) -> None:
    """Csippanás külön szálon: a felvétel közben indulhat, nem utána."""
    import winsound
    threading.Thread(target=winsound.Beep, args=(freq, 90), daemon=True).start()


def transcribe_command(audio) -> str:
    """A kérés leirata a pontos modellel, a parancsszavak súgásával; a hang megmarad hibakereséshez."""
    try:
        import wave
        import numpy as np
        with wave.open(str(ROOT / "out" / "voice" / "utolso_keres.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(engine.RATE)
            w.writeframes(np.clip(audio * 32767, -32768, 32767).astype(np.int16).tobytes())
    except Exception:  # noqa: BLE001
        pass
    text = engine.transcribe(big_model(), audio, hotwords=vcfg().get("command_hotwords", COMMAND_HOTWORDS))
    log(f"kérés leirata: {text!r}")
    if HALLUCINATION.search(text):
        hud.send("state", state="idle")
        log("whisper-hallucináció (csendből/zajból), eldobva")
        return ""
    if text.strip():
        hud.send("heard", text=text)
    return text


# a whisper csendre/zajra a feliratos videók záró mondatait „hallja" – ezek sosem kérések
HALLUCINATION = re.compile(r"megnézté(tek|d)|köszön\w*,? hogy (meg|vel|követ|figyel)|feliratoz|iratkozz(atok)? fel|"
                           r"videóm|csatornám|köszönöm a figyelm|amara\.org", re.I)
# párbeszéd közben ezek a beszélgetés végét jelentik (nem mennek a Claude-hoz); ékezet nélkül, kisbetűvel
END_WORDS = {"eleg", "vege", "kesz", "megse", "semmi", "hagyd", "hagyjuk", "allj", "csend", "fejezd be", "zard be",
             "eleg ennyi", "ennyi", "ennyi volt", "koszonom eleg"}
# bármikor: a „Figyelj"-hallgatás kikapcsolása hanggal (visszakapcsolni a tálcán / Ctrl+Alt+F-fel lehet)
MUTE_RE = re.compile(r"^(ne (figyelj|hallgass)|hallgass el|kapcsold ki a figyel)", re.I)


def handle_wake(segment, rest_hint: str, backend: str, word: str, strong: bool = False) -> None:
    if not _busy.acquire(blocking=False):
        return
    import winsound
    try:
        import assistant
        # a „Figyelj" hangerejéből: a kérés felvételénél ennél jóval halkabb hang (tévé, rádió) csendnek számít
        floor = engine.voice_level(segment) * 0.35
        globals()["_last_floor"] = floor
        log(f"hangerő: {floor / 0.35:.4f} → küszöb {floor:.4f}")
        hud.send("state", state="listen")
        if strong and len(rest_hint.split()) < 2:
            # BIZTOS ébredés, kérés nélkül („Figyelj."): azonnal csippan és veszi a kérést, a nagy modell
            # közben párhuzamosan töltődik – így a betöltés ideje alatt mondott kérés sem vész el
            loader = threading.Thread(target=big_model, daemon=True)
            loader.start()
            # a felvétel MÁR a csippanás alatt megy: aki rögtön mondja a kérést, annak sem vész el az eleje
            beep_during(1046)
            show("mondd… (csend = kész)")
            audio, _ = engine.record(max_s=15, silence_s=1.5, wait_s=7, floor=floor)
            if audio is None:
                winsound.Beep(440, 150)
                show("nem hallottam kérést", "error", 4)
                log("ébredés után nem jött kérés")
                return
            show("leirat…")
            loader.join()
            command = transcribe_command(audio)
        else:
            if strong:
                winsound.Beep(1046, 90)
                show("értem…")
            # a teljes mondatot a pontos modell írja le; bizonytalan ébredésnél ez MEG IS ERŐSÍTI a szót
            full = engine.transcribe(big_model(), segment, hotwords=word.capitalize())
            ok, command = wake_split(full, word)
            if not ok:
                # a nagy modell a magában álló szót gyakran elhallja („Fideje"); a biztos ébredés ettől még áll
                if not strong:
                    log(f"téves ébredés elvetve: {full!r}")
                    show("nem hallottam az ébresztőszót", "done", 2)
                    return
                log(f"a nagy modell mást hallott ({full!r}), a kis modellé az elsőbbség")
                command = rest_hint
            elif not strong:
                winsound.Beep(1046, 90)
            if len(command.split()) < 2:
                beep_during(1318)
                show("mondd… (csend = kész)")
                audio, _ = engine.record(max_s=15, silence_s=1.5, wait_s=7, floor=floor)
                if audio is None:
                    winsound.Beep(440, 150)
                    show("nem hallottam kérést", "error", 4)
                    return
                command = transcribe_command(audio)
        answer(command, backend)
    except Exception as e:  # noqa: BLE001
        show(f"hiba: {e}"[:80], "error", 10)
        hud.send("state", state="error")
        state(state="hiba", reason=f"{type(e).__name__}: {e}"[:200])
        log(traceback.format_exc())
    finally:
        _busy.release()


MAX_TURNS = 5        # párbeszédben ennyi kör mehet egymás után ébresztőszó nélkül
_last_floor = 0.0    # a beszélő hangerejéből számolt küszöb (a tévé ne tartsa nyitva a felvételt)


_sysp = None


def _system():
    """A system plugin modulja (a célpont-feloldáshoz; a végrehajtás a Hubon át megy)."""
    global _sysp
    if _sysp is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("system_plugin_voice", ROOT / "plugins" / "system" / "plugin.py")
        _sysp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_sysp)
    return _sysp


def _silent_say(text: str) -> None:
    """Néma válasz (gépelt kérésre): csak kiírva, a gömb a szöveg ritmusára „beszél"."""
    hud.send("say", text=text[:300], env=hud.text_envelope(text), fps=30)


# ---- magától szól (mint Jarvis): gépállapot és kész munkák figyelése ---------------------------

def quiet_now() -> bool:
    """Csendes órák ("voice": {"quiet_hours": "22-7"}) vagy kikapcsolt figyelés: csak a gömb jelez, hang nélkül."""
    if not listening():
        return True
    try:
        a, b = (int(x) for x in str(vcfg().get("quiet_hours", "22-7")).split("-"))
    except ValueError:
        return False
    h = time.localtime().tm_hour
    return (a <= h or h < b) if a > b else (a <= h < b)


def notify(text: str) -> None:
    log(f"jelzés: {text}")
    if quiet_now() or _busy.locked() or SPEAKER.active():
        hud.send("notify", text=text, env=hud.text_envelope(text), fps=30)
    else:
        hud.send("notify", text=text)
        speak(text)


def watcher() -> None:
    """30 mp-enként: akku, lemez, memória, GPU-hőfok, internet, és a 2 percnél hosszabb munkák vége (out/progress)."""
    import psutil
    seen, last, ram_hi, online = set(), {}, 0, True
    start = time.time()

    def once(key: str, every_s: float) -> bool:
        if time.time() - last.get(key, 0) < every_s:
            return False
        last[key] = time.time()
        return True

    batt_level = 100
    while True:
        time.sleep(30)
        try:
            b = psutil.sensors_battery()
            if b is not None:
                if b.power_plugged:
                    batt_level = 100
                else:
                    for lim in (20, 10, 5):
                        if b.percent <= lim < batt_level:
                            batt_level = lim
                            notify(f"Feri, az akkumulátor {round(b.percent)} százalékon van, nincs töltőn.")
                            break
            if psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\").free < 10 * 2**30 and once("disk", 6 * 3600):
                notify("Kevés a hely a C meghajtón: tíz gigabájt alatt.")
            ram_hi = ram_hi + 1 if psutil.virtual_memory().percent >= 92 else 0
            if ram_hi >= 2 and once("ram", 1800):
                notify("A memória majdnem tele van.")
            if once("gpu", 60):
                g = _gpu_temp()
                if g is not None and g >= 85 and once("gpu_hot", 900):
                    notify(f"Melegszik a videókártya: {g} fok.")
            now_online = _online()
            if now_online != online:
                online = now_online
                notify("Megszakadt az internetkapcsolat." if not online else "Az internet újra elérhető.")
            for f in (ROOT / "out" / "progress").glob("*.json"):
                if f.name in seen:
                    continue
                try:
                    rec = json.loads(f.read_text("utf-8"))
                except (OSError, ValueError):
                    continue
                if (rec.get("state") in ("done", "error") and rec.get("updated", 0) > max(start, time.time() - 90)
                        and rec.get("updated", 0) - rec.get("started", 0) >= 120 and not f.name.startswith("voice-")):
                    seen.add(f.name)
                    title = str(rec.get("title", "egy munka"))[:60]
                    notify(f"Elkészült: {title}." if rec["state"] == "done" else f"Hibával állt le: {title}.")
        except Exception:  # noqa: BLE001 – a figyelő nem állhat le egy hiba miatt
            log("figyelő hiba: " + traceback.format_exc())


def _gpu_temp() -> int | None:
    import shutil
    import subprocess
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"], capture_output=True,
                           text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return int(r.stdout.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _online() -> bool:
    import socket
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            with socket.create_connection((host, 53), timeout=1.5):
                return True
        except OSError:
            continue
    return False


def listen_now(backend: str) -> None:
    """Kattintás a gömbre: mint a „Figyelj" – csippanás, a kérés felvétele, válasz."""
    if not _busy.acquire(blocking=False):
        return
    import winsound
    try:
        beep_during(1046)
        show("mondd… (csend = kész)")
        hud.send("state", state="listen")
        audio, _ = engine.record(max_s=15, silence_s=1.5, wait_s=7, floor=_last_floor)
        if audio is None:
            winsound.Beep(440, 150)
            hud.send("state", state="idle")
            log("kattintás után nem jött kérés")
            return
        answer(transcribe_command(audio), backend)
    except Exception as e:  # noqa: BLE001
        show(f"hiba: {e}"[:80], "error", 10)
        hud.send("state", state="error")
        log(traceback.format_exc())
    finally:
        _busy.release()


def ask_typed(text: str, backend: str, voice: bool) -> None:
    """Gépelt kérés az ablakból: ugyanaz, mint a kimondott, alapból hang nélkül."""
    if not _busy.acquire(blocking=False):
        hud.send("result", ok=False, text="épp dolgozom egy másik kérésen")
        return
    try:
        log(f"gépelt kérés: {text!r}")
        hud.send("heard", text=text)
        answer(text, backend, silent=not voice)
    except Exception as e:  # noqa: BLE001
        show(f"hiba: {e}"[:80], "error", 10)
        hud.send("state", state="error")
        log(traceback.format_exc())
    finally:
        _busy.release()


def command_loop(backend: str) -> None:
    """Az ablak parancsai UDP-n (127.0.0.1:hud.CMD_PORT)."""
    import socket
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sk.bind(("127.0.0.1", hud.CMD_PORT))
    except OSError:
        log(f"a {hud.CMD_PORT} port foglalt – az ablak parancsai nem érkeznek meg")
        return
    while True:
        data, _ = sk.recvfrom(16384)
        try:
            m = json.loads(data.decode("utf-8"))
        except ValueError:
            continue
        if m.get("cmd") == "listen":
            threading.Thread(target=listen_now, args=(backend,), daemon=True).start()
        elif m.get("cmd") == "ask" and str(m.get("text", "")).strip():
            threading.Thread(target=ask_typed, args=(str(m["text"]).strip()[:2000], backend, bool(m.get("voice"))),
                             daemon=True).start()


def answer(command: str, backend: str, turn: int = 1, silent: bool = False) -> None:
    """A leírt kérés → asszisztens → a válasz mondatonként szól (az első mondat már a válasz vége előtt).
    PÁRBESZÉD: ha a válasz kérdéssel végződik, felolvasás után magától meghallgatja a választ (csippanás,
    nem kell újra „Figyelj"); csend → vége. Ha a gyorsbillentyűvel elhallgattatták, nem kérdez tovább.
    silent: gépelt kérés – nincs felolvasás, csippanás és visszahallgatás; a válasz a Jarvis-ablakban látszik."""
    global _recording
    import assistant
    out = _silent_say if silent else speak
    if not command.strip():
        show("nem értettem a kérést", "error", 4)
        log("a kérés leirata üres (csend vagy zaj)")
        hud.send("state", state="idle")
        return
    import winsound
    plain = " ".join(re.findall(r"\w+", _norm(command)))
    if MUTE_RE.match(plain):
        set_listening(False)            # a „wake" szál csippan és elengedi a mikrofont
        log(f"figyelés kikapcsolva hanggal: {command!r}")
        return
    if turn > 1 and plain in END_WORDS:
        hud.send("state", state="idle")
        winsound.Beep(523, 120)
        show("párbeszéd vége", "done", 3)
        log(f"párbeszéd vége ({command!r})")
        return
    show("gondolkodom: " + command[:60])
    hud.send("state", state="think")
    state(state="asszisztens", last_q=command)
    res = None
    if vcfg().get("quick", True):        # gyakori parancsok helyben, a Claude nélkül
        try:
            t0 = time.time()
            q = quick.handle(command, _system())
            if q is not None:
                out(q)
                res = {"answer": q, "backend": "helyi", "seconds": round(time.time() - t0, 1), "first_s": None}
        except Exception:  # noqa: BLE001 – hiba esetén a Claude intézi
            log("gyors út hiba: " + traceback.format_exc())
    if res is None:
        res = assistant.ask(command, backend, on_sentence=out)
    show(res["answer"][:70] + ("…" if len(res["answer"]) > 70 else ""), "done", 12)
    state(state="kész", last_q=command, last_a=res["answer"], backend=res["backend"], ask_s=res["seconds"],
          first_s=res.get("first_s"))
    log(f"asszisztens ({res['backend']}, első mondat {res.get('first_s')} s, teljes {res['seconds']} s): "
        f"{command[:80]}")
    if silent:
        hud.send("state", state="done")
        return
    # megvárjuk, amíg a válasz elhangzik
    t0 = time.time()
    while SPEAKER.active() and time.time() - t0 < 180:
        time.sleep(0.1)
    if SPEAKER._stop.is_set():          # Ctrl+Alt+D-vel elhallgattatta: nem erőltetjük a párbeszédet
        log("párbeszéd vége (elhallgattatva)")
        return
    if turn >= MAX_TURNS or not res["answer"].rstrip().endswith("?"):
        winsound.Beep(523, 120)         # vége: újra a „Figyelj"-re vár
        hud.send("state", state="idle")
        return
    # visszakérdezett: hallgatjuk a választ
    time.sleep(0.3)                     # a saját hangja ne kerüljön a felvétel elejére
    beep_during(1318)
    show("válaszolj… (csend = vége)")
    hud.send("state", state="listen")
    _stop.clear()
    _recording = True
    try:
        audio, _ = engine.record(max_s=15, silence_s=1.5, wait_s=6, stop=_stop, floor=_last_floor)
    finally:
        _recording = False
    if audio is None:
        winsound.Beep(440, 150)
        show("párbeszéd vége", "done", 3)
        log("párbeszéd vége (nem jött válasz)")
        hud.send("state", state="idle")
        return
    answer(transcribe_command(audio), backend, turn + 1)


# ---- gyors ébresztőszó (openWakeWord-beágyazás + saját modell, 80 ms-onként) -------------------

PAUSE = ROOT / "out" / "voice" / "_szunet.json"   # a hangminta-felvétel idejére a figyelés szünetel


def paused() -> bool:
    try:
        return time.time() < float(json.loads(PAUSE.read_text("utf-8"))["until"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


# a „Figyelj"-hallgatás kapcsolója (tálcamenü, Ctrl+Alt+F, voice_dictation wake_on/wake_off); újraindítás után is megmarad
LISTEN = ROOT / "out" / "voice" / "_hallgat.json"
_listen = {"checked": 0.0, "on": True}


def listening() -> bool:
    now = time.time()
    if now - _listen["checked"] > 0.5:
        _listen["checked"] = now
        try:
            _listen["on"] = bool(json.loads(LISTEN.read_text("utf-8")).get("on", True))
        except (OSError, ValueError, AttributeError):
            _listen["on"] = True
    return _listen["on"]


def set_listening(on: bool) -> None:
    LISTEN.parent.mkdir(parents=True, exist_ok=True)
    LISTEN.write_text(json.dumps({"on": bool(on)}), "utf-8")
    _listen["checked"] = 0.0


def fast_model_path(word: str) -> Path:
    import wake_train
    return wake_train.model_path(word)


def use_fast(word: str) -> bool:
    """auto: a gyors detektor csak akkor, ha a modell már a felhasználó hangját is ismeri (≥ 6 saját minta)."""
    mode = vcfg().get("wake_engine", "auto")
    p = fast_model_path(word)
    if mode == "whisper" or not p.exists():
        return False
    if mode == "fast":
        return True
    try:
        import wake_train
        return wake_train.Detector(p).meta.get("user_samples", 0) >= 6
    except Exception:  # noqa: BLE001
        return False


def fast_loop(word: str, backend: str) -> None:
    """80 ms-onként pontszám a saját modellel; két egymást követő keret a küszöb fölött → azonnali csippanás,
    és UGYANABBÓL a hangfolyamból veszi a kérést (nincs kimaradó rész). A modellfájl cseréjét (újratanítás)
    magától észreveszi."""
    import numpy as np
    import sounddevice as sd
    import winsound
    from openwakeword.utils import AudioFeatures

    import wake_train
    path = fast_model_path(word)
    det, mtime = wake_train.Detector(path), path.stat().st_mtime
    thr = float(vcfg().get("wake_threshold", max(det.threshold, 0.85)))
    feat = AudioFeatures(inference_framework="onnx")
    dev = sd.query_devices(kind="input")
    sr = int(dev["default_samplerate"])
    block = int(sr * 0.08)
    ring = np.zeros(engine.RATE * 3, np.float32)        # az utolsó 3 s (16 kHz) – a hangerőhöz
    prev, last_fire, last_check = 0.0, 0.0, time.time()
    state(wake=word, backend=backend, engine="gyors")
    log(f"gyors ébresztőszó-figyelés: „{word}” (küszöb {thr:.2f}, saját minták: {det.meta.get('user_samples', 0)})")

    def chunk(st):
        x = st.read(block)[0][:, 0]
        y = np.interp(np.linspace(0, len(x) - 1, 1280), np.arange(len(x)), x).astype(np.float32)
        return y

    with sd.InputStream(samplerate=sr, channels=1, dtype="float32", blocksize=block) as st:
        while True:
            if not listening() and not _busy.locked():
                return                                   # kikapcsolták: a mikrofont is elengedjük
            y = chunk(st)
            ring = np.concatenate([ring[1280:], y])
            feat(np.clip(y * 32767, -32768, 32767).astype(np.int16))
            now = time.time()
            if now - last_check > 10:                    # újratanított modell? → betöltjük
                last_check = now
                if path.exists() and path.stat().st_mtime != mtime:
                    det, mtime = wake_train.Detector(path), path.stat().st_mtime
                    log("a gyors modell újratöltve")
            if _busy.locked() or SPEAKER.active() or others_speaking() or paused():
                prev = 0.0
                continue
            sc = det.score(feat.get_features(16))
            fire = sc >= thr and prev >= thr and now - last_fire > 2.0
            prev = sc
            if not fire:
                continue
            last_fire = now
            if not _busy.acquire(blocking=False):
                continue
            try:
                threading.Thread(target=winsound.Beep, args=(1046, 90), daemon=True).start()
                show("mondd… (csend = kész)")
                hud.send("state", state="listen")
                floor = engine.voice_level(ring[-engine.RATE:]) * 0.35
                globals()["_last_floor"] = floor
                log(f"ébredés (gyors, pontszám {sc:.2f}, küszöb a kéréshez {floor:.4f})")
                # a kérés ugyanebből a folyamból: beszéd → 1,2 s csend vége; 6 s-ig nincs beszéd → nincs kérés
                frames, started, quiet, t0 = [], False, 0, time.time()
                noise = max(float(np.percentile(np.abs(ring), 50)) * 3, 0.008)
                while time.time() - t0 < 15:
                    y = chunk(st)
                    feat(np.clip(y * 32767, -32768, 32767).astype(np.int16))
                    frames.append(y)
                    # az első 0,32 s-ban a saját csippanása szól: felvesszük, de nem számít a beszéd kezdetének
                    # (különben a csippanás „elindítja", és 1,2 s csend után lezárul, mielőtt a kérés elhangzik)
                    loud = len(frames) > 4 and float(np.sqrt(np.mean(y * y))) > max(noise, floor)
                    started = started or loud
                    quiet = 0 if loud else quiet + 1
                    if not started and time.time() - t0 > 6:
                        break
                    if started and quiet * 0.08 >= 1.2:
                        break
                prev = 0.0
                if not started:
                    winsound.Beep(440, 150)
                    show("nem hallottam kérést", "error", 4)
                    log("ébredés után nem jött kérés")
                    hud.send("state", state="idle")
                    _busy.release()
                    continue
                audio = np.concatenate(frames[3:])     # a csippanás (első 0,24 s) ne zavarja a leiratot
            except Exception:  # noqa: BLE001
                _busy.release()
                log(traceback.format_exc())
                continue
            threading.Thread(target=_finish_fast, args=(audio, word, backend), daemon=True).start()


def _finish_fast(audio, word: str, backend: str) -> None:
    """A gyors ébredés utáni kérés leirata és megválaszolása (a _busy már a miénk)."""
    try:
        show("leirat…")
        text = transcribe_command(audio)
        ok, rest = wake_split(text, word)        # ha a szó vége még belelógott: levágjuk
        answer(rest if ok else text, backend)
    except Exception as e:  # noqa: BLE001
        show(f"hiba: {e}"[:80], "error", 10)
        hud.send("state", state="error")
        log(traceback.format_exc())
    finally:
        _busy.release()


def wake_loop(word: str, backend: str, allow_switch: bool = True) -> None:
    """Folyamatos hallgatás: beszédszakaszokra bont (energia alapján), és a rövid szakaszok elején keresi
    az ébresztőszót a kis modellel. Ha megvan, a handle_wake viszi tovább."""
    import numpy as np
    import sounddevice as sd
    from faster_whisper import WhisperModel

    small = WhisperModel("base", device="cpu", compute_type="int8")
    try:  # hibakereséshez: "voice": {"debug": true} → minden rövid beszédszakasz leirata a naplóba
        debug = bool(json.loads((ROOT / "config.json").read_text("utf-8")).get("voice", {}).get("debug"))
    except (OSError, ValueError):
        debug = False
    dev = sd.query_devices(kind="input")
    sr = int(dev["default_samplerate"])
    block = int(sr * 0.03)
    pre = int(0.3 / 0.03)                      # 0,3 mp előtét, hogy a szó eleje ne vesszen el
    noise, buf, seg, silent = 0.003, [], [], 0
    state(wake=word, backend=backend, engine="whisper")
    log(f"ébresztőszó-figyelés: „{word}”, háttér: {backend}, mikrofon: {dev['name']}")
    next_check = time.time() + 30
    with sd.InputStream(samplerate=sr, channels=1, dtype="float32", blocksize=block) as st:
        while True:
            if not listening() and not _busy.locked():
                return                                   # kikapcsolták: a mikrofont is elengedjük
            if time.time() > next_check and not seg:      # elkészült a saját hangra tanított gyors modell?
                next_check = time.time() + 30
                if allow_switch and use_fast(word):
                    log("átváltás a gyors detektorra")
                    return
            x = st.read(block)[0][:, 0].copy()
            if _busy.locked() or SPEAKER.active() or others_speaking() or paused():
                seg, silent, buf = [], 0, []
                continue
            rms = float(np.sqrt(np.mean(x * x)) + 1e-9)
            thr = max(noise * 3.0, 0.008)
            if not seg:
                buf = (buf + [x])[-pre:]
                if rms > thr:
                    seg, silent = buf + [x], 0
                else:
                    noise = 0.98 * noise + 0.02 * rms   # a háttérzaj lassan követve
                continue
            seg.append(x)
            silent = silent + 1 if rms <= thr else 0
            dur = len(seg) * 0.03
            if silent * 0.03 < 0.6 and dur < 4:
                continue
            audio = engine.resample(np.concatenate(seg), sr)
            seg, silent = [], 0
            if dur < 0.4:                          # túl rövid (koppanás)
                continue
            if dur >= 4:
                # folyamatos hang (tévé szól): nem dobjuk el – a LEGHANGOSABB 2 s-ban keressük a szót,
                # mert a közelebb álló beszélő hangosabb a háttérnél
                n, w = 480, engine.RATE * 2
                fr = np.sqrt((audio[: len(audio) // n * n].reshape(-1, n) ** 2).mean(axis=1))
                k = int(w / n)
                i = int(np.argmax(np.convolve(fr, np.ones(k), "valid"))) * n if len(fr) > k else 0
                head = audio[i:i + w]
            else:
                head = audio[: engine.RATE * 3]
            audio = head if dur >= 4 else audio
            segs, _ = small.transcribe(head, language="hu", beam_size=1, vad_filter=False,
                                       condition_on_previous_text=False, hotwords=word.capitalize())
            text = " ".join(s.text for s in segs).strip()
            ok, rest = wake_split(text, word)
            if debug:
                log(f"[debug] hallott ({dur:.1f} s, zajszint {noise:.4f}): {text!r} → {'ÉBRED' if ok else '-'}")
            if ok:
                strong = wake_split(text, word, 0.85)[0]   # a kis modell biztosan a szót hallotta
                log(f"ébredés: {text!r}" + (" (biztos)" if strong else ""))
                threading.Thread(target=handle_wake, args=(audio, rest, backend, word, strong), daemon=True).start()


def housekeeping(hub_pid: int) -> None:
    """Tétlen modell kiürítése; kilépés, ha a Hub már nem fut."""
    global _model
    while True:
        time.sleep(5)
        if (not KEEP_LOADED and _model is not None and not _busy.locked()
                and time.time() - _last_use > IDLE_UNLOAD_S):
            _model = None
            gc.collect()
            state(model="kiürítve")
        if hub_pid and not _alive(hub_pid):
            state(state="leállt", reason="a Hub leállt")
            os._exit(0)


def _alive(pid: int) -> bool:
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = wt.DWORD()
    kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    kernel32.CloseHandle(h)
    return code.value == 259  # STILL_ACTIVE


def main() -> int:
    hotkey = sys.argv[1] if len(sys.argv) > 1 else "Ctrl+Alt+D"
    hub_pid = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    wake = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "-" else ""
    backend = sys.argv[4] if len(sys.argv) > 4 else "claude"
    kernel32.CreateMutexW(None, False, "AIHUB_voice_dictate")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return 0
    try:
        mods, vk = parse_hotkey(hotkey)
    except ValueError as e:
        state(state="hiba", reason=str(e), pid=os.getpid())
        return 1
    if not user32.RegisterHotKey(None, 1, mods, vk):
        state(state="hiba", reason=f"a {hotkey} gyorsbillentyű foglalt", pid=os.getpid())
        return 1
    state(state="fut", pid=os.getpid(), hotkey=hotkey, last="", reason="", wake=wake or None)
    log(f"indult: {hotkey}")
    threading.Thread(target=housekeeping, args=(hub_pid,), daemon=True).start()
    # ELŐTÖLTÉS MINDIG, induláskor: a GPU-s whisper betöltése összeomlik, ha ugyanebben a folyamatban előtte
    # a Windows beszédszintézis (WinRT, a felolvasó) már futott – fordított sorrendben rendben van.
    # keep_loaded=false esetén a tétlen modell később kiürül (a DLL-ek betöltve maradnak, az újratöltés jó).
    def preload():
        big_model(quiet=True)
        state(model="betöltve")
        log("a beszédmodell betöltve" + (" (állandóan)" if KEEP_LOADED else ""))
    threading.Thread(target=preload, name="preload", daemon=True).start()
    if wake and vcfg().get("jarvis", True):    # a Jarvis-ablak (grafikus felület); "jarvis": false kikapcsolja
        exe = Path(sys.executable).with_name("pythonw.exe")
        import subprocess
        subprocess.Popen([str(exe if exe.exists() else sys.executable), str(HERE / "jarvis.py"), str(os.getpid())],
                         cwd=str(HERE), close_fds=True)
        threading.Thread(target=command_loop, args=(backend,), name="hud-cmd", daemon=True).start()
    if wake and vcfg().get("proactive", True):  # magától szól: "proactive": false kikapcsolja
        threading.Thread(target=watcher, name="watcher", daemon=True).start()
    if wake:
        def guarded():
            fast_failed, was = False, None
            try:
                while True:
                    on = listening()
                    if on != was:                          # kapcsoló váltott (vagy indulás): jelzés + állapot
                        if was is not None:
                            import winsound
                            for f in ((784, 1046) if on else (1046, 784)):
                                winsound.Beep(f, 90)
                            show(f"„{wake}” figyelés: {'BE' if on else 'KI'}", "done", 3)
                            log(f"„{wake}” figyelés: {'be' if on else 'ki'}kapcsolva")
                        state(listening=on)
                        hud.send("state", state="idle" if on else "off")
                        was = on
                    if not on:
                        time.sleep(0.5)
                        continue
                    if not fast_failed and use_fast(wake):
                        try:
                            fast_loop(wake, backend)
                            continue                       # csak kikapcsoláskor tér vissza
                        except Exception:  # noqa: BLE001 – a gyors detektor hibájánál a régi út marad
                            fast_failed = True
                            log("a gyors detektor leállt, marad a whisper-alapú:\n" + traceback.format_exc())
                    wake_loop(wake, backend, allow_switch=not fast_failed)  # visszatér, ha elkészült a gyors modell
            except Exception as e:  # noqa: BLE001 – a diktálás ettől még menjen
                state(wake_error=f"{type(e).__name__}: {e}"[:200])
                log(traceback.format_exc())
        threading.Thread(target=guarded, name="wake", daemon=True).start()
        wake_hotkey = str(vcfg().get("wake_hotkey", "Ctrl+Alt+F"))
        try:
            if wake_hotkey and not user32.RegisterHotKey(None, 2, *parse_hotkey(wake_hotkey)):
                log(f"a {wake_hotkey} (figyelés be/ki) gyorsbillentyű foglalt")
        except ValueError as e:
            log(f"wake_hotkey: {e}")
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY and msg.wParam == 2:
            set_listening(not listening())     # a „wake" szál jelez és engedi el / veszi fel a mikrofont
            continue
        if msg.message == WM_HOTKEY:
            if stop_speaking():
                continue               # szól a válasz: a gyorsbillentyű elhallgattatja
            if _recording:
                _stop.set()            # második lenyomás: kész
            else:
                threading.Thread(target=dictate_once, daemon=True).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
