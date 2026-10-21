"""system – a gép kezelése, hogy a hangasszisztens (és bármely kliens) végre is tudja hajtani a kéréseket.

  system_open   – weboldal az alapértelmezett böngészőben, fájl/mappa a társított programmal, program név alapján
  system_apps   – a Start menü indítható programjai (szűrhető), hogy a Claude tudja, mi a pontos név
  system_volume – hangerő és némítás lekérdezése/állítása (Core Audio, comtypes)
  system_power  – leállítás / újraindítás / alvás / zárolás / visszavonás (danger: Windows-jóváhagyás)
Programkeresés: a Start menü bejegyzései (Get-StartApps – a lokalizált nevek és az áruházas appok is)
+ a Start menü mappáinak .lnk-jai; egyezés kis/nagybetű és ékezet nélkül. Magyar becenevek: böngésző,
jegyzettömb, számológép, fájlkezelő, beállítások, letöltések/dokumentumok/asztal mappa stb.
Futtatható fájlt (exe, bat, ps1, …) útvonalról nem indít: programot csak a Start menüből, név alapján.
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import winreg
from pathlib import Path

from hubkit import tool

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
START_DIRS = [Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
              Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs"]
CACHE_S = 600
SLEEP_DELAY_S = 10       # alvás előtt ennyit vár, hogy a válasz még elhangozzon
MAX_DELAY_S = 86400
SCHEMES = ("http://", "https://", "mailto:", "ms-settings:")
DOMAIN_RE = re.compile(r"^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,24}(:\d+)?([/?#]\S*)?$", re.I)
# ezek fájlnevek, nem domainek ("jegyzet.txt" ne legyen weboldal)
FILE_EXT = {"exe", "lnk", "bat", "cmd", "ps1", "txt", "md", "pdf", "doc", "docx", "xls", "xlsx", "xlsm", "ppt", "pptx",
            "csv", "json", "xml", "dxf", "dwg", "stl", "png", "jpg", "jpeg", "gif", "mp3", "mp4", "wav", "mkv", "avi",
            "zip", "7z", "rar", "py", "js", "html", "htm", "msi", "url", "log", "ini", "cfg"}
# útvonalról nem indítunk kódot
BLOCKED = {".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msi",
           ".msp", ".scr", ".hta", ".cpl", ".pif", ".reg", ".jar", ".py", ".pyw", ".appref-ms", ".lnk", ".url"}
SKIP_WORDS = ("uninstall", "eltavolit", "readme", "release notes", "documentation", "help", "sugo", "updater",
              "error reporter", "diagnostics")
PROGRAMS = {  # normalizált becenév → (megjelenő név, parancs)
    "jegyzettomb": ("Jegyzettömb", "notepad.exe"), "notepad": ("Jegyzettömb", "notepad.exe"),
    "szamologep": ("Számológép", "calc.exe"), "kalkulator": ("Számológép", "calc.exe"),
    "calculator": ("Számológép", "calc.exe"),
    "fajlkezelo": ("Fájlkezelő", "explorer.exe"), "intezo": ("Fájlkezelő", "explorer.exe"),
    "explorer": ("Fájlkezelő", "explorer.exe"), "sajatgep": ("Fájlkezelő", "explorer.exe"),
    "beallitasok": ("Beállítások", "ms-settings:"), "gepbeallitasok": ("Beállítások", "ms-settings:"),
    "settings": ("Beállítások", "ms-settings:"),
    "feladatkezelo": ("Feladatkezelő", "taskmgr.exe"), "taskmanager": ("Feladatkezelő", "taskmgr.exe"),
    "vezerlopult": ("Vezérlőpult", "control.exe"), "paint": ("Paint", "mspaint.exe"),
}
BROWSER = {"bongeszo", "bongeszot", "webbongeszo", "internet", "browser", "alapertelmezettbongeszo"}
FOLDERS = {  # normalizált név → a „Shell Folders” kulcs értékneve
    "letoltesek": "{374DE290-123F-4565-9164-39C4925E467B}", "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "dokumentumok": "Personal", "documents": "Personal", "asztal": "Desktop", "desktop": "Desktop",
    "kepek": "My Pictures", "pictures": "My Pictures", "zene": "My Music", "zenek": "My Music", "music": "My Music",
    "videok": "My Video", "videos": "My Video",
}

_apps = {"ts": 0.0, "list": []}
_sleep = {"timer": None}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").casefold()
    return "".join(c for c in s if c.isalnum())


def _words(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s or "").casefold()
    return [w for w in re.split(r"[^0-9a-z]+", "".join(c for c in s if not unicodedata.combining(c))) if w]


def _start_apps() -> list[dict]:
    """[{name, launch: ('appid'|'lnk', érték)}] – Get-StartApps + .lnk fájlok, név szerint egyedi; 10 percig gyorsítótárazva."""
    if _apps["list"] and time.time() - _apps["ts"] < CACHE_S:
        return _apps["list"]
    seen, out = set(), []
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                           creationflags=NOWIN)
        data = json.loads(r.stdout or "[]")
        for a in data if isinstance(data, list) else [data]:
            name, appid = (a.get("Name") or "").strip(), (a.get("AppID") or "").strip()
            if name and appid and norm(name) not in seen:
                seen.add(norm(name))
                out.append({"name": name, "launch": ("appid", appid)})
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    for d in START_DIRS:
        if d.is_dir():
            for p in d.rglob("*.lnk"):
                if norm(p.stem) not in seen:
                    seen.add(norm(p.stem))
                    out.append({"name": p.stem, "launch": ("lnk", str(p))})
    out.sort(key=lambda a: a["name"].casefold())
    _apps.update(ts=time.time(), list=out)
    return out


def _score(query: str, name: str) -> int:
    q, n = norm(query), norm(name)
    if not q:
        return 1
    if n == q:
        return 100
    if n.startswith(q):
        return 80
    qw, nw = _words(query), _words(name)
    if qw and all(any(w.startswith(x) for w in nw) for x in qw):
        return 60
    if q in n:
        return 50
    return 0


def _skipped(name: str, query: str) -> bool:
    low, q = " ".join(_words(name)), " ".join(_words(query))
    return any(w in low and w not in q for w in SKIP_WORDS)


def find_app(query: str) -> tuple[dict | None, list[str]]:
    """A legjobb Start menü-találat és a többi jelölt neve (legfeljebb 4)."""
    scored = [(_score(query, a["name"]), a) for a in _start_apps() if not _skipped(a["name"], query)]
    scored = sorted((x for x in scored if x[0] > 0), key=lambda x: (-x[0], len(x[1]["name"])))
    if not scored:
        return None, []
    return scored[0][1], [a["name"] for _, a in scored[1:5]]


def _folder(key: str) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
            return winreg.QueryValueEx(k, key)[0]
    except OSError:
        return None


def _default_browser() -> tuple[str, str] | None:
    """(név, exe) az alapértelmezett https-kezelőből."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations"
                                                      r"\UrlAssociations\https\UserChoice") as k:
            progid = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\shell\open\command") as k:
            cmd = winreg.QueryValueEx(k, "")[0]
    except OSError:
        return None
    m = re.match(r'\s*"([^"]+)"|\s*(\S+)', cmd)
    exe = (m.group(1) or m.group(2)) if m else ""
    names = {"msedge": "Microsoft Edge", "chrome": "Google Chrome", "firefox": "Firefox", "brave": "Brave",
             "opera": "Opera", "vivaldi": "Vivaldi"}
    return (names.get(Path(exe).stem.lower(), Path(exe).stem), exe) if exe and Path(exe).exists() else None


def resolve(target: str) -> dict:
    """Mit nyitna meg a target: {kind, name, run} vagy {error}. Nem indít semmit."""
    t = (target or "").strip().strip('"').strip()
    if not t:
        return {"error": "üres target"}
    low = t.lower()
    if low.startswith(SCHEMES):
        return {"kind": "url", "name": t, "run": ("start", t)}
    if re.match(r"^www\.", low) or (" " not in t and DOMAIN_RE.match(t) and low.rsplit(".", 1)[-1].split("/")[0]
                                     not in FILE_EXT and not Path(t).exists()):
        url = "https://" + t
        return {"kind": "url", "name": url, "run": ("start", url)}
    if re.match(r"^[a-z][a-z0-9+.-]+:", low):  # file:, javascript:, egyéb protokoll
        return {"error": "csak http(s), mailto és ms-settings cím nyitható meg"}
    p = Path(os.path.expandvars(os.path.expanduser(t)))
    if p.is_absolute() and p.exists():
        if p.is_file() and p.suffix.lower() in BLOCKED:
            return {"error": f"futtatható fájlt ({p.suffix}) útvonalról nem indítok; programot név alapján nyiss meg"}
        return {"kind": "folder" if p.is_dir() else "file", "name": str(p), "run": ("start", str(p))}
    n = norm(t)
    for suffix in ("mappat", "mappa", "mappam", "mappamat"):  # „letöltések mappa” → letöltések
        if n.endswith(suffix) and n[:-len(suffix)] in FOLDERS:
            n = n[:-len(suffix)]
    if n in FOLDERS:
        path = _folder(FOLDERS[n])
        if path and Path(path).is_dir():
            return {"kind": "folder", "name": path, "run": ("start", path)}
    if n in BROWSER:
        b = _default_browser()
        if b:
            return {"kind": "app", "name": b[0], "run": ("exe", b[1])}
        return {"kind": "url", "name": "https://www.google.com", "run": ("start", "https://www.google.com")}
    if n in PROGRAMS:
        name, cmd = PROGRAMS[n]
        return {"kind": "app", "name": name, "run": ("start" if cmd.endswith(":") else "exe", cmd)}
    app, others = find_app(t)
    if app:
        out = {"kind": "app", "name": app["name"], "run": app["launch"]}
        if others:
            out["others"] = others
        return out
    if p.exists():
        return {"error": "relatív útvonal helyett teljes útvonalat adj meg"}
    return {"error": f"nem találom: {t!r} (nem weboldal, nem létező fájl, és nincs ilyen program a Start menüben; "
                     f"lásd system_apps)"}


def _launch(run: tuple[str, str]) -> None:
    how, what = run
    if how == "appid":
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{what}"], creationflags=NOWIN, close_fds=True)
    elif how == "exe":
        subprocess.Popen([what], close_fds=True)
    else:  # start / lnk – a társított programmal
        os.startfile(what)  # noqa: S606


@tool
def system_open(target: str, dry_run: bool = False) -> dict:
    """Megnyit: weboldal (URL vagy domain) böngészőben, fájl/mappa, programnév (Start menü), 'böngésző', 'letöltések'."""
    r = resolve(target)
    if "error" in r:
        return {"ok": False, "error": r["error"]}
    out = {"ok": True, "kind": r["kind"], "opened": r["name"]}
    if r.get("others"):
        out["others"] = r["others"]
    if dry_run:
        out["dry_run"] = True
        return out
    try:
        _launch(r["run"])
    except OSError as e:
        return {"ok": False, "error": f"nem sikerült megnyitni: {e}"[:300]}
    return out


@tool
def system_apps(query: str = "", limit: int = 80) -> dict:
    """A Start menü indítható programjainak neve (query szűr, ékezet nélkül is); a név mehet a system_open-nek."""
    apps = _start_apps()
    if query:
        scored = sorted(((_score(query, a["name"]), a["name"]) for a in apps), key=lambda x: (-x[0], len(x[1])))
        names = [n for s, n in scored if s > 0]
    else:
        names = [a["name"] for a in apps if not _skipped(a["name"], "")]
    limit = max(1, min(int(limit), 500))
    out = {"ok": True, "n": len(names), "apps": names[:limit]}
    if len(names) > limit:
        out["more"] = len(names) - limit
    return out


def _gpu() -> dict | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5,
                           creationflags=NOWIN)
        name, util, temp, used, total = [x.strip() for x in r.stdout.splitlines()[0].split(",")]
        return {"name": name, "pct": int(util), "temp_c": int(temp), "mem_pct": round(100 * int(used) / int(total))}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, ZeroDivisionError):
        return None


def _online() -> bool:
    import socket
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=1.0):
            return True
    except OSError:
        return False


@tool
def system_status() -> dict:
    """Gép állapota: CPU, memória, GPU (terhelés, hőfok), akku, C: szabad hely, internet, üzemidő."""
    import psutil
    out = {"ok": True, "cpu_pct": round(psutil.cpu_percent(interval=0.4)), "ram_pct": round(psutil.virtual_memory().percent)}
    g = _gpu()
    if g:
        out["gpu"] = g
    b = psutil.sensors_battery()
    if b is not None:
        out["battery"] = {"pct": round(b.percent), "plugged": bool(b.power_plugged)}
    du = psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
    out["disk_c"] = {"free_gb": round(du.free / 2**30), "pct": round(du.percent)}
    out["online"] = _online()
    out["uptime_h"] = round((time.time() - psutil.boot_time()) / 3600, 1)
    return out


# ---------------------------------------------------------------- hangerő (Core Audio)
_iface = {}


def _audio_types():
    if _iface:
        return _iface
    from ctypes import POINTER, c_float, c_uint, c_void_p
    from ctypes.wintypes import BOOL, DWORD

    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    class IAudioEndpointVolume(IUnknown):
        _iid_ = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
        _methods_ = [
            COMMETHOD([], HRESULT, "RegisterControlChangeNotify", (["in"], c_void_p, "notify")),
            COMMETHOD([], HRESULT, "UnregisterControlChangeNotify", (["in"], c_void_p, "notify")),
            COMMETHOD([], HRESULT, "GetChannelCount", (["out"], POINTER(c_uint), "count")),
            COMMETHOD([], HRESULT, "SetMasterVolumeLevel", (["in"], c_float, "db"), (["in"], POINTER(GUID), "ctx")),
            COMMETHOD([], HRESULT, "SetMasterVolumeLevelScalar", (["in"], c_float, "level"),
                      (["in"], POINTER(GUID), "ctx")),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevel", (["out"], POINTER(c_float), "db")),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar", (["out"], POINTER(c_float), "level")),
            COMMETHOD([], HRESULT, "SetChannelVolumeLevel", (["in"], c_uint, "ch"), (["in"], c_float, "db"),
                      (["in"], POINTER(GUID), "ctx")),
            COMMETHOD([], HRESULT, "SetChannelVolumeLevelScalar", (["in"], c_uint, "ch"), (["in"], c_float, "level"),
                      (["in"], POINTER(GUID), "ctx")),
            COMMETHOD([], HRESULT, "GetChannelVolumeLevel", (["in"], c_uint, "ch"), (["out"], POINTER(c_float), "db")),
            COMMETHOD([], HRESULT, "GetChannelVolumeLevelScalar", (["in"], c_uint, "ch"),
                      (["out"], POINTER(c_float), "level")),
            COMMETHOD([], HRESULT, "SetMute", (["in"], BOOL, "mute"), (["in"], POINTER(GUID), "ctx")),
            COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(BOOL), "mute")),
        ]

    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], DWORD, "ctx"),
                      (["in"], c_void_p, "params"), (["out"], POINTER(POINTER(IUnknown)), "iface")),
        ]

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_uint, "flow"), (["in"], DWORD, "mask"),
                      (["out"], POINTER(POINTER(IUnknown)), "devices")),
            COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_uint, "flow"), (["in"], c_uint, "role"),
                      (["out"], POINTER(POINTER(IMMDevice)), "device")),
        ]

    _iface.update(vol=IAudioEndpointVolume, enum=IMMDeviceEnumerator,
                  clsid=GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"))
    return _iface


def _volume_job(level, change, mute) -> dict:
    """Saját szálon fut (saját COM-lakás), hogy a Hub szálkészletének COM-állapotát ne zavarja."""
    import comtypes
    from comtypes import CLSCTX_ALL

    comtypes.CoInitialize()
    try:
        t = _audio_types()
        enum = comtypes.CoCreateInstance(t["clsid"], t["enum"], CLSCTX_ALL)
        dev = enum.GetDefaultAudioEndpoint(0, 1)  # eRender, eMultimedia
        vol = dev.Activate(ctypes.byref(t["vol"]._iid_), CLSCTX_ALL, None).QueryInterface(t["vol"])
        before = round(vol.GetMasterVolumeLevelScalar() * 100)
        was_muted = bool(vol.GetMute())
        new = before
        if level is not None:
            new = level
        if change:
            new = new + change
        new = max(0, min(100, int(new)))
        if new != before:
            vol.SetMasterVolumeLevelScalar(new / 100, None)
        if mute is not None and bool(mute) != was_muted:
            vol.SetMute(bool(mute), None)
        out = {"ok": True, "level": round(vol.GetMasterVolumeLevelScalar() * 100), "mute": bool(vol.GetMute())}
        if out["level"] != before or out["mute"] != was_muted:
            out["was"] = {"level": before, "mute": was_muted}
        del vol, dev, enum
        return out
    finally:
        comtypes.CoUninitialize()


@tool
def system_volume(level: int | None = None, change: int = 0, mute: bool | None = None) -> dict:
    """Hangerő 0–100: paraméter nélkül lekérdez; level=beállít, change=±lépés (pl. 10/-10), mute=némít/visszakapcsol."""
    if level is not None and not 0 <= level <= 100:
        return {"ok": False, "error": "a level 0 és 100 közötti szám legyen"}
    if not -100 <= change <= 100:
        return {"ok": False, "error": "a change -100 és 100 közötti szám legyen"}
    box = {}

    def job():
        try:
            box["r"] = _volume_job(level, change, mute)
        except Exception as e:  # noqa: BLE001 – COM-hiba szövegként menjen vissza
            box["r"] = {"ok": False, "error": f"hangerő: {type(e).__name__}: {e}"[:300]}

    th = threading.Thread(target=job, daemon=True)
    th.start()
    th.join(15)
    return box.get("r", {"ok": False, "error": "a hangeszköz nem válaszolt"})


# ---------------------------------------------------------------- tápellátás
POWER = {"shutdown": "shutdown", "leallitas": "shutdown", "kikapcsolas": "shutdown",
         "restart": "restart", "ujrainditas": "restart", "reboot": "restart",
         "sleep": "sleep", "alvas": "sleep", "lock": "lock", "zarolas": "lock",
         "cancel": "cancel", "megse": "cancel", "visszavonas": "cancel", "abort": "cancel"}


def _suspend() -> None:
    _sleep["timer"] = None
    ctypes.windll.powrprof.SetSuspendState(False, False, False)


def _shutdown(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["shutdown", *args], capture_output=True, text=True, encoding="oem", errors="replace",
                          timeout=15, creationflags=NOWIN)


@tool
def system_power(action: str, delay_s: int = 60) -> dict:
    """Gép: shutdown/restart (delay_s mp múlva, programokat bezárja), sleep, lock, cancel (függő leállítás visszavonása)."""
    a = POWER.get(norm(action))
    if not a:
        return {"ok": False, "error": "action: shutdown, restart, sleep, lock vagy cancel"}
    if a in ("shutdown", "restart"):
        if not 0 <= delay_s <= MAX_DELAY_S:
            return {"ok": False, "error": f"a delay_s 0 és {MAX_DELAY_S} közötti legyen"}
        r = _shutdown("/s" if a == "shutdown" else "/r", "/t", str(int(delay_s)))
        if r.returncode:
            return {"ok": False, "error": (r.stderr or r.stdout).strip()[:300] or f"hibakód {r.returncode}"}
        return {"ok": True, "action": a, "in_s": int(delay_s), "undo": "system_power(action='cancel')"}
    if a == "sleep":
        if _sleep["timer"]:
            _sleep["timer"].cancel()
        _sleep["timer"] = threading.Timer(SLEEP_DELAY_S, _suspend)
        _sleep["timer"].daemon = True
        _sleep["timer"].start()
        return {"ok": True, "action": a, "in_s": SLEEP_DELAY_S, "undo": "system_power(action='cancel')"}
    if a == "lock":
        if not ctypes.windll.user32.LockWorkStation():
            return {"ok": False, "error": "a zárolás nem sikerült"}
        return {"ok": True, "action": a}
    # cancel
    cancelled = []
    if _sleep["timer"]:
        _sleep["timer"].cancel()
        _sleep["timer"] = None
        cancelled.append("sleep")
    r = _shutdown("/a")
    if r.returncode == 0:
        cancelled.append("shutdown")
    return {"ok": True, "action": a, "cancelled": cancelled or "nem volt függő leállítás vagy alvás"}


def __hub_unload__():
    if _sleep["timer"]:
        _sleep["timer"].cancel()
        _sleep["timer"] = None
