"""Negyedik szint: helyi vision-modell – mielőtt kép Claude-hoz kerülne, ez nézi meg, ingyen.

vl_backend = "llamacpp" (alap): a Csevegő llama-server.exe-je + GGUF/mmproj. Ha a Csevegő épp VL-modellt
futtat, azt használja; különben saját példányt indít (vl_port), ami vl_idle_min percig betöltve marad.
vl_backend = "ollama": Ollama /api/chat (vl_model = ollama modellnév).
"""
import base64
import io
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

from .common import SNAPS, save_image

_proc = None
_idle: threading.Timer | None = None


def _get(url: str, timeout: float = 2):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def _healthy(port: int) -> bool:
    try:
        return _get(f"http://127.0.0.1:{port}/health")[0] == 200
    except Exception:
        return False


def _running_vision_port() -> int | None:
    """Fut-e már llama-server --mmproj kapcsolóval (pl. a Csevegőből)? → port"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | % CommandLine"],
            capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if "--mmproj" in line and "--no-mmproj" not in line:
            m = re.search(r"--port\s+(\d+)", line)
            if m and _healthy(int(m.group(1))):
                return int(m.group(1))
    return None


def stop() -> None:
    """A saját indítású llama-server leállítása (a Csevegőét nem bántjuk)."""
    global _proc
    if _proc and _proc.poll() is None:
        _proc.terminate()
    _proc = None


def _touch(cfg: dict) -> None:
    global _idle
    if _idle:
        _idle.cancel()
    _idle = threading.Timer(float(cfg.get("vl_idle_min", 30)) * 60, stop)
    _idle.daemon = True
    _idle.start()


def _llamacpp_port(cfg: dict) -> int:
    """Kész szerver portja; ha még töltődik: TimeoutError (a következő hívás már kész lehet)."""
    global _proc
    port = int(cfg.get("vl_port", 8766))
    if _healthy(port):
        return port
    other = _running_vision_port()
    if other:
        return other
    if _proc is None or _proc.poll() is not None:
        args = [cfg["llama_server"], "-m", cfg["vl_model"], "--mmproj", cfg["vl_mmproj"],
                "--host", "127.0.0.1", "--port", str(port), *cfg.get("vl_args", [])]
        SNAPS.mkdir(parents=True, exist_ok=True)
        logf = open(SNAPS / "llama-vl.log", "ab")
        from pathlib import Path

        _proc = subprocess.Popen(args, cwd=str(Path(cfg["llama_server"]).parent), stdout=logf, stderr=logf,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
    end = time.monotonic() + float(cfg.get("vl_wait_s", 90))
    while time.monotonic() < end:
        if _healthy(port):
            return port
        if _proc.poll() is not None:
            raise RuntimeError(f"a llama-server leállt (kód {_proc.returncode}), napló: {SNAPS / 'llama-vl.log'}")
        time.sleep(1)
    raise TimeoutError("a VL-modell még töltődik – próbáld újra kb. 1 perc múlva")

PROMPT = """Egy képernyőképet (vagy képernyőrészletet) látsz. Válaszolj a kérdésre röviden, magyarul.
Kérdés: {q}
Csak ezt a JSON-t add vissza:
{{"answer": "<rövid válasz>", "confidence": "high" vagy "low", "point": [x, y] vagy null}}
- confidence: "high" csak akkor, ha a képen egyértelműen látszik a válasz; különben "low".
- point: ha a kérdés egy elem helyére vonatkozik, az elem közepe 0–1000 közé normált koordinátákban
  (bal felső sarok [0,0], jobb alsó [1000,1000]); egyébként null."""

PROMPT2 = """Két képernyőképet látsz: az első a korábbi, a második a mostani állapot.
Kérdés: {q}
Csak ezt a JSON-t add vissza: {{"answer": "<rövid válasz: mi változott>", "confidence": "high" vagy "low", "point": null}}"""


def _b64(img, max_px: int) -> tuple[str, float]:
    scale = min(1.0, max_px / max(img.width, img.height, 1))
    if scale < 1:
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode(), scale


def _chat(cfg: dict, prompt: str, images: list[str]) -> str:
    if cfg.get("vl_backend", "llamacpp") == "llamacpp":
        port = _llamacpp_port(cfg)
        _touch(cfg)
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in images
        ]
        body = {"messages": [{"role": "user", "content": content}], "temperature": 0, "max_tokens": 300,
                "response_format": {"type": "json_object"}}
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=cfg.get("vl_timeout_s", 100)) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]
    body = {
        "model": cfg["vl_model"],
        "messages": [{"role": "user", "content": prompt, "images": images}],
        "stream": False,
        "format": "json",
        "think": False,
        "keep_alive": cfg.get("keep_alive", "30m"),  # betöltve marad a további kérdésekhez
        "options": {"temperature": 0},
    }
    url = cfg["ollama_url"].rstrip("/") + "/api/chat"

    def post(b):
        req = urllib.request.Request(url, json.dumps(b).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=cfg.get("vl_timeout_s", 100)) as r:
            return json.loads(r.read())["message"]["content"]

    try:
        return post(body)
    except urllib.error.HTTPError as e:
        if e.code == 400 and "think" in e.read().decode(errors="replace"):
            body.pop("think")
            return post(body)
        raise


def _parse(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else {"answer": text.strip()[:300], "confidence": "low"}


def ask(session, question: str, el=None, region=None, decisive: bool = False, compare: str = "") -> dict:
    cfg = session.cfg
    if not cfg.get("vl_model"):
        return {"ok": False, "reason": "nincs beállítva vl_model", "hint": "%USERPROFILE%\\.look\\config.json"}
    b = session.need()
    if isinstance(region, str) and region:
        region = [float(v) for v in region.split(",")]
    if el:
        loc, _ = session.resolve({"el": el})
        img, origin = b.screenshot(loc=loc), None
    else:
        img = b.screenshot(region=region or None)
        origin = (region[0], region[1]) if region else (0, 0)

    max_px = int(cfg.get("max_image_px", 1024))
    cur, scale = _b64(img, max_px)
    if compare:
        from PIL import Image

        prev, _ = _b64(Image.open(compare), max_px)
        prompt, images = PROMPT2.format(q=question), [prev, cur]
    else:
        prompt, images = PROMPT.format(q=question), [cur]

    try:
        raw = _chat(cfg, prompt, images)
    except TimeoutError as e:
        return {"ok": False, "reason": "loading", "hint": str(e)}
    except Exception as e:
        return {"ok": False, "reason": f"VL: {type(e).__name__}: {str(e)[:150]}", "escalate": True}
    data = _parse(raw)
    conf = "high" if str(data.get("confidence", "")).lower() == "high" else "low"
    res = {"ok": True, "answer": str(data.get("answer", ""))[:400], "confidence": conf}
    pt = data.get("point")
    if isinstance(pt, list) and len(pt) == 2 and origin is not None:
        try:
            # a backend koordinátáiba (oldal/ablak), hogy {"do":"click","at":[x,y]} közvetlenül használhassa
            res["point"] = [round(origin[0] + float(pt[0]) / 1000 * img.width),
                            round(origin[1] + float(pt[1]) / 1000 * img.height)]
        except (TypeError, ValueError):
            pass
    if decisive or conf == "low":
        res["escalate"] = True  # Claude nézze meg maga: a kép útvonala kéznél van
        res["image"] = save_image(img, "ask", 800)["image"]
    return res
