"""A pluginok ezt importálják: eszköz-dekorátor, titokkezelés, haladásjelzés.

    from hubkit import tool, secret, progress

    @tool
    def hello_say(name: str) -> dict:
        "Köszön a megadott névnek."
        return {"ok": True, "text": f"Szia, {name}!"}

Hosszú munkánál (akár háttérfolyamatból is):
    progress("sajat-kulcs", "Mit csinálok", pct=40, detail="2/5 fájl")   # pct=None → mozgó csík
    progress("sajat-kulcs", "Mit csinálok", state="done", detail="kész")
A haladás a lebegő folyamatjelzőben látszik (widget.py), amely magától megnyílik.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import keyring

SERVICE = "hub"
ROOT = Path(__file__).resolve().parent
PROGRESS_DIR = ROOT / "out" / "progress"
_known_secrets: set[str] = set()


def progress(key: str, title: str, pct: float | None = None, detail: str = "", state: str = "running",
             keep_s: int = 600, show_after_s: float = 0, widget: bool = True) -> None:
    """Haladás bejegyzése a közös folyamatmappába (state: running | done | error).
    keep_s: a kész bejegyzés ennyi ideig látszik; show_after_s: csak ennyi futás után jelenik meg."""
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)[:80]
    path = PROGRESS_DIR / f"{safe}.json"
    started = time.time()
    try:
        started = json.loads(path.read_text("utf-8")).get("started", started)
    except (OSError, ValueError):
        new = True
    else:
        new = False
    rec = {"title": title, "pct": None if pct is None else max(0.0, min(100.0, float(pct))), "detail": detail,
           "state": state, "started": started, "updated": time.time(), "keep_s": keep_s, "show_after_s": show_after_s}
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False), "utf-8")
    os.replace(tmp, path)
    if widget and new and state == "running" and show_after_s <= 0:
        show_widget()


def show_widget() -> None:
    """A lebegő folyamatjelző indítása (egy példány fut; ha már nyitva van, nem nyílik újra)."""
    exe = Path(sys.executable).with_name("pythonw.exe")
    try:
        subprocess.Popen([str(exe if exe.exists() else sys.executable), str(ROOT / "widget.py")],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
    except OSError:
        pass


def tool(fn=None, *, name: str | None = None):
    """Eszközzé jelöli a függvényt. A szintet NEM itt, hanem a manifest adja meg."""

    def deco(f):
        f.__hub_tool__ = name or f.__name__
        return f

    return deco(fn) if fn is not None else deco


def secret(key: str) -> str | None:
    """Titok a Windows Hitelesítőadat-kezelőből (keyring, szolgáltatás: 'hub').
    A visszaadott értéket a Hub kitakarja a naplóból és a kimenetből."""
    value = keyring.get_password(SERVICE, key)
    if value:
        _known_secrets.add(value)
    return value


def known_secrets() -> set[str]:
    return set(_known_secrets)
