"""Hangasszisztens háttere: a beszédből jött kérésre válasz, felolvasható formában.

backend = "claude": a Claude Code parancssori módja (claude -p), csak a Hub MCP-vel, csak hub-eszközökkel
          (a danger szintű hub-eszközök továbbra is Windows-jóváhagyást kérnek). 5 percen belül folytatja
          ugyanazt a beszélgetést (--resume), így lehet visszakérdezni.
backend = "local":  a helyi Ollama-modell (qwen3:8b) – ingyenes, eszközök nélkül.
Ha a claude nincs bejelentkezve / nem érhető el, magától a helyire vált, és ezt meg is mondja.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "out" / "voice"
LOG = OUT / "assistant.jsonl"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CONTINUE_S = 300

SYSTEM = ("Hangasszisztens vagy Feri Windows-gépén. A kérés beszédből jött (gépi leirat, lehetnek benne félrehallások – "
          "értsd a szándékot). Válaszolj magyarul, röviden (legfeljebb 3 mondat), felolvasható formában: ne használj "
          "Markdownt, felsorolást, kódot, URL-t vagy emojit. Ha a Hub eszközeivel elvégzel valamit, csak az eredményt "
          "mondd el egy mondatban. Ha nem tudod megcsinálni, mondd meg röviden, miért. Ez PÁRBESZÉD: ha a kérés nem "
          "egyértelmű, vagy választani kell, kérdezz vissza egy rövid kérdéssel (a válasz utolsó mondata legyen a "
          "kérdés, kérdőjellel) – a gép ilyenkor magától meghallgatja a választ. Ha nincs mit kérdezned, ne kérdezz. "
          "Weboldal TARTALMÁT (hírek, cikkek, adatok) a look_open eszközzel olvasd ki – más webes eszközöd nincs; "
          "a system_open csak megmutatja Ferinek a böngészőben, azt akkor használd, ha megnyitást kér.")

# "voice": {"persona": "jarvis"} (alap) – a filmbeli J.A.R.V.I.S. hangneme; "persona": "" = semleges
PERSONA = {"jarvis": (" Stílusod a filmbeli J.A.R.V.I.S.-é: udvarias, nyugodt, tömör, finom száraz humorral (csak ha "
                      "illik). Ferit néha a nevén szólítod, de nem minden válaszban. Elvégzett műveletnél elég egy rövid "
                      "nyugtázás (például: „Máris, Feri.” vagy „Kész, megnyitottam.”). Rossz hírt egyenesen, "
                      "nyugodtan mondasz. Sosem hízelegsz, sosem mentegetőzöl hosszan.")}


def system_prompt() -> str:
    try:
        persona = json.loads((ROOT / "config.json").read_text("utf-8")).get("voice", {}).get("persona", "jarvis")
    except (OSError, ValueError):
        persona = "jarvis"
    return SYSTEM + PERSONA.get(persona or "", "")


_session = {"id": None, "ts": 0.0}
_local_hist: list[dict] = []


class NotAvailable(Exception):
    pass


def _claude_exe() -> str | None:
    p = shutil.which("claude")
    if p:
        return p
    cand = Path.home() / ".local" / "bin" / "claude.exe"
    return str(cand) if cand.exists() else None


def _mcp_config() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = json.loads((ROOT / "config.json").read_text("utf-8"))
    p = OUT / "mcp_hub.json"
    url = f"http://{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 8765)}{cfg.get('path', '/mcp')}"
    p.write_text(json.dumps({"mcpServers": {"hub": {"type": "http", "url": url}}}), "utf-8")
    return p


def ask_claude(text: str, timeout: float = 150) -> str:
    exe = _claude_exe()
    if not exe:
        raise NotAvailable("a claude parancs nincs telepítve")
    args = [exe, "-p", text, "--output-format", "json", "--strict-mcp-config", "--mcp-config", str(_mcp_config()),
            "--allowedTools", "mcp__hub__*", "--max-turns", "10", "--append-system-prompt", system_prompt()]
    if _session["id"] and time.time() - _session["ts"] < CONTINUE_S:
        args += ["--resume", _session["id"]]
    r = subprocess.run(args, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, cwd=str(ROOT),
                       creationflags=NOWIN)
    try:
        res = json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise NotAvailable("a claude nem adott értelmezhető választ: " + r.stderr.decode("utf-8", "replace")[-150:])
    if res.get("is_error"):
        msg = str(res.get("result") or res.get("terminal_reason") or "hiba")
        if "login" in msg.lower() or "logged in" in msg.lower():
            raise NotAvailable("a claude parancssori módja nincs bejelentkezve")
        raise NotAvailable(msg[:150])
    _session.update(id=res.get("session_id"), ts=time.time())
    return str(res.get("result") or "").strip()


_hub_calls: set = set()


def _hud_tools(o: dict) -> None:
    try:
        import hud
    except ImportError:
        return
    for b in (o.get("message") or {}).get("content") or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use" and str(b.get("name", "")).startswith("mcp__hub__"):
            _hub_calls.add(b.get("id"))
            hud.send("tool", name=str(b.get("name", "")).replace("mcp__hub__", ""), input=b.get("input") or {})
            _project(hud, b.get("input") or {})
        elif b.get("type") == "tool_result" and b.get("tool_use_id") in _hub_calls:
            c = b.get("content")
            text = " ".join(x.get("text", "") for x in c if isinstance(x, dict)) if isinstance(c, list) else str(c or "")
            ok = not b.get("is_error") and '"ok":false' not in text.replace(" ", "")
            hud.send("result", text=re.sub(r"\s+", " ", text)[:160], ok=ok)
            try:
                _project(hud, json.loads(text))
            except ValueError:
                pass


IMG_EXT = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")


def _project(hud, data) -> None:
    """Hologram-kivetítés: ha egy eszköz képet adott (rajz, PDF-oldal, képernyőkép), az ablak mellé úszik."""
    found = []

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v[:20]:
                walk(x)
        elif isinstance(v, str) and v.lower().endswith(IMG_EXT) and len(v) < 400 and Path(v).is_file():
            found.append(v)

    walk(data)
    for f in dict.fromkeys(found).keys():
        hud.send("project", path=f, title=Path(f).name)
        break


SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÁÉÍÓÖŐÚÜŰ0-9„\"(])")


def ask_claude_stream(text: str, on_sentence, timeout: float = 150) -> str:
    """Mint az ask_claude, de a választ mondatonként adja át, ahogy érkezik (on_sentence), így a felolvasás
    már az első mondatnál indulhat. Vissza: a teljes válasz."""
    exe = _claude_exe()
    if not exe:
        raise NotAvailable("a claude parancs nincs telepítve")
    args = [exe, "-p", text, "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--strict-mcp-config", "--mcp-config", str(_mcp_config()), "--allowedTools", "mcp__hub__*",
            "--max-turns", "10", "--append-system-prompt", system_prompt()]
    if _session["id"] and time.time() - _session["ts"] < CONTINUE_S:
        args += ["--resume", _session["id"]]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         cwd=str(ROOT), creationflags=NOWIN)
    deadline, buf, said, result = time.time() + timeout, "", [], None

    def flush(final: bool):
        nonlocal buf
        parts = SENTENCE_END.split(buf)
        done, buf = (parts, "") if final else (parts[:-1], parts[-1])
        for s in done:
            s = speakable(s)
            if s:
                said.append(s)
                on_sentence(s)

    try:
        for raw in p.stdout:
            if time.time() > deadline:
                raise subprocess.TimeoutExpired(args, timeout)
            try:
                o = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if o.get("type") == "stream_event":
                e = o.get("event", {})
                if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
                    buf += e["delta"].get("text", "")
                    flush(False)
                elif (e.get("type") == "content_block_start" and e.get("content_block", {}).get("type") == "tool_use"
                      and not said and not buf.strip()):
                    said.append("")           # csak egyszer: eszközhívás előtt, amíg még semmit nem mondtunk
                    on_sentence("Egy pillanat, megnézem.")
                elif e.get("type") == "content_block_stop" and buf.strip():
                    buf += " "
                    flush(True)          # egy szövegblokk vége (pl. eszközhívás előtt): ami maradt, menjen
            elif o.get("type") in ("assistant", "user"):
                _hud_tools(o)            # a Jarvis-ablak naplója: eszközhívás kódként, eredmény röviden
            elif o.get("type") == "result":
                result = o
    finally:
        try:
            p.kill()
        except OSError:
            pass
    if result is None:
        raise NotAvailable("a claude nem adott választ")
    if result.get("is_error"):
        msg = str(result.get("result") or result.get("terminal_reason") or "hiba")
        if "login" in msg.lower() or "logged in" in msg.lower():
            raise NotAvailable("a claude parancssori módja nincs bejelentkezve")
        raise NotAvailable(msg[:150])
    if buf.strip():
        flush(True)
    _session.update(id=result.get("session_id"), ts=time.time())
    return " ".join(s for s in said if s) or str(result.get("result") or "").strip()


def ask_local(text: str) -> str:
    sys.path.insert(0, str(ROOT / "plugins" / "local"))
    import local_ollama
    hist = "".join(f"Kérdés: {h['q']}\nVálasz: {h['a']}\n" for h in _local_hist[-4:])
    r = local_ollama.chat((hist + f"Kérdés: {text}\nVálasz:") if hist else text, local_ollama.DEFAULT_MODEL,
                          system_prompt(), 300, 0.4, timeout=110)
    ans = r["text"].strip()
    _local_hist.append({"q": text, "a": ans})
    return ans


def speakable(text: str) -> str:
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[*_`#>|]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def ask(text: str, backend: str = "claude", on_sentence=None) -> dict:
    """Vissza: {"answer", "backend", "seconds", ["note"], ["first_s"]}. on_sentence megadásakor a választ
    mondatonként is átadja, ahogy érkezik (a megjegyzést is, ha a helyi modellre kellett váltani)."""
    t0, note, first = time.time(), None, []

    def emit(s: str) -> None:
        if not first:
            first.append(round(time.time() - t0, 1))
        on_sentence(s)

    if backend == "claude":
        try:
            ans = ask_claude_stream(text, emit) if on_sentence else ask_claude(text)
        except (NotAvailable, subprocess.TimeoutExpired, OSError) as e:
            note = f"A Claude most nem érhető el ({e if isinstance(e, NotAvailable) else 'időtúllépés'}), a helyi modell válaszol."
            backend, ans = "local", ask_local(text)
            if on_sentence:
                emit(note)
                emit(speakable(ans))
    else:
        ans = ask_local(text)
        if on_sentence:
            emit(speakable(ans))
    out = {"answer": speakable(ans) or "Erre nem tudok mit mondani.", "backend": backend,
           "seconds": round(time.time() - t0, 1)}
    if note:
        out["note"] = note
    if first:
        out["first_s"] = first[0]
    OUT.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "q": text, **out}, ensure_ascii=False) + "\n")
    return out


if __name__ == "__main__":  # kézi próba: python assistant.py "kérdés" [claude|local]
    print(json.dumps(ask(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "claude"), ensure_ascii=False))
