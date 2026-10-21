"""Gyors út: a gyakori hangparancsok helyben, a Claude nélkül (~0,1–0,5 s a 3–8 s helyett).

  „Nyisd meg / indítsd el …"      → system_open (csak ha a célpont biztos: weboldal, fájl/mappa, becenév,
                                     vagy a program neve pontosan / az elejéig egyezik)
  „hangosabban / halkabban / némítsd / hangerő 40" → system_volume
  „hány óra van / milyen nap van ma"               → helyben
A végrehajtás a Hubon át megy (napló, jogosultsági szintek érvényesek). Ha nem biztos, None → a Claude-hoz megy.
"""
import asyncio
import json
import re
import time
from pathlib import Path

import hud
from engine import _norm

ROOT = Path(__file__).resolve().parents[2]
MONTHS = ["január", "február", "március", "április", "május", "június", "július", "augusztus", "szeptember",
          "október", "november", "december"]
DAYS = ["hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap"]
OPEN_RE = re.compile(r"^(?:kérlek,?\s+)?(?:nyisd meg|nyiss meg|nyisd|indítsd el|indítsd|mutasd meg|hozd be)\s+"
                     r"(?:nekem\s+)?(?:a\s+|az\s+)?(.+?)[\s.!?]*$", re.I)
ACC = re.compile(r"-?(?:ot|et|öt|at|t)$", re.I)    # tárgyrag: „Chrome-ot", „böngészőt", „Excelt"


def _hub_url() -> str:
    try:
        cfg = json.loads((ROOT / "config.json").read_text("utf-8"))
    except (OSError, ValueError):
        cfg = {}
    return f"http://{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 8765)}{cfg.get('path', '/mcp')}"


def hub_call(tool: str, args: dict, timeout: float = 20) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def run():
        async with streamablehttp_client(_hub_url()) as (r, w, _), ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
            text = "".join(getattr(c, "text", "") for c in res.content)
            try:
                return json.loads(text)
            except ValueError:
                return {"ok": False, "error": text[:200]}

    return asyncio.run(asyncio.wait_for(run(), timeout))


def _tool(name: str, args: dict) -> dict:
    hud.send("tool", name=name, input=args)
    try:
        res = hub_call(name, args)
    except Exception as e:  # noqa: BLE001 – a Hub nem érhető el
        res = {"ok": False, "error": f"a Hub nem válaszolt ({type(e).__name__})"}
    ok = res.get("ok", False)
    hud.send("result", name=name, ok=ok, text=json.dumps(res, ensure_ascii=False)[:160])
    return res


def _open_target(raw: str, sysp) -> str | None:
    """A kimondott célpontból a system_open-nek adható, BIZTOS cél (vagy None)."""
    cands = []
    for c in (ACC.sub("", raw), raw):
        c = c.strip(" ,.")
        if c and c not in cands:
            cands.append(c)
    for c in cands:
        r = sysp.resolve(c)
        if "error" in r:
            continue
        if r["kind"] != "app" or r["run"][0] in ("exe", "start"):    # weboldal, fájl, mappa, becenév
            return c
        if sysp._score(c, r["name"]) >= 80:                          # programnév: pontos vagy az elejéig egyező
            return c
        words = set(sysp._words(c))
        if words <= set(sysp._words(r["name"])) and not any(            # a név egész szava(i): „Chrome",
                words <= set(sysp._words(o)) for o in r.get("others", [])):  # de csak ha egyértelmű
            return c
    return None


# protokollok (mint a filmben): "voice": {"protocols": {"név": [lépések]}}; egy lépés: programnév / weboldal / útvonal,
# {"volume": 35}, vagy {"wait": 2}. Indítás: „munka protokoll", „indítsd a pihenő protokollt", „munka mód".
DEFAULT_PROTOCOLS = {"munka": ["Alnico", "Outlook", "Excel"], "pihenő": [{"volume": 35}, "Plex"]}
PROTO_RE = re.compile(r"(?:inditsd (?:el )?|kapcsold be |johet |aktivald )?(?:a |az )?(.+?) "
                      r"(?:protokoll|protokollt|mod|modot|uzemmod|uzemmodot)(?: inditasa| indul| be| jon)?")


def _protocols() -> dict:
    try:
        v = json.loads((ROOT / "config.json").read_text("utf-8")).get("voice", {}).get("protocols")
    except (OSError, ValueError):
        v = None
    return v if isinstance(v, dict) and v else DEFAULT_PROTOCOLS


def _run_protocol(name: str, steps: list) -> str:
    done, failed = 0, []
    for st in steps:
        if isinstance(st, dict) and "volume" in st:
            res = _tool("system_volume", {"level": int(st["volume"])})
        elif isinstance(st, dict) and "wait" in st:
            time.sleep(min(10.0, float(st["wait"])))
            continue
        else:
            res = _tool("system_open", {"target": str(st)})
            if res.get("ok"):
                hud.send("icon", target=str(st))
        if res.get("ok"):
            done += 1
        else:
            failed.append(str(st if not isinstance(st, dict) else "hangerő"))
    msg = f"{name.capitalize()} protokoll kész: {done} lépés."
    return msg + (f" Nem sikerült: {', '.join(failed)}." if failed else "")


def handle(command: str, sysp) -> str | None:
    """A felolvasandó válasz, ha helyben elintézhető; különben None."""
    text = command.strip()
    p = " ".join(re.findall(r"\w+", _norm(text)))
    if re.fullmatch(r"(?:kerek |johet (?:egy )?)?(?:allapotjelentes|rendszerallapot|diagnosztika|gep ?allapot(?:a)?|"
                    r"hogy van a gep|milyen allapotban van a gep|statusz(?:jelentes)?)", p):
        res = _tool("system_status", {})
        if not res.get("ok"):
            return "Most nem érem el a gép adatait."
        hud.send("status", data=res)
        parts = [f"Processzor {res['cpu_pct']}, memória {res['ram_pct']} százalék"]
        g = res.get("gpu")
        if g:
            parts.append(f"videókártya {g['pct']} százalék, {g['temp_c']} fok")
        b = res.get("battery")
        if b:
            parts.append(f"akku {b['pct']} százalék" + (", töltőn" if b["plugged"] else ""))
        parts.append(f"a C meghajtón {res['disk_c']['free_gb']} gigabájt szabad")
        if not res.get("online"):
            parts.append("nincs internet")
        return "Állapotjelentés: " + "; ".join(parts) + "."
    m = PROTO_RE.fullmatch(p)
    if m:
        protos = _protocols()
        hit = next((k for k in protos if _norm(k).replace(" ", "") == m.group(1).replace(" ", "")), None)
        if hit is not None:
            return _run_protocol(hit, protos[hit] if isinstance(protos[hit], list) else [protos[hit]])
    if re.fullmatch(r"(mennyi az ido|hany ora( van)?( most)?|mennyi ido van)", p):
        t = time.localtime()
        return f"{t.tm_hour} óra {t.tm_min} perc van."
    if re.fullmatch(r"(milyen nap van( ma)?|hanyadika van( ma)?|mi a mai datum|mi a datum)", p):
        t = time.localtime()
        return f"Ma {t.tm_year}. {MONTHS[t.tm_mon - 1]} {t.tm_mday}., {DAYS[t.tm_wday]} van."
    vol = None
    if re.fullmatch(r"(hangosabban|hangosabb|hangositsd( fel)?( a hangot)?|nagyobb hangero|hangosits)", p):
        vol = {"change": 15}
    elif re.fullmatch(r"(halkabban|halkabb|halkitsd( le)?( a hangot)?|kisebb hangero|halkits)", p):
        vol = {"change": -15}
    elif re.fullmatch(r"(nemitsd( el)?( a hangot)?|nemitas|hang ki|kapcsold ki a hangot)", p):
        vol = {"mute": True}
    elif re.fullmatch(r"(nemitas ki|hang vissza|kapcsold vissza a hangot|kapcsold be a hangot)", p):
        vol = {"mute": False}
    else:
        m = re.fullmatch(r"(?:allitsd (?:a )?hangerot|hangero)(?: legyen)? (\d{1,3})(?: szazalek(?:ra)?|ra|re)?", p)
        if m and int(m.group(1)) <= 100:
            vol = {"level": int(m.group(1))}
    if vol is not None:
        res = _tool("system_volume", vol)
        if not res.get("ok"):
            return "Most nem tudom állítani a hangerőt."
        return "Némítva." if res.get("mute") else f"Hangerő: {res.get('level')} százalék."
    m = OPEN_RE.match(text)
    if m:
        target = _open_target(m.group(1), sysp)
        if target is None:
            return None                       # nem biztos → a Claude dönt
        res = _tool("system_open", {"target": target})
        if not res.get("ok"):
            return None
        kind = res.get("kind")
        if kind == "url":
            return f"Megnyitottam a böngészőben: {re.sub(r'^https?://(www[.])?', '', res.get('opened', target))}."
        if kind in ("folder", "file"):
            return f"Megnyitottam a {'mappát' if kind == 'folder' else 'fájlt'}: {Path(res.get('opened', target)).name}."
        return f"Megnyitottam: {res.get('opened', target)}."
    return None
