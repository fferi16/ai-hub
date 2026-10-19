"""A look munkamenet: backend-választás, lépések futtatása, változásfigyelés, képek, receptek."""
import json
import re
import time

from .common import RECIPES, URL_RE, Deadline, config, save_image, short, similar


class StepError(Exception):
    pass


STEP_HELP = (
    'formátum: {"do":"click","el":3} | {"do":"click","name":"OK"} | {"do":"type","el":2,"text":"x"} | '
    '{"do":"key","keys":"Enter"} | {"do":"wait","value":"szöveg"|2} | {"do":"expect","text":"x"} | '
    '{"do":"open","target":"url"} | {"do":"snap"} | {"do":"ask","q":"kérdés"}; rövid alak: {"click":3}, {"wait":2}'
)

# rövid alak ({"click": 3}) → melyik mezőbe kerül az érték
_SHORT = {"open": "target", "type": "text", "key": "keys", "wait": "value", "expect": "text", "ask": "q", "snap": None}


def normalize_step(step) -> dict:
    """Rövid alak ({"click": 3}, {"wait": 2}) → teljes alak ({"do": ..., ...}); hibás lépésre példát ad."""
    if not isinstance(step, dict):
        raise StepError(f"a lépés objektum legyen, nem {type(step).__name__}; {STEP_HELP}")
    if "do" in step:
        return step
    keys = [k for k in step if k == "click" or k in _SHORT]
    if len(keys) != 1:
        raise StepError(f"hiányzik a 'do' mező; {STEP_HELP}")
    k = keys[0]
    val = step[k]
    rest = {x: v for x, v in step.items() if x != k}
    if k == "click":
        if isinstance(val, (list, tuple)):
            return {"do": "click", "at": list(val), **rest}
        if isinstance(val, int) and not isinstance(val, bool):
            return {"do": "click", "el": val, **rest}
        return {"do": "click", "name": str(val), **rest}
    field = _SHORT[k]
    keep = field and val is not None and val is not True  # {"snap": true} → nincs érték
    return {"do": k, **({field: val} if keep else {}), **rest}


class Session:
    def __init__(self):
        self.cfg = config()
        self.backend = None
        self.els: list[dict] = []
        self.target = ""
        self.electron = False
        self.last_ok_steps: list[dict] | None = None

    # ---------- backend ----------
    def _backend_for(self, target: str, electron: bool):
        if electron or URL_RE.match(target):
            from .web import Web

            return Web(self.cfg)
        from .win import Win

        return Win(self.cfg)

    def open(self, target: str, electron: bool = False) -> None:
        self.cfg = config()
        new = self._backend_for(target, electron)
        if self.backend is None or self.backend.kind != new.kind or electron or self.electron:
            self.close()
            self.backend = new
        self.backend.open(target, electron=electron)
        self.target, self.electron = target, electron
        self._maybe_ocr()
        self.backend.settle()

    def _maybe_ocr(self) -> None:
        """Ha nincs (vagy alig van) kiolvasható elemfa, vagy vászon az oldal: ocr backend."""
        from .ocr import Ocr, wants_ocr

        if isinstance(self.backend, Ocr):
            return
        els = self.backend.elements()
        if wants_ocr(self.backend, els, self.cfg):
            self.backend = Ocr(self.backend, self.cfg)

    def close(self) -> None:
        if self.backend:
            try:
                self.backend.close()
            except Exception:
                pass
        self.backend, self.els = None, []

    def need(self):
        if self.backend is None:
            raise StepError("nincs megnyitott cél – előbb look_open")
        return self.backend

    # ---------- kimenet ----------
    def listing(self) -> list[dict]:
        self.els = self.need().elements()
        return self.els

    def rows(self, els: list[dict]) -> list[list]:
        out = []
        for e in els[: self.cfg["max_elements"]]:
            row = [e["n"], e["role"], e["name"]]
            if "value" in e:
                row.append(e["value"])
            out.append(row)
        return out

    def describe(self) -> dict:
        b = self.need()
        els = self.listing()
        res = {"ok": True, "backend": b.kind, "window": b.title(), "elements": self.rows(els)}
        if len(els) > self.cfg["max_elements"]:
            res["more"] = len(els) - self.cfg["max_elements"]
        errs = b.drain_errors() + b.drain_dialogs()
        if errs:
            res["errors"] = errs
        return res

    # ---------- elemfeloldás ----------
    def resolve(self, step: dict) -> tuple[object, dict]:
        """→ (backend-kezelő, recept-alak {name, role})"""
        b = self.need()
        if "el" in step:
            n = int(step["el"])
            el = next((e for e in self.els if e["n"] == n), None)
            if el is None:
                raise StepError(f"az el {n} nem található" + (f", {similar(str(step.get('name', '')), self.els)}" if step.get("name") else ""))
            return b.locate(el), {"name": el["name"], "role": el["role"]}
        name, role = step.get("name", ""), step.get("role", "")
        if not name and not role:
            raise StepError("hiányzik az elem: el, name vagy role")
        cands = [e for e in self.els if (not role or e["role"] == role)]
        exact = [e for e in cands if name and e["name"].lower() == name.lower()]
        if exact:
            return b.locate(exact[0]), {"name": exact[0]["name"], "role": exact[0]["role"]}
        loc = b.locate(None, name=name, role=role)
        if loc is None:
            raise StepError(f"'{name}' nem található" + (f", {similar(name, self.els)}" if self.els else ""))
        return loc, {"name": name, "role": role}

    # ---------- futtatás ----------
    def run(self, steps: list[dict], full: bool = False) -> dict:
        if isinstance(steps, dict):
            steps = [steps]
        cfg = self.cfg
        tmo = int(cfg["step_timeout_s"] * 1000)
        dl = Deadline(cfg["run_timeout_s"])
        if self.backend is not None and not self.els:
            self.listing()
        before = {(e["role"], e["name"]) for e in self.els}
        title0 = self.backend.title() if self.backend else ""
        expects, images, answers, recorded = [], [], [], []
        for i, step in enumerate(steps, 1):
            dl.check()
            try:
                step = normalize_step(step)
            except StepError as e:
                return self._fail(i, step if isinstance(step, dict) else {}, str(e))
            do = step.get("do")
            rec = {k: v for k, v in step.items() if k != "el"}
            try:
                guard = step.get("window")
                if guard and do in ("click", "type", "key") and guard.lower() not in self.need().title().lower():
                    raise StepError(f"az ablak címe nem tartalmazza: '{guard}' (most: '{self.backend.title()}') – lépés kihagyva")
                if do == "open":
                    self.open(step["target"], bool(step.get("electron")))
                    self.listing()
                elif do == "click" and "at" in step:  # koordinátás kattintás (pl. look_ask "point")
                    x, y = step["at"]
                    self.need().click_xy(float(x), float(y))
                    self.backend.settle()
                elif do in ("click", "type"):
                    loc, ref = self.resolve(step)
                    rec.update(ref)
                    if do == "click":
                        self.backend.click(loc, tmo)
                    else:
                        self.backend.type(loc, str(step.get("text", "")), tmo)
                    self.backend.settle()
                elif do == "key":
                    for _ in range(max(1, min(int(step.get("repeat", 1)), 50))):
                        self.need().key(step["keys"], tmo)
                    self.backend.settle()
                elif do == "wait":
                    val = step.get("value")
                    secs = val if isinstance(val, (int, float)) and not isinstance(val, bool) else None
                    if step.get("for") == "time" or secs is not None:
                        time.sleep(max(0.0, min(float(val if val is not None else 1), 10)))
                    elif val in (None, ""):
                        raise StepError("a wait-hez value kell: szöveg (megjelenésre vár) vagy szám (másodperc, max 10)")
                    else:
                        self.need().wait_text(str(val), int(step.get("timeout_s", cfg["step_timeout_s"]) * 1000))
                elif do == "expect":
                    text = self.need().text()
                    found = str(step["text"]).lower() in text.lower()
                    expects.append({"text": step["text"], "found": found})
                elif do == "snap":
                    images.append(self.snap(step.get("el"), step.get("region"), int(step.get("max", 800)))["image"])
                elif do == "ask":
                    from .vl import ask

                    a = ask(self, step["q"], step.get("el"), step.get("region"), bool(step.get("decisive")), step.get("compare", ""))
                    answers.append(a)
                else:
                    raise StepError(f"ismeretlen lépés: {do!r}; {STEP_HELP}")
            except StepError as e:
                return self._fail(i, step, str(e))
            except KeyError as e:
                return self._fail(i, step, f"hiányzó mező a(z) {do} lépésben: {e.args[0]!r}; {STEP_HELP}")
            except TimeoutError as e:
                return self._fail(i, step, f"időtúllépés: {short(e, 120)}")
            except Exception as e:
                return self._fail(i, step, f"{type(e).__name__}: {short(e, 160)}")
            recorded.append(rec)
            if do in ("click", "type", "key", "open"):
                self.listing()  # a további el-hivatkozások a friss listára vonatkoznak

        if not any(s.get("do") == "open" for s in recorded) and self.target:
            recorded.insert(0, {"do": "open", "target": self.target, **({"electron": True} if self.electron else {})})
        self.last_ok_steps = recorded
        b = self.need()
        res = {"ok": all(x["found"] for x in expects), "backend": b.kind, "window": b.title()}
        if full:
            res["elements"] = self.rows(self.els)
        else:
            after = [e for e in self.els if (e["role"], e["name"]) not in before]
            gone = len(before - {(e["role"], e["name"]) for e in self.els})
            changed = [f"új: [{e['n']}] {e['role']} '{e['name']}'" for e in after[:10]]
            if len(after) > 10:
                changed.append(f"+{len(after) - 10} új elem")
            if gone:
                changed.append(f"eltűnt: {gone} elem")
            if res["window"] != title0:
                changed.append(f"cím: '{res['window']}'")
            changed += [f"dialógus: {d}" for d in b.drain_dialogs()]
            res["changed"] = changed
        errs = b.drain_errors()
        if errs:
            res["errors"] = errs
        if expects:
            res["expect"] = expects
        if images:
            res["image"] = images[0] if len(images) == 1 else images
        if answers:
            res["ask"] = answers[0] if len(answers) == 1 else answers
            if any(a.get("escalate") for a in answers):
                res["escalate"] = True
        return res

    def _fail(self, i: int, step: dict, why: str) -> dict:
        res = {"ok": False, "step": i, "do": step.get("do"), "reason": why}
        if "el" in step or "name" in step:
            hint = similar(str(step.get("name") or ""), self.els) if step.get("name") else ""
            if hint and hint not in why:
                res["hint"] = hint
        if self.backend:
            errs = self.backend.drain_errors()
            if errs:
                res["errors"] = errs
        return res

    # ---------- kép ----------
    def snap(self, el=None, region=None, max_px: int = 800) -> dict:
        b = self.need()
        if isinstance(region, str) and region:
            region = [float(v) for v in region.split(",")]
        labels = None
        if el:
            loc, _ = self.resolve({"el": el})
            img = b.screenshot(loc=loc)
            tag = f"el{el}"
        else:
            img = b.screenshot(region=region or None)
            tag = "region" if region else "full"
            if not region:
                labels = [(e["n"], b.image_box(e)) for e in self.els if b.image_box(e)]
        return {"ok": True, **save_image(img, tag, max_px, labels)}

    # ---------- receptek ----------
    def recipe(self, action: str, name: str = "") -> dict:
        RECIPES.mkdir(parents=True, exist_ok=True)
        if action == "list":
            return {"ok": True, "recipes": sorted(p.stem for p in RECIPES.glob("*.json"))}
        if not re.match(r"^[\w\-]{1,40}$", name or ""):
            return {"ok": False, "reason": "bad_name", "hint": "betű, szám, _ vagy -, max 40"}
        path = RECIPES / f"{name}.json"
        if action == "save":
            if not self.last_ok_steps:
                return {"ok": False, "reason": "nincs sikeres look_run, amit menteni lehetne"}
            path.write_text(json.dumps(self.last_ok_steps, ensure_ascii=False, indent=1), "utf-8")
            return {"ok": True, "recipe": name, "steps": len(self.last_ok_steps)}
        if action == "run":
            if not path.exists():
                return {"ok": False, "reason": "nincs ilyen recept"}
            steps = json.loads(path.read_text("utf-8"))
            res = self.run(steps)
            if res.get("ok") and not res.get("errors"):
                return {"ok": True, "recipe": name}
            res["recipe"] = name
            return res
        return {"ok": False, "reason": "action: save | run | list"}
