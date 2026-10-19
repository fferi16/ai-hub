"""Plugin-betöltés és az egységes hívásburkoló (szint, jóváhagyás, napló, levágás)."""
import asyncio
import functools
import hashlib
import importlib.util
import inspect
import itertools
import json
import logging
import re
import sys
import threading
import time
import weakref
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import Context, FastMCP

import hubkit

log = logging.getLogger("hub.core")

LEVELS = {"read": 0, "act": 1, "danger": 2}
NAME_RE = re.compile(r"^_?[a-z][a-z0-9_]{1,30}$")
SECRET_KEY_RE = re.compile(r"pass|secret|token|api_?key|credential|auth|pwd|jelsz", re.I)
_counter = itertools.count()
CALL_SHOW_AFTER_S = 3  # ennél rövidebb eszközhívás nem jelenik meg a folyamatjelzőben
CALL_KEEP_S = 20       # a befejezett hívás ennyi ideig látszik még


def compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def norm_level(level: Any) -> str:
    """Ismeretlen vagy hiányzó szint → danger (kétes esetben a magasabb)."""
    return level if level in LEVELS else "danger"


def tool_prefix(plugin: str) -> str:
    return plugin.lstrip("_") + "_"


def plugin_hash(pdir: Path) -> str:
    h = hashlib.sha256()
    for rel in ("manifest.json", "plugin.py", "tests/cases.json"):
        p = pdir / rel
        h.update(rel.encode())
        h.update(p.read_bytes() if p.exists() else b"")
    return h.hexdigest()


class Hub:
    def __init__(self, root: Path, cfg: dict, mcp: FastMCP):
        self.root, self.cfg, self.mcp = root, cfg, mcp
        self.plugins_dir = root / "plugins"
        self.out_dir = root / "out"
        self.audit_path = root / "logs" / "audit.jsonl"
        for d in (self.plugins_dir, self.out_dir, self.audit_path.parent):
            d.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.loaded: dict[str, dict] = {}  # plugin -> {"manifest", "tools": {név: leírás}}
        self.sessions: "weakref.WeakSet" = weakref.WeakSet()
        # Jóváhagyás: (cím, szöveg, időkorlát) -> bool. Alapból mindent elutasít.
        self.approve: Callable[[str, str, int], bool] = lambda *a: False

    # ---------- konfiguráció ----------
    def save_config(self) -> None:
        with self.lock:
            (self.root / "config.json").write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2) + "\n", "utf-8")

    def is_enabled(self, plugin: str) -> bool:
        return bool(self.cfg.get("plugins", {}).get(plugin, {}).get("enabled"))

    def set_enabled(self, plugin: str, on: bool) -> None:
        with self.lock:
            self.cfg.setdefault("plugins", {}).setdefault(plugin, {})["enabled"] = on
            self.save_config()

    def manifests(self) -> dict[str, dict]:
        out = {}
        for mf in sorted(self.plugins_dir.glob("*/manifest.json")):
            try:
                out[mf.parent.name] = json.loads(mf.read_text("utf-8"))
            except Exception as e:
                out[mf.parent.name] = {"name": mf.parent.name, "error": f"hibás manifest: {e}"}
        return out

    # ---------- titkok ----------
    def redact(self, obj: Any) -> Any:
        secrets = hubkit.known_secrets()
        if isinstance(obj, dict):
            return {k: "***" if SECRET_KEY_RE.search(str(k)) else self.redact(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact(v) for v in obj]
        if isinstance(obj, str):
            for s in secrets:
                obj = obj.replace(s, "***")
            return obj[:300]
        return obj

    def scrub(self, text: str) -> str:
        for s in hubkit.known_secrets():
            text = text.replace(s, "***")
        return text

    # ---------- napló ----------
    def audit(self, entry: dict) -> None:
        line = compact(entry)
        with self.lock, self.audit_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ---------- kimenet ----------
    def shape(self, tool: str, result: Any) -> tuple[str, str | None]:
        """Tömör JSON; a határ fölött levágja, a teljeset az out\\ mappába menti."""
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
        text = self.scrub(compact(result))
        limit = self.cfg.get("max_output_chars", 4000)
        if len(text) <= limit:
            return text, None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = self.out_dir / f"{stamp}_{tool}.json"
        path.write_text(text, "utf-8")
        short = {
            "ok": result.get("ok", True),
            "truncated": True,
            "chars": len(text),
            "path": str(path),
            "excerpt": text[: self.cfg.get("excerpt_chars", 600)],
        }
        return compact(short), str(path)

    # ---------- hívásburkoló ----------
    async def invoke(self, tool: str, plugin: str, level: str, fn: Callable, kwargs: dict, timeout: float | None = None) -> str:
        t0 = time.perf_counter()
        params = {k: v for k, v in kwargs.items() if not isinstance(v, Context)}
        for v in kwargs.values():
            if isinstance(v, Context):
                try:
                    self.sessions.add(v.session)
                except Exception:
                    pass
        status, result = "ok", None
        # folyamatjelző: a hívás 3 mp után megjelenik a lebegő ablakban
        pkey = f"call-{next(_counter)}"
        self._progress(pkey, tool, "fut")
        show = asyncio.get_running_loop().call_later(CALL_SHOW_AFTER_S, hubkit.show_widget)
        try:
            if level == "danger":
                msg = f"{tool}({compact(self.redact(params))[:200]})"
                ok = await asyncio.to_thread(self.approve, "Hub – jóváhagyás kell", msg, self.cfg.get("approval_timeout_s", 120))
                if not ok:
                    status, result = "denied", {"ok": False, "reason": "denied"}
            if result is None:
                timeout = timeout or self.cfg.get("tool_timeout_s", 120)
                if inspect.iscoroutinefunction(fn):
                    result = await asyncio.wait_for(fn(**kwargs), timeout)
                else:
                    result = await asyncio.wait_for(asyncio.to_thread(fn, **kwargs), timeout)
                if isinstance(result, dict) and result.get("ok") is False:
                    status = "fail"
        except (TimeoutError, asyncio.TimeoutError):
            status, result = "timeout", {"ok": False, "reason": "timeout"}
        except Exception as e:
            log.exception("Eszközhiba: %s", tool)
            status, result = "error", {"ok": False, "reason": "error", "error": f"{type(e).__name__}: {e}"[:500]}
        show.cancel()
        if time.perf_counter() - t0 < CALL_SHOW_AFTER_S:  # rövid hívás: nyom nélkül eltűnik
            (hubkit.PROGRESS_DIR / f"{pkey}.json").unlink(missing_ok=True)
        else:
            self._progress(pkey, tool, {"ok": "kész", "fail": "nem sikerült"}.get(status, status),
                           "done" if status in ("ok", "fail") else "error")
        text, path = self.shape(tool, result)
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "tool": tool,
            "plugin": plugin,
            "level": level,
            "params": self.redact(params),
            "result": status,
            "ms": round((time.perf_counter() - t0) * 1000),
            "chars": len(text),
        }
        if isinstance(result, dict) and status != "ok" and result.get("reason"):
            entry["reason"] = result["reason"]
        if path:
            entry["out"] = path
        self.audit(entry)
        return text

    @staticmethod
    def _progress(key: str, tool: str, detail: str, state: str = "running") -> None:
        try:
            hubkit.progress(key, f"Eszköz: {tool}", None, detail, state, keep_s=CALL_KEEP_S,
                            show_after_s=CALL_SHOW_AFTER_S, widget=False)
        except Exception:  # a folyamatjelző soha nem akaszthatja meg a hívást
            log.debug("progress írási hiba", exc_info=True)

    def register(self, fn: Callable, name: str, level: str, plugin: str, description: str, timeout: float | None = None) -> None:
        @functools.wraps(fn)
        async def wrapper(**kwargs):
            return await self.invoke(name, plugin, level, fn, kwargs, timeout)

        if name in self.mcp._tool_manager._tools:
            self.mcp.remove_tool(name)
        self.mcp.add_tool(wrapper, name=name, description=f"[{level}] {description}", structured_output=False)

    # ---------- pluginok ----------
    def import_plugin(self, plugin: str):
        path = self.plugins_dir / plugin / "plugin.py"
        modname = f"hubplugin_{plugin.lstrip('_')}_{next(_counter)}"
        spec = importlib.util.spec_from_file_location(modname, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def collect_tools(module) -> dict[str, Callable]:
        return {getattr(f, "__hub_tool__"): f for f in vars(module).values() if callable(f) and hasattr(f, "__hub_tool__")}

    @staticmethod
    def describe(fn: Callable) -> str:
        return (inspect.getdoc(fn) or "").strip().splitlines()[0][:120] if inspect.getdoc(fn) else ""

    def load_plugin(self, plugin: str) -> dict:
        """Betölti és regisztrálja a plugin eszközeit. Hiba esetén nem regisztrál semmit."""
        if not NAME_RE.match(plugin):
            return {"ok": False, "reason": "bad_name"}
        manifest = self.manifests().get(plugin)
        if not manifest:
            return {"ok": False, "reason": "not_found"}
        if "error" in manifest:
            return {"ok": False, "reason": "bad_manifest", "error": manifest["error"]}
        try:
            module = self.import_plugin(plugin)
        except Exception as e:
            return {"ok": False, "reason": "import_error", "error": f"{type(e).__name__}: {e}"[:500]}
        found = self.collect_tools(module)
        levels = {t["name"]: norm_level(t.get("level")) for t in manifest.get("tools", [])}
        missing = [t for t in levels if t not in found]
        prefix = tool_prefix(plugin)
        bad = [t for t in set(levels) | set(found) if not t.startswith(prefix)]
        if missing or bad:
            return {"ok": False, "reason": "manifest_mismatch", "missing": missing, "bad_prefix": bad}
        with self.lock:
            self.unload_plugin(plugin)
            tools = {}
            for name, fn in found.items():
                level = levels.get(name, "danger")  # manifestben nem szereplő eszköz → danger
                desc = self.describe(fn)
                self.register(fn, name, level, plugin, desc)
                tools[name] = {"level": level, "desc": desc}
            self.loaded[plugin] = {"manifest": manifest, "tools": tools, "module": module}
        log.info("Plugin betöltve: %s (%d eszköz)", plugin, len(tools))
        return {"ok": True, "plugin": plugin, "tools": sorted(tools)}

    def unload_plugin(self, plugin: str) -> None:
        with self.lock:
            info = self.loaded.pop(plugin, None)
            hook = getattr((info or {}).get("module"), "__hub_unload__", None)
            if callable(hook):  # a plugin itt zárhatja le a böngészőt, szálakat stb.
                try:
                    hook()
                except Exception:
                    log.exception("Leállítási hiba: %s", plugin)
            for name in (info or {}).get("tools", {}):
                if name in self.mcp._tool_manager._tools:
                    self.mcp.remove_tool(name)

    def load_enabled(self) -> None:
        for plugin in self.manifests():
            if self.is_enabled(plugin):
                res = self.load_plugin(plugin)
                if not res["ok"]:
                    log.error("Nem tölthető be: %s → %s", plugin, res)

    async def notify_tools_changed(self) -> None:
        for s in list(self.sessions):
            try:
                await s.send_tool_list_changed()
            except Exception:
                self.sessions.discard(s)
