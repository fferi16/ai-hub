"""alnico – a saját CAD-program (Alnico) vezérlése a helyi automatizálási felületén át.

Az Alnico (0.5.34-től) indításkor egy csak 127.0.0.1-en figyelő végpontot nyit, a portot és a tokent
a profilmappájába írja (%APPDATA%\\alnico-pdf\\automata.json). Ezen át a MEGNYITOTT rajz is elérhető:
  alnico_status  – fut-e, mi az aktív dokumentum, épp mit kérdez a parancssor
  alnico_drawing – az aktív rajz összefoglalója (rétegek, befoglaló, kijelölés, szerkesztések)
  alnico_open    – DXF/DWG megnyitása (ha az Alnico nem fut, elindítja)
  alnico_cmd     – sorok a parancssorba, mintha a felhasználó gépelné; vissza a kiírt sorok
A parancssor nyelve az Alnicóé: `súgó` = parancslista; pont: `100,200` vagy `100;200`, `@50,0`, `@100<90`;
különleges sorok: <enter> (alapérték / alakzat vége), <esc> (mégsem), <zar> (polivonal bezárása).
Sugár/hossz is PONTként megy (kör: középpont, majd `@150,0`); puszta számot az eszköz nem fogad el.
A most rajzolt elemek mentésig csak az Alnicóban vannak (alnico_drawing → rajzolt); a fájl változatlan.
Fájlszintű munkához (olvasás, darabjegyzék, írás) a dxf plugin való; ez az élő rajzhoz.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from hubkit import tool

DESC = Path(os.environ.get("ALNICO_AUTOMATA_FILE") or
            Path(os.environ.get("APPDATA", "")) / "alnico-pdf" / "automata.json")
EXE = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Alnico" / "Alnico.exe"
NOT_RUNNING = {"ok": False, "reason": "az Alnico nem fut, vagy ez a verziója még nem tud automatizálást",
               "hint": "alnico_open elindítja; automatizálás az Alnico 0.5.34-től van"}


def _desc() -> dict | None:
    try:
        d = json.loads(DESC.read_text("utf-8"))
        return d if d.get("port") and d.get("token") else None
    except (OSError, ValueError):
        return None


def _call(method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    d = _desc()
    if not d:
        return dict(NOT_RUNNING)
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(f"http://127.0.0.1:{int(d['port'])}{path}", data=data, method=method,
                                 headers={"X-Alnico-Token": d["token"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except ValueError:
            return {"ok": False, "reason": f"HTTP {e.code}"}
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        return {**NOT_RUNNING, "detail": f"{type(e).__name__}: {e}"[:150]}


@tool
def alnico_status() -> dict:
    """Fut-e az Alnico (automatizálással), mi az aktív dokumentum, van-e futó kérdés a parancssorban."""
    return _call("GET", "/allapot", timeout=8)


@tool
def alnico_drawing() -> dict:
    """Az Alnicóban megnyitott CAD-rajz összefoglalója: rétegek, befoglaló, egység, kijelölés, szerkesztések."""
    return _call("GET", "/rajz", timeout=15)


@tool
def alnico_open(path: str, wait_s: int = 60) -> dict:
    """DXF/DWG megnyitása az Alnicóban (élő munkához); ha nem fut, elindítja és megvárja az automatizálást."""
    p = Path(path).expanduser()
    if not p.is_file():
        return {"ok": False, "reason": f"nincs ilyen fájl: {path}"}
    if p.suffix.lower() not in (".dxf", ".dwg"):
        return {"ok": False, "reason": f"csak DXF/DWG: {p.suffix}"}
    p = p.resolve()
    if _call("GET", "/allapot", timeout=5).get("ok"):
        return _call("POST", "/megnyit", {"path": str(p)}, timeout=125)
    if not EXE.is_file():
        return {"ok": False, "reason": f"nincs telepítve az Alnico ({EXE})"}
    subprocess.Popen([str(EXE), str(p)], close_fds=True)  # a fájlt az indítás maga nyitja meg
    deadline = time.time() + max(5, min(int(wait_s), 110))
    while time.time() < deadline:
        time.sleep(1)
        st = _call("GET", "/allapot", timeout=5)
        akt = (st.get("aktiv") or {}).get("ut", "") if st.get("ok") else ""
        if akt and Path(akt).resolve() == p:
            return {"ok": True, "started": True, "dok": st["aktiv"]}
    return {"ok": False, "started": True, "reason": "az Alnico elindult, de az automatizálás nem jelentkezett",
            "hint": "régebbi Alnico-verzió? (0.5.34-től van) – a rajz ettől még nyitva van"}


@tool
def alnico_cmd(lines: list[str] | str, wait_ms: int = 40) -> dict:
    """Sorok az Alnico parancssorába (mint gépelve): pl. ['vonal','0,0','@1000,0','<esc>']; vissza a kiírt sorok."""
    if isinstance(lines, str):
        lines = lines.replace("\r", "").split("\n")  # csak sortörés: a ';' a koordinátában is lehet (100;200)
    lines = [str(s).strip() for s in lines if str(s).strip()]
    if not lines:
        return {"ok": False, "reason": "üres parancs"}
    if len(lines) > 500:
        return {"ok": False, "reason": "legfeljebb 500 sor egyszerre"}
    wait = max(10, min(int(wait_ms), 2000))
    return _call("POST", "/parancs", {"sorok": lines, "varMs": wait}, timeout=min(110, 5 + len(lines) * wait / 1000 * 3))
