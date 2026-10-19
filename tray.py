"""Tálcaikon állapottal és vészleállító (menü + Ctrl+Alt+Shift+H)."""
import ctypes
import ctypes.wintypes
import logging
import os
import threading
from typing import Callable

import pystray
from PIL import Image, ImageDraw

log = logging.getLogger("hub.tray")

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
WM_HOTKEY = 0x0312


def emergency_stop(reason: str) -> None:
    log.warning("Vészleállítás: %s", reason)
    logging.shutdown()
    os._exit(0)  # azonnali, a futó hívásokat is megszakítja


def _run_updater() -> None:
    """Külön folyamatban: a frissítő leállítja és újraindítja a hubot."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent
    exe = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen([str(exe if exe.exists() else sys.executable), str(root / "updater.py"), "ui"], cwd=str(root),
                     creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def _icon_image() -> Image.Image:
    from pathlib import Path
    png = Path(__file__).resolve().parent / "assets" / "ai-hub.png"
    if png.exists():
        return Image.open(png).resize((64, 64))
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(32, 140, 90, 255))
    d.text((21, 12), "H", fill="white", font_size=34)
    return img


def start_hotkey() -> None:
    """Ctrl+Alt+Shift+H → leállítás. Saját szálon, saját üzenetsorral."""

    def loop():
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, ord("H")):
            log.error("A Ctrl+Alt+Shift+H gyorsbillentyű foglalt")
            return
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                emergency_stop("gyorsbillentyű")

    threading.Thread(target=loop, name="hotkey", daemon=True).start()


def run_tray(status: Callable[[], str], open_logs: Callable[[], None]) -> None:
    """Blokkol (főszálon fut)."""
    menu = pystray.Menu(
        pystray.MenuItem(lambda _: status(), None, enabled=False),
        pystray.MenuItem("Folyamatjelző megnyitása", lambda: __import__("hubkit").show_widget()),
        pystray.MenuItem("Frissítés keresése…", lambda: _run_updater()),
        pystray.MenuItem("Napló megnyitása", lambda: open_logs()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Hub leállítása", lambda: emergency_stop("tálcamenü")),
    )
    icon = pystray.Icon("hub", _icon_image(), "AI HUB", menu)
    icon.run()
