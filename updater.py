"""AI HUB frissítő: python updater.py check | apply | ui

- check: van-e újabb kiadás a GitHubon (config.json → update.repo, pl. "fferi16/ai-hub")
- apply: letölti a legújabb kiadás forrását, leállítja a hubot, felülírja a programfájlokat,
         a felhasználói adatokat megtartja (.venv, logs, out, a config.json beállításai),
         a megszűnt programfájlokat törli, telepíti a megváltozott csomagokat, majd újraindítja a hubot
- ui:    ellenőriz, és ha van új verzió, rákérdez (Igen/Nem ablak), majd frissít

Privát repóhoz azonosítás kell: GITHUB_TOKEN környezeti változó, bejelentkezett `gh` CLI,
vagy a Windows Hitelesítőadat-kezelőben a hub/github_token kulcs. Fejlesztői (git) példányt nem ír felül.
"""
import ctypes
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for _s in (sys.stdout, sys.stderr):  # a Windows-konzol (cp1250) ne bukjon el az ékezeteken / nyilakon
    if _s is not None and hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")
LOG = ROOT / "logs" / "update.log"
CHECK_FILE = ROOT / "out" / "update_check.json"
LIST_FILE = ROOT / ".installed_files"
KEEP_DIRS = {".venv", "logs", "out", ".git", "__pycache__"}
UNINS = re.compile(r"unins\d{3}\.(exe|dat|msg)", re.I)  # az Inno Setup eltávolítója
NOWIN = 0x08000000


def log(msg: str) -> None:
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    print(msg)


def note(pct, detail, state="running"):
    try:
        import hubkit
        hubkit.progress("hub-update", "AI HUB frissítése", pct, detail, state, keep_s=300)
    except Exception:  # noqa: BLE001
        pass


def version_tuple(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in v.strip().lstrip("vV").split("."))


def current() -> str:
    p = ROOT / "VERSION"
    return p.read_text().strip() if p.exists() else "0.0.0"


def config() -> dict:
    return json.loads((ROOT / "config.json").read_text("utf-8"))


def repo() -> str:
    return config().get("update", {}).get("repo", "")


def token() -> str | None:
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    gh = shutil.which("gh")
    if gh:
        r = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, creationflags=NOWIN)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    try:
        import keyring
        return keyring.get_password("hub", "github_token")
    except Exception:  # noqa: BLE001
        return None


def _get(url: str, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "ai-hub-updater"})
    tok = token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def check() -> dict:
    rp = repo()
    if not rp:
        return {"ok": False, "reason": "nincs beállítva frissítési forrás (config.json → update.repo)"}
    try:
        rel = json.loads(_get(f"https://api.github.com/repos/{rp}/releases/latest"))
    except urllib.error.HTTPError as e:
        hint = " – privát repó? Kell GitHub-azonosítás (gh auth login)" if e.code == 404 else ""
        return {"ok": False, "reason": f"GitHub: HTTP {e.code}{hint}", "repo": rp}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"nincs kapcsolat a GitHubbal ({type(e).__name__})", "repo": rp}
    latest = rel.get("tag_name", "").lstrip("vV")
    res = {"ok": True, "repo": rp, "current": current(), "latest": latest,
           "update_available": version_tuple(latest) > version_tuple(current()),
           "notes": (rel.get("body") or "")[:1500], "url": rel.get("html_url"), "zip": rel.get("zipball_url")}
    CHECK_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECK_FILE.write_text(json.dumps({**res, "checked": time.time()}, ensure_ascii=False), "utf-8")
    return res


# ---------------- alkalmazás ----------------
def hub_processes() -> list[int]:
    """A hubhoz tartozó python-folyamatok (hub.py, widget.py) – csak ebből a mappából."""
    ps = (f"Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | Where-Object {{ $_.CommandLine -like "
          f"'*{str(ROOT)}*' -and ($_.CommandLine -like '*hub.py*' -or $_.CommandLine -like '*widget.py*') }} | "
          f"ForEach-Object {{ $_.ProcessId }}")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, creationflags=NOWIN)
    return [int(x) for x in r.stdout.split() if x.isdigit() and int(x) != os.getpid()]


def stop_hub() -> int:
    pids = hub_processes()
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, creationflags=NOWIN)
    time.sleep(1.5 if pids else 0)
    return len(pids)


def start_hub() -> None:
    exe = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    subprocess.Popen([str(exe), str(ROOT / "hub.py")], cwd=str(ROOT), creationflags=NOWIN, close_fds=True)


def merge(old, new):
    """A régi (felhasználói) értékek nyernek, az új kulcsok bekerülnek."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = dict(new)
        for k, v in old.items():
            out[k] = merge(v, new[k]) if k in new else v
        return out
    return old


def skip(rel: Path) -> bool:
    """Felhasználói adat vagy a telepítő saját fájlja (unins000.*) – ezekhez a frissítő nem nyúl."""
    parts = rel.parts
    return (parts[0] in KEEP_DIRS or "__pycache__" in parts or rel.name == ".tested"
            or rel.as_posix() in ("config.json", ".installed_files") or bool(UNINS.fullmatch(rel.name)))


def apply(force_dev: bool = False) -> dict:
    if (ROOT / ".git").exists() and not force_dev:
        return {"ok": False, "reason": "ez fejlesztői (git) példány – itt `git pull` a frissítés"}
    info = check()
    if not info.get("ok"):
        return info
    if not info["update_available"]:
        return {"ok": True, "updated": False, "current": info["current"], "reason": "már a legújabb verzió fut"}
    log(f"Frissítés: {info['current']} → {info['latest']} ({info['repo']})")
    note(5, f"letöltés: v{info['latest']}")
    data = _get(info["zip"], accept="application/vnd.github+json")
    with tempfile.TemporaryDirectory() as td:
        zipfile.ZipFile(io.BytesIO(data)).extractall(td)
        src = next(Path(td).iterdir())  # a GitHub-zip egyetlen gyökérmappája
        new_files = sorted(p.relative_to(src) for p in src.rglob("*") if p.is_file() and not skip(p.relative_to(src)))
        old_reqs = _deps_signature()
        note(30, "a hub leállítása")
        stopped = stop_hub()
        note(40, f"fájlok cseréje ({len(new_files)} db)")
        for rel in new_files:
            dst = ROOT / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dst)
        removed = 0
        if LIST_FILE.exists():
            keep = {r.as_posix() for r in new_files}
            for line in LIST_FILE.read_text("utf-8").splitlines():
                if line and line not in keep and not skip(Path(line)) and (ROOT / line).is_file():
                    (ROOT / line).unlink()
                    removed += 1
        LIST_FILE.write_text("\n".join(r.as_posix() for r in new_files), "utf-8")
        new_cfg = json.loads((src / "config.json").read_text("utf-8"))
        old_cfg = config()
        (ROOT / "config.json").write_text(json.dumps(merge(old_cfg, new_cfg), ensure_ascii=False, indent=2) + "\n", "utf-8")
    deps_ok = True
    if _deps_signature() != old_reqs:
        note(70, "új csomagok telepítése")
        deps_ok = subprocess.run([str(ROOT / ".venv" / "Scripts" / "python.exe"), str(ROOT / "deps.py")],
                                 creationflags=NOWIN).returncode == 0
    note(95, "a hub újraindítása")
    start_hub()
    log(f"Kész: v{info['latest']} (fájlok: {len(new_files)}, törölt: {removed}, csomagok: {'ok' if deps_ok else 'HIBA'})")
    note(100, f"v{info['latest']} fut", "done" if deps_ok else "error")
    return {"ok": True, "updated": True, "from": info["current"], "to": info["latest"], "files": len(new_files),
            "removed": removed, "hub_restarted": True, "was_running": stopped > 0, "deps_ok": deps_ok}


def _deps_signature() -> str:
    parts = [(ROOT / "requirements.txt").read_text("utf-8") if (ROOT / "requirements.txt").exists() else ""]
    parts += [json.dumps(json.loads(m.read_text("utf-8")).get("deps", [])) for m in sorted((ROOT / "plugins").glob("*/manifest.json"))]
    return "\n".join(parts)


def background_check(max_age_s: int = 86400) -> None:
    """A hub indulásakor: naponta egyszer ellenőriz, és értesít, ha van új verzió (nem frissít magától)."""
    try:
        if not config().get("update", {}).get("check_daily", True):
            return
        last = json.loads(CHECK_FILE.read_text("utf-8")).get("checked", 0) if CHECK_FILE.exists() else 0
        if time.time() - last < max_age_s:
            return
        res = check()
        if res.get("ok") and res.get("update_available"):
            from windows_toasts import Toast, WindowsToaster
            WindowsToaster("AI HUB").show_toast(Toast([f"Új AI HUB verzió: v{res['latest']}",
                                                      "Tálcaikon → „Frissítés keresése”, vagy a Start menü: AI HUB frissítése"]))
    except Exception:  # noqa: BLE001
        pass


def ui() -> None:
    MB = ctypes.windll.user32.MessageBoxW
    res = check()
    if not res.get("ok"):
        MB(None, res.get("reason", "ismeretlen hiba"), "AI HUB frissítés", 0x30)
        return
    if not res["update_available"]:
        MB(None, f"A legújabb verzió fut: v{res['current']}", "AI HUB frissítés", 0x40)
        return
    notes = res["notes"][:600] or "(nincs leírás)"
    if MB(None, f"Új verzió: v{res['latest']} (most: v{res['current']})\n\n{notes}\n\nFrissítsem most? "
                "A hub pár másodpercre leáll.", "AI HUB frissítés", 0x24) != 6:
        return
    out = apply()
    MB(None, (f"Kész: v{out['to']} fut." if out.get("updated") else out.get("reason", "nem sikerült")), "AI HUB frissítés",
       0x40 if out.get("ok") else 0x10)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ui"
    if cmd == "check":
        print(json.dumps(check(), ensure_ascii=False, indent=1))
    elif cmd == "apply":
        print(json.dumps(apply("--force-dev" in sys.argv), ensure_ascii=False, indent=1))
    else:
        ui()
