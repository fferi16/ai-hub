"""state – munkamenet-átadó jegyzet projektenként.

A munkamenet végén state_save: mi készült el, mi a következő lépés, döntések, érintett fájlok, nyitott kérdések.
A következő munkamenet elején state_load: csak a legutóbbi mentés + az azóta írt rövid jegyzetek (tokentakarékos).
Tárolás: out/state/<projekt>.jsonl (soronként egy bejegyzés; a frissítő megtartja, a git nem követi).
A projekt neve lehet mappaútvonal is (pl. C:\\HUB) – ilyenkor az utolsó mappanév számít.
"""
import json
import re
import time
from pathlib import Path

from hubkit import tool

STORE = Path(__file__).resolve().parents[2] / "out" / "state"
MAX_ENTRIES = 50     # projektenként ennyi bejegyzés marad meg
FIELD_CHARS = 1500   # mezőnkénti felső korlát
NOTE_CHARS = 400


def _key(project: str) -> str:
    name = re.split(r"[\\/]", project.strip().rstrip("\\/"))[-1].lower()
    name = re.sub(r"[^\w.-]+", "_", name).strip("._")[:60]
    return name


def _path(project: str) -> Path | None:
    k = _key(project)
    return STORE / f"{k}.jsonl" if k else None


def _items(text: str) -> list[str]:
    """';' vagy sortörés mentén listává bont, üres elemek nélkül."""
    return [s.strip(" -•\t")[:300] for s in re.split(r"[;\n]", text or "") if s.strip(" -•\t")][:20]


def _read(p: Path) -> list[dict]:
    if not p.is_file():
        return []
    out = []
    for line in p.read_text("utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _write(p: Path, entries: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries[-MAX_ENTRIES:]), "utf-8")
    tmp.replace(p)


def _age_h(ts: float) -> float:
    return round((time.time() - ts) / 3600, 1)


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _known() -> list[str]:
    return sorted(p.stem for p in STORE.glob("*.jsonl")) if STORE.is_dir() else []


@tool
def state_save(project: str, summary: str, next_steps: str = "", decisions: str = "", files: str = "",
               questions: str = "") -> dict:
    """Munkamenet végén: összefoglaló + következő lépések/döntések/fájlok/kérdések (';'-vel) mentése a projekthez."""
    p = _path(project)
    if not p:
        return {"ok": False, "reason": "üres projektnév"}
    if not summary.strip():
        return {"ok": False, "reason": "üres összefoglaló"}
    rec = {"ts": time.time(), "kind": "save", "summary": summary.strip()[:FIELD_CHARS]}
    for k, v in (("next", next_steps), ("decisions", decisions), ("files", files), ("questions", questions)):
        if items := _items(v):
            rec[k] = items
    entries = _read(p) + [rec]
    _write(p, entries)
    return {"ok": True, "project": p.stem, "saved": _stamp(rec["ts"]), "entries": min(len(entries), MAX_ENTRIES)}


@tool
def state_note(project: str, text: str) -> dict:
    """Munka közbeni rövid jegyzet a projekthez (a következő state_load a legutóbbi mentés után mutatja)."""
    p = _path(project)
    if not p or not text.strip():
        return {"ok": False, "reason": "üres projektnév vagy jegyzet"}
    entries = _read(p) + [{"ts": time.time(), "kind": "note", "text": text.strip()[:NOTE_CHARS]}]
    _write(p, entries)
    return {"ok": True, "project": p.stem, "entries": min(len(entries), MAX_ENTRIES)}


@tool
def state_load(project: str, history: int = 0) -> dict:
    """Munkamenet elején: a projekt legutóbbi mentése + az azóta írt jegyzetek; history=N a korábbi N összefoglaló."""
    p = _path(project)
    entries = _read(p) if p else []
    if not entries:
        known = _known()
        k = _key(project)
        similar = [n for n in known if k and (k in n or n in k)]
        return {"ok": False, "reason": f"nincs mentett állapot: {project}", "similar": similar or known[:10]}
    saves = [i for i, e in enumerate(entries) if e.get("kind") == "save"]
    last_i = saves[-1] if saves else -1
    out = {"ok": True, "project": p.stem}
    if last_i >= 0:
        last = entries[last_i]
        out.update({"saved": _stamp(last["ts"]), "age_h": _age_h(last["ts"]), "summary": last["summary"]})
        out.update({k: last[k] for k in ("next", "decisions", "files", "questions") if k in last})
    notes = [f"{_stamp(e['ts'])} {e['text']}" for e in entries[last_i + 1:] if e.get("kind") == "note"]
    if notes:
        out["notes"] = notes[-20:]
    n = max(0, min(int(history), 20))
    if n:
        older = [entries[i] for i in saves[:-1]][-n:]
        out["history"] = [f"{_stamp(e['ts'])} {e['summary'][:150]}" for e in reversed(older)]
    return out


@tool
def state_list() -> dict:
    """Projektek mentett állapottal: név, utolsó mentés ideje, bejegyzésszám, az összefoglaló eleje."""
    rows = []
    for name in _known():
        entries = _read(STORE / f"{name}.jsonl")
        if not entries:
            continue
        saves = [e for e in entries if e.get("kind") == "save"]
        last = entries[-1]
        rows.append({"p": name, "upd": _stamp(last["ts"]), "n": len(entries),
                     "s": saves[-1]["summary"][:80] if saves else ""})
    rows.sort(key=lambda r: r["upd"], reverse=True)
    return {"ok": True, "projects": rows}


@tool
def state_clear(project: str) -> dict:
    """A projekt állapotának törlése (biztonsági másolat: <projekt>.jsonl.bak, a következő törlésig)."""
    p = _path(project)
    if not p or not p.is_file():
        return {"ok": False, "reason": f"nincs mentett állapot: {project}"}
    p.replace(p.with_suffix(".jsonl.bak"))
    return {"ok": True, "project": p.stem, "backup": str(p.with_suffix(".jsonl.bak"))}
