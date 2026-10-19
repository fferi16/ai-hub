"""A frissítő logikájának tesztjei: python tests\\test_updater.py  (kilépési kód 0 = minden átment)

- skip(): a felhasználói adatokhoz és a telepítő eltávolítójához nem nyúl, a programfájlokhoz igen
- merge(): a felhasználó beállításai nyernek, az új kulcsok bekerülnek
- version_tuple(): verzió-összehasonlítás
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("updater_under_test", ROOT / "updater.py")
up = importlib.util.module_from_spec(spec)
spec.loader.exec_module(up)

fails = []


def check(name, cond):
    print(("OK   " if cond else "HIBA ") + name)
    if not cond:
        fails.append(name)


for n in ["unins000.exe", "unins000.dat", "UNINS001.MSG", "config.json", ".installed_files", "out/x.json",
          "logs/hub.log", ".venv/Scripts/python.exe", "plugins/media/.tested", "plugins/pdf/__pycache__/x.pyc"]:
    check(f"kihagyja: {n}", up.skip(Path(n)))
for n in ["hub.py", "uninstall_autostart.ps1", "plugins/media/plugin.py", "assets/ai-hub.ico", "installer/ai-hub.iss"]:
    check(f"frissíti: {n}", not up.skip(Path(n)))

old = {"tool_timeout_s": 77, "plugins": {"media": {"enabled": False}}, "update": {"repo": "a/b"}, "sajat": 1}
new = {"tool_timeout_s": 120, "plugins": {"media": {"enabled": True}, "uj": {"enabled": True}},
       "update": {"repo": "x/y", "check_daily": True}, "uj_kulcs": 3}
m = up.merge(old, new)
check("a felhasználó értéke marad", m["tool_timeout_s"] == 77 and m["plugins"]["media"]["enabled"] is False)
check("az új plugin és kulcs bekerül", m["plugins"]["uj"]["enabled"] and m["uj_kulcs"] == 3)
check("beágyazott: régi repo marad, új kulcs bejön", m["update"] == {"repo": "a/b", "check_daily": True})
check("saját kulcs megmarad", m["sajat"] == 1)

check("verzió: 0.10.0 > 0.9.9", up.version_tuple("0.10.0") > up.version_tuple("0.9.9"))
check("verzió: v0.2.1 > 0.2.0", up.version_tuple("v0.2.1") > up.version_tuple("0.2.0"))
check("verzió: azonos", up.version_tuple("0.2.0") == up.version_tuple("v0.2.0"))

print(f"\n{'MINDEN ÁTMENT' if not fails else f'{len(fails)} HIBA'}")
sys.exit(1 if fails else 0)
