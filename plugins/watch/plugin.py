"""watch – Mappafigyelő: új fájl érkezésekor automatikus művelet, folyamatjelzővel

Műveletek (action):
- "log"           csak naplózza az új fájlt (watch_events) – próbához
- "media"         videó/hang → media_transcribe + media_frames
- "media+summary" mint a media, és a kész leiratból helyi tananyag-összefoglaló (media_digest → <név>_digest.md)
A fájlt csak akkor dolgozza fel, ha a mérete settle_s másodpercig nem változik (kész a másolás/letöltés).
A már meglévő fájlokat hozzáadáskor ismertnek veszi (process_existing=True esetén feldolgozza).
copy_to: ide másolja a kész leiratot (.txt) és összefoglalót (.md) – pl. maga a figyelt mappa.
A hub 15 mp-enként átnézi a szabályokat (háttérszál); watch_scan azonnal futtat egy kört.
"""
import fnmatch
import importlib.util
import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from hubkit import progress, secret, tool  # noqa: F401

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "out" / "watch"
STATE = OUT / "state.json"
EVENTS = OUT / "events.jsonl"
ACTIONS = ("log", "media", "media+summary")
INTERVAL_S = 15
_lock = threading.RLock()
_stop = threading.Event()
_bridges: dict[str, object] = {}


# ---------------- állapot ----------------
def _load() -> dict:
    try:
        return json.loads(STATE.read_text("utf-8"))
    except (OSError, ValueError):
        return {"rules": []}


def _save(st: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")
    os.replace(tmp, STATE)


def _event(rule: dict, kind: str, **kw) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "rule": rule["id"], "event": kind, **kw}
    with EVENTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _bridge(plugin: str):
    """Másik plugin függvényeinek elérése (saját modulpéldány; a pluginok állapotmentesek)."""
    if plugin not in _bridges:
        spec = importlib.util.spec_from_file_location(f"watch_bridge_{plugin}", ROOT / "plugins" / plugin / "plugin.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _bridges[plugin] = mod
    return _bridges[plugin]


def _note(rule: dict, key: str, title: str, detail: str, state: str = "done") -> None:
    if "_test" in rule["folder"]:
        return
    try:
        progress(f"watch-{rule['id']}-{key}", title, None if state == "running" else 100, detail, state, keep_s=900)
    except Exception:  # noqa: BLE001
        pass


# ---------------- egy kör ----------------
def _files(rule: dict) -> list[Path]:
    folder = Path(rule["folder"])
    if not folder.is_dir():
        return []
    pats = [p.strip() for p in rule["pattern"].split(";") if p.strip()]
    return sorted(p for p in folder.iterdir() if p.is_file() and any(fnmatch.fnmatch(p.name.lower(), x.lower()) for x in pats))


def _trigger(rule: dict, f: Path) -> None:
    act = rule["action"]
    _event(rule, "new_file", file=f.name, action=act)
    if act == "log":
        return
    media = _bridge("media")
    tr = media.media_transcribe(str(f))
    fr = media.media_frames(str(f))
    _event(rule, "started", file=f.name, transcribe=tr.get("job"), frames=fr.get("job"), ok=tr.get("ok") and fr.get("ok"))
    _note(rule, f.stem[:40], f"Új fájl: {f.name}", "leirat és képkockák elindítva")
    if tr.get("ok"):
        rule.setdefault("pending", []).append({"file": f.name, "media_job": tr["job"], "step": "transcribe"})


def _advance(rule: dict) -> None:
    """Folyamatban lévő láncok léptetése: kész leirat → másolás / összefoglaló; kész összefoglaló → másolás."""
    keep = []
    for pj in rule.get("pending", []):
        if pj["step"] == "transcribe":
            st = _job_status(ROOT / "out" / "media" / "jobs" / pj["media_job"])
            if st.get("state") == "error":
                _event(rule, "failed", file=pj["file"], step="transcribe", error=st.get("error"))
                continue
            if st.get("state") != "done":
                keep.append(pj)
                continue
            _copy(rule, st.get("txt"))
            _event(rule, "transcript_done", file=pj["file"], txt=st.get("txt"))
            if rule["action"] == "media+summary":
                r = _bridge("media").media_digest(st["txt"])
                if r.get("ok") and r.get("cached"):  # már volt friss összefoglaló
                    _copy(rule, r.get("md"))
                    _event(rule, "summary_done", file=pj["file"], md=r.get("md"))
                elif r.get("ok"):
                    keep.append({**pj, "step": "summary", "local_job": r["job"]})
                    _event(rule, "summary_started", file=pj["file"], job=r["job"])
                else:
                    _event(rule, "failed", file=pj["file"], step="summary", error=r.get("reason"))
        elif pj["step"] == "summary":
            st = _job_status(ROOT / "out" / "local" / "jobs" / pj["local_job"])
            if st.get("state") == "error":
                _event(rule, "failed", file=pj["file"], step="summary", error=st.get("error"))
            elif st.get("state") == "done":
                _copy(rule, st.get("md"))
                _event(rule, "summary_done", file=pj["file"], md=st.get("md"))
                _note(rule, pj["file"][:40] + "-sum", f"Összefoglaló kész: {pj['file']}", "a .md elkészült")
            else:
                keep.append(pj)
    rule["pending"] = keep


def _job_status(jdir: Path) -> dict:
    try:
        return json.loads((jdir / "status.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _copy(rule: dict, path: str | None) -> None:
    if rule.get("copy_to") and path and Path(path).is_file():
        dst = Path(rule["copy_to"])
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dst / Path(path).name)


def _scan(only: str = "all", folder: str = "") -> dict:
    with _lock:
        st = _load()
        now, fired = time.time(), []
        for rule in st["rules"]:
            if only != "all" and rule["id"] != only:
                continue
            if folder and Path(rule["folder"]) != Path(folder).expanduser().resolve():
                continue
            seen = set(rule.get("seen", []))
            cand = rule.setdefault("candidates", {})
            present = {f.name: f for f in _files(rule)}
            for name, f in present.items():
                if name in seen:
                    continue
                size = f.stat().st_size
                prev = cand.get(name)
                if prev is None or prev[0] != size:
                    cand[name] = [size, now]
                    prev = cand[name]
                if now - prev[1] >= rule.get("settle_s", 20):
                    try:
                        _trigger(rule, f)
                    except Exception as e:  # noqa: BLE001
                        _event(rule, "failed", file=name, step="trigger", error=f"{type(e).__name__}: {e}"[:200])
                    seen.add(name)
                    cand.pop(name, None)
                    fired.append(name)
            for name in list(cand):
                if name not in present:
                    cand.pop(name)
            try:
                _advance(rule)
            except Exception as e:  # noqa: BLE001
                _event(rule, "failed", step="advance", error=f"{type(e).__name__}: {e}"[:200])
            rule["seen"] = sorted(seen)
            rule["last_scan"] = datetime.now().isoformat(timespec="seconds")
        _save(st)
        return {"fired": fired}


def _loop():
    while not _stop.wait(INTERVAL_S):
        try:
            _scan()
        except Exception:  # noqa: BLE001 – a figyelő soha ne álljon le egy hibától
            pass


# A hub alatt (hubplugin_* modulnév) indul a háttérszál; a tesztfuttatóban nem.
if __name__.startswith("hubplugin_"):
    threading.Thread(target=_loop, name="watch", daemon=True).start()


def __hub_unload__():
    _stop.set()


# ---------------- eszközök ----------------
@tool
def watch_add(folder: str, pattern: str = "*.mp4;*.mkv;*.mov;*.m4a;*.mp3", action: str = "media",
              copy_to: str = "", settle_s: int = 20, process_existing: bool = False) -> dict:
    """Mappafigyelés: action = log | media | media+summary; pattern ';'-vel elválasztva; copy_to: ide másolja az eredményt."""
    p = Path(folder).expanduser()
    if not p.is_dir():
        return {"ok": False, "reason": f"nincs ilyen mappa: {folder}"}
    if action not in ACTIONS:
        return {"ok": False, "reason": f"ismeretlen action: {action} (lehet: {', '.join(ACTIONS)})"}
    with _lock:
        st = _load()
        for r in st["rules"]:
            if Path(r["folder"]) == p.resolve() and r["pattern"] == pattern and r["action"] == action:
                return {"ok": False, "reason": "ez a szabály már létezik", "id": r["id"]}
        rule = {"id": uuid.uuid4().hex[:6], "folder": str(p.resolve()), "pattern": pattern, "action": action,
                "copy_to": str(Path(copy_to).expanduser()) if copy_to else "", "settle_s": max(0, int(settle_s)),
                "created": datetime.now().isoformat(timespec="seconds"), "candidates": {}, "pending": [], "seen": []}
        existing = [f.name for f in _files(rule)]
        if not process_existing:
            rule["seen"] = sorted(existing)
        st["rules"].append(rule)
        _save(st)
    return {"ok": True, "id": rule["id"], "existing_files": len(existing),
            "existing": "feldolgozandó" if process_existing else "ismertnek vett"}


@tool
def watch_list() -> dict:
    """Figyelési szabályok: mappa, minta, művelet, ismert fájlok száma, folyamatban lévő láncok, utolsó kör."""
    with _lock:
        st = _load()
    return {"ok": True, "interval_s": INTERVAL_S, "rules": [
        {"id": r["id"], "folder": r["folder"], "pattern": r["pattern"], "action": r["action"], "copy_to": r.get("copy_to"),
         "seen": len(r.get("seen", [])), "waiting": list(r.get("candidates", {})), "pending": r.get("pending", []),
         "last_scan": r.get("last_scan")} for r in st["rules"]]}


@tool
def watch_remove(id: str = "", folder: str = "") -> dict:
    """Figyelési szabály törlése azonosító (vagy az összes adott mappára vonatkozó) alapján; a futó feldolgozások maradnak."""
    if not id and not folder:
        return {"ok": False, "reason": "adj meg id-t vagy folder-t"}
    fp = Path(folder).expanduser().resolve() if folder else None
    with _lock:
        st = _load()
        n = len(st["rules"])
        st["rules"] = [r for r in st["rules"] if not ((id and r["id"] == id) or (fp and Path(r["folder"]) == fp))]
        if len(st["rules"]) == n:
            return {"ok": False, "reason": f"nincs ilyen szabály: {id or folder}"}
        _save(st)
    return {"ok": True, "removed": n - len(st["rules"]), "rules": len(st["rules"])}


@tool
def watch_scan(id: str = "all", folder: str = "") -> dict:
    """Azonnali kör (nem kell várni a 15 mp-es ciklusra): új fájlok indítása és a láncok léptetése; id/folder szűkíthet."""
    with _lock:
        if id != "all" and not any(r["id"] == id for r in _load()["rules"]):
            return {"ok": False, "reason": f"nincs ilyen szabály: {id}"}
    return {"ok": True, **_scan(id, folder)}


@tool
def watch_events(n: int = 20, id: str = "") -> dict:
    """Az utolsó n figyelő-esemény (új fájl, indítás, kész leirat/összefoglaló, hiba); id szűrhet szabályra."""
    if not EVENTS.exists():
        return {"ok": True, "events": []}
    lines = EVENTS.read_text("utf-8").splitlines()
    evs = [json.loads(l) for l in lines if l.strip()]
    if id:
        evs = [e for e in evs if e.get("rule") == id]
    return {"ok": True, "events": evs[-max(1, min(int(n), 200)):]}
