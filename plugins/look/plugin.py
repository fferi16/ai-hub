"""look – programok és weboldalak megnyitása, kiolvasása, kipróbálása, minél kevesebb tokennel.

Backendek: web (Playwright/Edge, Electron CDP-n), win (pywinauto UIA), ocr (képernyőkép + Windows OCR),
helyi VL-modell (Ollama). Minden backend-hívás egyetlen saját szálon fut (Playwright és COM szálhoz kötött).
"""
import sys
from pathlib import Path

from hubkit import tool

_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)
for _m in [m for m in sys.modules if m == "lookcore" or m.startswith("lookcore.")]:
    del sys.modules[_m]  # hot reload: mindig a friss kód töltődjön be

from lookcore.common import Worker, short  # noqa: E402
from lookcore.engine import Session  # noqa: E402

_worker = Worker()
_session = Session()


def _do(fn, *args, **kw) -> dict:
    try:
        return _worker.call(fn, *args, timeout=118, **kw)
    except Exception as e:  # rövid ok a teljes stacktrace helyett
        return {"ok": False, "reason": f"{type(e).__name__}: {short(str(e).splitlines()[0] if str(e) else '', 200)}"}


def __hub_unload__() -> None:
    try:
        _do(_session.close)
        from lookcore import vl

        vl.stop()
    finally:
        _worker.stop()


@tool
def look_open(target: str, electron: bool = False) -> dict:
    """URL, exe vagy ablakcím megnyitása/csatlakozás; számozott elemlistát ad vissza."""

    def go():
        _session.open(target, electron)
        return _session.describe()

    return _do(go)


@tool
def look_run(steps: list[dict], full: bool = False) -> dict:
    """Lépéssor egy hívásban; alapból csak a változást adja. Lépés: {"do":"click","el":3|"name":"OK"},
    {"do":"type","el":2,"text":"x"}, {"do":"key","keys":"Enter"}, {"do":"wait","value":"szöveg" vagy mp},
    {"do":"expect","text":"x"}, {"do":"open","target":"url"}, {"do":"snap"}, {"do":"ask","q":"..."};
    rövid alak is jó: {"click":3}, {"wait":2}."""
    return _do(_session.run, steps, full)


@tool
def look_snap(el: int = 0, region: str = "", max_px: int = 800) -> dict:
    """Kép CSAK kérésre: elem vagy régió (x,y,w,h), kicsinyítve, elemszámokkal; csak az útvonalat adja."""
    return _do(_session.snap, el or None, region or None, max_px)


@tool
def look_ask(question: str, el: int = 0, region: str = "", decisive: bool = False, compare: str = "") -> dict:
    """Kérdés a képernyőről a helyi VL-modellnek (ingyen); escalate=true, ha Claude-nak kell megnéznie."""

    def go():
        from lookcore.vl import ask

        return ask(_session, question, el or None, region or None, decisive, compare)

    return _do(go)


@tool
def look_recipe(action: str, name: str = "") -> dict:
    """Recept: save <név> (utolsó sikeres run), run <név>, list."""
    return _do(_session.recipe, action, name)


@tool
def look_close() -> dict:
    """A megnyitott böngésző/ablak-kapcsolat lezárása."""
    _do(_session.close)
    return {"ok": True}
