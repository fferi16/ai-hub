"""A plugin be/kikapcsolása nem írhatja felül a futás közben beírt beállításokat.
Futtatás: python tests\\test_config.py   (kilépési kód 0 = minden átment)"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Hub  # noqa: E402

fails = 0


def check(name: str, cond: bool) -> None:
    global fails
    print(("OK  " if cond else "HIBA") + " " + name)
    fails += not cond


with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    start = {"port": 8765, "plugins": {"a": {"enabled": True}}}
    (root / "config.json").write_text(json.dumps(start), "utf-8")
    hub = Hub(root, json.loads(json.dumps(start)), None)

    # futás közben valaki (kézzel / plugin) új szakaszt ír a fájlba
    disk = json.loads((root / "config.json").read_text("utf-8"))
    disk["voice"] = {"wake": "figyelj"}
    disk["plugins"]["a"]["extra"] = 1
    (root / "config.json").write_text(json.dumps(disk), "utf-8")

    hub.set_enabled("b", True)
    after = json.loads((root / "config.json").read_text("utf-8"))
    check("a kézzel beírt szakasz megmaradt", after.get("voice") == {"wake": "figyelj"})
    check("a plugin saját extra mezője megmaradt", after["plugins"]["a"].get("extra") == 1)
    check("az új kapcsoló a fájlban", after["plugins"]["b"]["enabled"] is True)
    check("az új kapcsoló a memóriában", hub.is_enabled("b"))

    hub.set_enabled("a", False)
    after = json.loads((root / "config.json").read_text("utf-8"))
    check("kikapcsolás után is megvan a szakasz", after.get("voice") == {"wake": "figyelj"})
    check("a kikapcsolás érvényes", after["plugins"]["a"]["enabled"] is False and not hub.is_enabled("a"))

    (root / "config.json").write_text("{hibás json", "utf-8")
    hub.set_enabled("c", True)
    check("hibás fájlnál a memóriabeli konfigból ír", json.loads((root / "config.json").read_text("utf-8"))
          ["plugins"]["c"]["enabled"] is True)

print("EREDMÉNY:", "minden rendben" if not fails else f"{fails} hiba")
sys.exit(1 if fails else 0)
