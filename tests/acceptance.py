"""A hub-spec.md 6 elfogadási tesztje. A Hubnak futnia kell (start_hub.cmd).

Futtatás: .venv\\Scripts\\python.exe tests\\acceptance.py
A 3. tesztnél Windows-értesítés jelenik meg: kattints a „Nem” gombra (vagy 2 perc után magától Nem).
A 6. teszt elküldi a Ctrl+Alt+Shift+H billentyűt, és leállítja a Hubot.
"""
import asyncio
import ctypes
import json
import shutil
import sys
import time
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8765/mcp"
results: dict[int, tuple[bool, str]] = {}

HELLO_MANIFEST = {
    "name": "hello", "version": "0.1.0", "description": "Köszönő próba-plugin",
    "tools": [{"name": "hello_say", "level": "read"}], "deps": [],
}
HELLO_PY = '''"""hello – köszönő próba-plugin."""
from hubkit import tool


@tool
def hello_say(name: str) -> dict:
    """Köszön a megadott névnek."""
    return {"ok": True, "text": f"Szia, {name}!"}
'''
HELLO_CASES = [
    {"tool": "hello_say", "args": {"name": "Feri"}, "expect": {"ok": True, "text": "Szia, Feri!"}},
    {"tool": "hello_say", "args": {"name": "Anna"}, "contains": "Anna"},
]


def text(res) -> dict:
    return json.loads(res.content[0].text)


def check(n: int, ok: bool, info: str) -> None:
    results[n] = (ok, info)
    print(f"[{'OK ' if ok else 'HIBA'}] {n}. {info}", flush=True)


async def main() -> None:
    hello_dir = ROOT / "plugins" / "hello"
    t_start = time.strftime("%Y-%m-%dT%H:%M:%S")

    async with streamablehttp_client(URL) as (r, w, _), ClientSession(r, w) as s:
        await s.initialize()
        if hello_dir.exists():  # tiszta kezdés: a korábbi próba kikapcsolása és eltávolítása
            await s.call_tool("hub_disable", {"plugin": "hello"})
            shutil.rmtree(hello_dir)

        # 1. hub_list → look
        lst = text(await s.call_tool("hub_list", {}))
        names = [p["n"] for p in lst["plugins"]]
        check(1, "look" in names, f"hub_list pluginjai: {names}")

        # 2. hello plugin a plugin-gyárral
        new = text(await s.call_tool("hub_new_plugin", {"name": "hello", "description": "Köszönő próba-plugin"}))
        (hello_dir / "manifest.json").write_text(json.dumps(HELLO_MANIFEST, ensure_ascii=False, indent=2), "utf-8")
        (hello_dir / "plugin.py").write_text(HELLO_PY, "utf-8")
        (hello_dir / "tests" / "cases.json").write_text(json.dumps(HELLO_CASES, ensure_ascii=False, indent=2), "utf-8")
        tst = text(await s.call_tool("hub_test", {"plugin": "hello"}))
        before = {t.name for t in (await s.list_tools()).tools}
        en = text(await s.call_tool("hub_enable", {"plugin": "hello"}))
        after = {t.name for t in (await s.list_tools()).tools}
        say = text(await s.call_tool("hello_say", {"name": "Feri"}))
        ok2 = new["ok"] and tst["ok"] and en["ok"] and "hello_say" not in before and "hello_say" in after \
            and say.get("text") == "Szia, Feri!"
        check(2, ok2, f"new={new['ok']} test={tst.get('passed')}/{len(HELLO_CASES)} enable={en['ok']} hello_say → {say}")

        # 3. danger → értesítés, Nem → denied
        print("   → Most jön a Windows-értesítés: kattints a „Nem” gombra!", flush=True)
        t0 = time.time()
        dg = text(await s.call_tool("probe_danger", {"note": "elfogadási teszt"}))
        check(3, dg == {"ok": False, "reason": "denied"}, f"probe_danger → {dg} ({time.time() - t0:.0f} s)")

        # 4. 10 000 karakter → levágás + out\
        big = text(await s.call_tool("probe_big", {"n": 10000}))
        p = Path(big.get("path", ""))
        full = p.read_text("utf-8") if p.is_file() else ""
        ok4 = big.get("truncated") is True and len(json.dumps(big)) < 4000 and len(full) >= 10000
        check(4, ok4, f"levágva={big.get('truncated')}, válasz {len(json.dumps(big))} kar., fájl {len(full)} kar.: {p.name}")

        # 5. minden hívás az audit.jsonl-ban
        lines = (ROOT / "logs" / "audit.jsonl").read_text("utf-8").splitlines()
        entries = [json.loads(l) for l in lines if l.strip()]
        entries = [e for e in entries if e["ts"] >= t_start]
        seen = {(e["tool"], e["result"]) for e in entries}
        need = [("hub_list", "ok"), ("hub_new_plugin", "ok"), ("hub_test", "ok"), ("hub_enable", "ok"),
                ("hello_say", "ok"), ("probe_danger", "denied"), ("probe_big", "ok")]
        missing = [n for n in need if n not in seen]
        check(5, not missing, f"{len(entries)} bejegyzés, hiányzik: {missing or 'semmi'}")

    # 6. vészleállító (Ctrl+Alt+Shift+H szimulálva) → kapcsolódási hiba
    user32 = ctypes.windll.user32
    keys = [0x11, 0x12, 0x10, ord("H")]  # Ctrl, Alt, Shift, H
    for k in keys:
        user32.keybd_event(k, 0, 0, 0)
    for k in reversed(keys):
        user32.keybd_event(k, 0, 2, 0)
    await asyncio.sleep(2)
    try:
        async with streamablehttp_client(URL) as (r, w, _), ClientSession(r, w) as s:
            await asyncio.wait_for(s.initialize(), 5)
        check(6, False, "a Hub még válaszol")
    except BaseException as e:  # noqa: BLE001 – a kapcsolódási hiba ExceptionGroupban érkezik
        check(6, True, f"kapcsolódási hiba: {type(e).__name__}")

    passed = sum(ok for ok, _ in results.values())
    print(f"\nEredmény: {passed}/6 átment")
    sys.exit(0 if passed == 6 else 1)


if __name__ == "__main__":
    asyncio.run(main())
