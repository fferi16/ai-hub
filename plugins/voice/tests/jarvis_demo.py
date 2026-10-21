"""Néma bemutató a Jarvis-ablakhoz: elindítja az ablakot, és lejátszik két beszélgetést HANG NÉLKÜL.

    .venv\\Scripts\\python.exe plugins\\voice\\tests\\jarvis_demo.py [képek-mappája]

A válaszok hangját a felolvasó elkészíti, de nem szól: csak a hangerő-burkolója megy az ablaknak, így a gömb
pontosan úgy mozog, mint beszéd közben. Programot nem indít (csak a megjelenítés eseményei mennek).
Ha megadsz mappát, közben képernyőképeket ment az ablakról.
"""
import importlib.util
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
import hud  # noqa: E402

SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)   # valódi képpont-koordináták a képernyőképhez
except Exception:  # noqa: BLE001
    pass
spec = importlib.util.spec_from_file_location("tts_demo", ROOT / "plugins" / "tts" / "plugin.py")
tts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tts)


def say(text: str) -> float:
    (wav, _voice, _n), err = tts._render(tts.clean(text), "", 1.1, 1.0, 1.0)
    hud.send("say", text=text, env=hud.envelope(wav), fps=30)
    return tts._duration(wav)


def shot(name: str) -> None:
    if not SHOTS:
        return
    import ctypes
    import ctypes.wintypes as wt
    from PIL import ImageGrab
    hwnd = ctypes.windll.user32.FindWindowW(None, "Jarvis")
    r = wt.RECT()
    if hwnd and ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        SHOTS.mkdir(parents=True, exist_ok=True)
        ImageGrab.grab((r.left, r.top, r.right, r.bottom), include_layered_windows=True).save(SHOTS / f"{name}.png")


exe = Path(sys.executable).with_name("pythonw.exe")
win = subprocess.Popen([str(exe if exe.exists() else sys.executable), str(HERE / "jarvis.py"), "0"], cwd=str(HERE))
time.sleep(2.5)
try:
    hud.send("state", state="wake")
    time.sleep(0.7)
    hud.send("state", state="listen")
    time.sleep(1.5)
    shot("1_hallgat")
    hud.send("heard", text="Nyisd meg a Chrome-ot!")
    hud.send("state", state="think")
    time.sleep(0.6)
    hud.send("tool", name="system_open", input={"target": "chrome"})
    time.sleep(0.4)
    hud.send("result", name="system_open", ok=True, text='{"ok": true, "kind": "app", "opened": "Google Chrome"}')
    time.sleep(0.5)
    shot("2_ikon")
    d = say("Megnyitottam a Google Chrome-ot.")
    time.sleep(min(d, 1.0))
    shot("3_beszel")
    time.sleep(max(0.0, d - 1.0) + 2.5)

    hud.send("state", state="wake")
    time.sleep(0.6)
    hud.send("state", state="listen")
    time.sleep(1.2)
    hud.send("heard", text="Mik a főcímek az index.hu-n?")
    hud.send("state", state="think")
    time.sleep(1.5)
    hud.send("tool", name="look_open", input={"target": "https://index.hu", "electron": False})
    time.sleep(2.0)
    hud.send("result", name="look_open", ok=True, text='{"ok": true, "title": "Index – Hírek", "text": "…"}')
    time.sleep(0.8)
    for s in ("Az Index vezető híre szerint az ügyészség eljárást kezdeményez.",
              "Szerepel még egy rendőrségi razzia az Európai Unió egyik intézményénél.",
              "Felolvassam valamelyik cikket részletesen?"):
        d = say(s)
        time.sleep(d + 0.25)
    hud.send("state", state="idle")
    time.sleep(3)
finally:
    win.terminate()
