"""AI HUB asztali ikon: ha a hub nem fut, elindítja; ha fut, megnyitja a folyamatjelzőt és jelzi az állapotot."""
import json
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def running(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def toast(title: str, text: str) -> None:
    try:
        from windows_toasts import Toast, WindowsToaster
        WindowsToaster("AI HUB").show_toast(Toast([title, text]))
    except Exception:  # noqa: BLE001
        pass


cfg = json.loads((ROOT / "config.json").read_text("utf-8"))
version = (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").exists() else "?"
if running(cfg["port"]):
    import hubkit
    hubkit.show_widget()
    toast("AI HUB fut", f"v{version} · http://127.0.0.1:{cfg['port']}{cfg['path']} · tálcaikon: menü, frissítés")
else:
    exe = Path(sys.executable).with_name("pythonw.exe")
    subprocess.Popen([str(exe if exe.exists() else sys.executable), str(ROOT / "hub.py")], cwd=str(ROOT),
                     creationflags=0x08000000)
    toast("AI HUB indul", f"v{version} · pár másodperc múlva megjelenik a tálcaikon")
