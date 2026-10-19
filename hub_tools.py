"""A Hub saját eszközei (hub_*). Ugyanazon a burkolón mennek át, mint a pluginok."""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context

from core import NAME_RE, Hub, norm_level, plugin_hash, tool_prefix

DEP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~ ]*$")


def python_exe() -> str:
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")  # pythonw alatt is konzolos pythont hívunk
    return str(console if console.exists() else exe)


def run_quiet(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
        timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def install(hub: Hub) -> None:
    def pdir(plugin: str) -> Path | None:
        if not NAME_RE.match(plugin or ""):
            return None
        p = hub.plugins_dir / plugin
        return p if (p / "manifest.json").exists() else None

    def hub_list() -> dict:
        """Pluginok és eszközeik tömören: név, verzió, be/ki, eszközök [név, szint, leírás]."""
        plugins = []
        for name, m in hub.manifests().items():
            loaded = hub.loaded.get(name, {}).get("tools", {})
            tools = [
                [t["name"], norm_level(t.get("level")), loaded.get(t["name"], {}).get("desc") or t.get("desc", "")]
                for t in m.get("tools", [])
            ]
            item = {"n": name, "v": m.get("version", ""), "on": name in hub.loaded, "d": m.get("description", ""), "t": tools}
            if "error" in m:
                item["err"] = m["error"]
            plugins.append(item)
        return {"ok": True, "plugins": plugins}

    def hub_new_plugin(name: str, description: str) -> dict:
        """Új plugin-váz létrehozása sablonból (manifest, plugin.py, tests/cases.json)."""
        if not NAME_RE.match(name) or name == "hub":
            return {"ok": False, "reason": "bad_name", "hint": "kisbetű, szám, _ ; 2–31 karakter"}
        target = hub.plugins_dir / name
        if target.exists():
            return {"ok": False, "reason": "exists", "path": str(target)}
        shutil.copytree(hub.root / "templates" / "plugin", target)
        subs = {"{{name}}": name, "{{description}}": description.replace('"', "'"), "{{prefix}}": tool_prefix(name)}
        for f in target.rglob("*"):
            if f.is_file():
                text = f.read_text("utf-8")
                for k, v in subs.items():
                    text = text.replace(k, v)
                f.write_text(text, "utf-8")
        hub.set_enabled(name, False)
        return {
            "ok": True,
            "path": str(target),
            "next": f"írd meg a plugin.py-t, a manifest.json-t és a tests/cases.json-t (eszköznév: {tool_prefix(name)}…), majd hub_test, hub_enable",
        }

    def hub_install_deps(plugin: str) -> dict:
        """A plugin manifestjében megadott függőségek telepítése pip-pel a közös venv-be."""
        p = pdir(plugin)
        if not p:
            return {"ok": False, "reason": "not_found"}
        deps = json.loads((p / "manifest.json").read_text("utf-8")).get("deps", [])
        bad = [d for d in deps if not isinstance(d, str) or not DEP_RE.match(d)]
        if bad:
            return {"ok": False, "reason": "bad_deps", "deps": bad}
        if not deps:
            return {"ok": True, "installed": []}
        r = run_quiet([python_exe(), "-m", "pip", "install", "--disable-pip-version-check", *deps], 900)
        return {"ok": r.returncode == 0, "installed": deps, "tail": (r.stdout + r.stderr)[-600:]}

    def hub_test(plugin: str) -> dict:
        """A plugin tests/cases.json eseteinek futtatása: átment db, bukott esetek és okuk."""
        p = pdir(plugin)
        if not p:
            return {"ok": False, "reason": "not_found"}
        try:
            r = run_quiet([python_exe(), str(hub.root / "hubtest.py"), str(p)], 580)
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": "timeout"}
        lines = r.stdout.strip().splitlines()
        try:
            res = json.loads(lines[-1])
        except (IndexError, json.JSONDecodeError):
            return {"ok": False, "reason": "runner_error", "stderr": r.stderr[-600:]}
        marker = p / ".tested"
        if res.get("ok"):
            marker.write_text(plugin_hash(p), "utf-8")
        elif marker.exists():
            marker.unlink()
        return res

    async def hub_enable(plugin: str, ctx: Context) -> dict:
        """Plugin bekapcsolása újraindítás nélkül (ha a tesztjei átmentek; danger eszköznél jóváhagyással)."""
        p = pdir(plugin)
        if not p:
            return {"ok": False, "reason": "not_found"}
        marker = p / ".tested"
        if not marker.exists() or marker.read_text("utf-8") != plugin_hash(p):
            return {"ok": False, "reason": "untested", "hint": "előbb hub_test (a kód a legutóbbi teszt óta változott)"}
        manifest = json.loads((p / "manifest.json").read_text("utf-8"))
        danger = [t["name"] for t in manifest.get("tools", []) if norm_level(t.get("level")) == "danger"]
        if danger:
            ok = await asyncio.to_thread(
                hub.approve, "Hub – plugin bekapcsolása", f"{plugin}: danger eszközök: {', '.join(danger)}",
                hub.cfg.get("approval_timeout_s", 120),
            )
            if not ok:
                return {"ok": False, "reason": "denied"}
        res = hub.load_plugin(plugin)
        if res["ok"]:
            hub.set_enabled(plugin, True)
            await hub.notify_tools_changed()
        return res

    async def hub_disable(plugin: str, ctx: Context) -> dict:
        """Plugin kikapcsolása újraindítás nélkül."""
        if not pdir(plugin):
            return {"ok": False, "reason": "not_found"}
        hub.unload_plugin(plugin)
        hub.set_enabled(plugin, False)
        await hub.notify_tools_changed()
        return {"ok": True, "plugin": plugin}

    def hub_log(n: int = 20) -> dict:
        """Az utolsó n napló-bejegyzés (max 200)."""
        n = max(1, min(int(n), 200))
        if not hub.audit_path.exists():
            return {"ok": True, "entries": []}
        lines = hub.audit_path.read_text("utf-8").splitlines()[-n:]
        return {"ok": True, "entries": [json.loads(l) for l in lines if l.strip()]}

    def hub_progress(action: str = "status") -> dict:
        """Folyamatjelző: action='status' a futó/nemrég kész folyamatok, 'open' megnyitja a lebegő ablakot."""
        import hubkit
        if action == "open":
            hubkit.show_widget()
            return {"ok": True, "opened": True}
        if action != "status":
            return {"ok": False, "reason": "action: status | open"}
        items = []
        for f in sorted(hubkit.PROGRESS_DIR.glob("*.json")) if hubkit.PROGRESS_DIR.exists() else []:
            try:
                r = json.loads(f.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            items.append({"key": f.stem, "title": r.get("title"), "state": r.get("state"), "pct": r.get("pct"),
                          "detail": r.get("detail"), "s": round(r.get("updated", 0) - r.get("started", 0))})
        return {"ok": True, "items": items[-30:]}

    def hub_update_check() -> dict:
        """Van-e újabb AI HUB kiadás a GitHubon (verziók, kiadási jegyzet)."""
        import updater
        return updater.check()

    def hub_update_apply() -> dict:
        """Frissítés a legújabb kiadásra (jóváhagyással): külön folyamatban leállítja, frissíti és újraindítja a hubot."""
        import updater
        info = updater.check()
        if not info.get("ok") or not info.get("update_available"):
            return info if not info.get("ok") else {"ok": True, "updated": False, "reason": "már a legújabb verzió fut"}
        if (hub.root / ".git").exists():
            return {"ok": False, "reason": "fejlesztői (git) példány – itt `git pull` a frissítés"}
        exe = Path(sys.executable).with_name("python.exe")
        subprocess.Popen([str(exe), str(hub.root / "updater.py"), "apply"], cwd=str(hub.root),
                         creationflags=0x00000008 | 0x00000200 | 0x08000000)
        return {"ok": True, "started": True, "to": info["latest"],
                "note": "a hub pár másodpercre leáll; az állapot a folyamatjelzőben és a logs\\update.log-ban látszik"}

    levels = {
        hub_list: "read", hub_new_plugin: "act", hub_install_deps: "act", hub_test: "act",
        hub_enable: "act", hub_disable: "act", hub_log: "read", hub_progress: "act",
        hub_update_check: "read", hub_update_apply: "danger",
    }
    long_running = {hub_test: 600, hub_install_deps: 900}
    for fn, level in levels.items():
        hub.register(fn, fn.__name__, level, "hub", hub.describe(fn), long_running.get(fn))

    async def hub_call(tool: str, ctx: Context, args: dict[str, Any] | None = None) -> str:
        """Tartalék: plugin-eszköz hívása név alapján, ha a kliens még nem frissítette az eszközlistát."""
        if tool.startswith("hub_") or not any(tool in v["tools"] for v in hub.loaded.values()):
            return json.dumps({"ok": False, "reason": "unknown_tool", "hint": "hub_list"}, ensure_ascii=False)
        hub.sessions.add(ctx.session)
        return await hub.mcp._tool_manager.call_tool(tool, args or {}, context=ctx)

    hub.mcp.add_tool(hub_call, name="hub_call", description=hub.describe(hub_call), structured_output=False)
