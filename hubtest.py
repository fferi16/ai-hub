"""Plugin-tesztfuttató (külön folyamatban fut, hogy a hibás plugin ne döntse le a Hubot).

Használat: python hubtest.py <plugin-mappa>   → egy sor JSON a kimeneten

tests/cases.json formátuma:
[
  {"tool": "hello_say", "args": {"name": "Feri"}, "expect": {"ok": true, "text": "Szia, Feri!"}},
  {"tool": "hello_say", "args": {"name": "X"}, "contains": "X"},
  {"tool": "hello_fail", "args": {}, "raises": true}
]
- expect: dict esetén részhalmaz-egyezés (minden megadott kulcs egyezzen), egyébként pontos egyezés;
  számra feltétel is adható: {"kb": {"$lt": 150}}, {"n": {"$gt": 0}}
- contains: a tömör JSON-kimenet tartalmazza a szöveget
- raises: a hívás kivételt dob
A danger szintű eszközöket nem futtatja (jóváhagyás nélkül nem hívható).
"""
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import Hub, compact, norm_level  # noqa: E402


def matches(expected, actual) -> bool:
    if isinstance(expected, dict) and set(expected) <= {"$lt", "$gt"} and expected:
        return isinstance(actual, (int, float)) and all(
            actual < v if k == "$lt" else actual > v for k, v in expected.items()
        )
    if isinstance(expected, dict) and isinstance(actual, dict):
        return all(k in actual and matches(v, actual[k]) for k, v in expected.items())
    return expected == actual


HUB_ROOT = Path(__file__).resolve().parent


def _relocate(obj):
    """A tesztesetek C:\\HUB\\… útvonalait a tényleges telepítési mappára írja át (más gépen más helyen lehet)."""
    if isinstance(obj, dict):
        return {k: _relocate(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_relocate(v) for v in obj]
    if isinstance(obj, str) and obj[:7].upper() == "C:\\HUB\\":
        return str(HUB_ROOT) + obj[6:]
    return obj


def run(pdir: Path) -> dict:
    manifest = json.loads((pdir / "manifest.json").read_text("utf-8"))
    cases_path = pdir / "tests" / "cases.json"
    cases = _relocate(json.loads(cases_path.read_text("utf-8"))) if cases_path.exists() else []
    if not cases:
        return {"ok": False, "reason": "no_tests", "passed": 0, "failed": []}

    spec = importlib.util.spec_from_file_location("plugin_under_test", pdir / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = Hub.collect_tools(module)
    levels = {t["name"]: norm_level(t.get("level")) for t in manifest.get("tools", [])}

    passed, failed, skipped = 0, [], []
    for i, case in enumerate(cases, 1):
        name = case.get("tool")
        if name not in tools:
            failed.append({"case": i, "tool": name, "why": "nincs ilyen eszköz a plugin.py-ban"})
            continue
        if levels.get(name, "danger") == "danger":
            skipped.append({"case": i, "tool": name, "why": "danger szint"})
            continue
        try:
            result = tools[name](**case.get("args", {}))
            if not isinstance(result, dict):
                result = {"ok": True, "result": result}
        except Exception as e:
            if case.get("raises"):
                passed += 1
            else:
                failed.append({"case": i, "tool": name, "why": f"kivétel: {type(e).__name__}: {e}"[:300]})
            continue
        text = compact(result)
        why = None
        if case.get("raises"):
            why = "kivételt vártunk"
        elif "expect" in case and not matches(case["expect"], result):
            why = f"várt: {compact(case['expect'])[:150]}, kapott: {text[:150]}"
        elif "contains" in case and case["contains"] not in text:
            why = f"nem tartalmazza: {case['contains']!r}, kapott: {text[:150]}"
        if why:
            failed.append({"case": i, "tool": name, "why": why})
        else:
            passed += 1
    hook = getattr(module, "__hub_unload__", None)
    if callable(hook):
        hook()
    out = {"ok": not failed and passed > 0, "passed": passed, "failed": failed}
    if skipped:
        out["skipped"] = skipped
    return out


if __name__ == "__main__":
    import contextlib

    try:
        with contextlib.redirect_stdout(sys.stderr):  # a plugin print()-jei ne keveredjenek a JSON-ba
            res = run(Path(sys.argv[1]))
    except Exception as e:
        res = {"ok": False, "reason": "load_error", "error": f"{type(e).__name__}: {e}"[:500]}
    sys.stdout.write(compact(res))
