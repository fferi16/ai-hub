"""Közös részek: beállítások, útvonalak, munkaszál, képkezelés."""
import concurrent.futures
import difflib
import json
import os
import re
import time
from pathlib import Path

LOOK_HOME = Path(os.environ.get("USERPROFILE", Path.home())) / ".look"
RECIPES = LOOK_HOME / "recipes"
SNAPS = Path(__file__).resolve().parents[3] / "out" / "look"

DEFAULTS = {
    "vl_model": "",
    "ollama_url": "http://localhost:11434",
    "max_image_px": 1024,
    "browser_channel": "msedge",
    "headless": True,
    "step_timeout_s": 10,
    "run_timeout_s": 110,
    "max_elements": 60,
    "min_win_elements": 3,
}

URL_RE = re.compile(r"^(https?|file|about|data):|^www\.", re.I)


def config() -> dict:
    cfg = dict(DEFAULTS)
    path = LOOK_HOME / "config.json"
    if path.exists():
        cfg.update(json.loads(path.read_text("utf-8")))
    return cfg


def short(text, n: int = 40) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def similar(name: str, els: list[dict]) -> str:
    names = [e["name"] for e in els if e["name"]]
    best = difflib.get_close_matches(name, names, n=1, cutoff=0.3)
    if not best:
        return ""
    e = next(e for e in els if e["name"] == best[0])
    return f"hasonló: '{e['name']}' (el {e['n']})"


class Worker:
    """Egyetlen saját szál: a Playwright és a UIA (COM) szálhoz kötött, ezért minden itt fut."""

    def __init__(self):
        self.pool = concurrent.futures.ThreadPoolExecutor(1, "look", initializer=self._init)

    @staticmethod
    def _init():
        try:
            import pythoncom  # noqa: F401  (pywinauto hozza)

            pythoncom.CoInitializeEx(0)
        except Exception:
            pass

    def call(self, fn, *args, timeout: float = 120, **kw):
        return self.pool.submit(fn, *args, **kw).result(timeout)

    def stop(self):
        self.pool.shutdown(wait=False, cancel_futures=True)


def save_image(img, tag: str, max_px: int = 800, labels: list | None = None) -> dict:
    """Kicsinyít, opcionálisan elemszámokat rajzol rá, a kisebb formátumban ment. → {image, kb}"""
    from PIL import Image, ImageDraw

    img = img.convert("RGB")
    scale = min(1.0, max_px / max(img.width, 1))
    if labels:
        d = ImageDraw.Draw(img)
        for n, (x, y, w, h) in labels:
            d.rectangle((x, y, x + w, y + h), outline=(255, 0, 80), width=max(1, int(1 / scale)))
            fs = max(10, int(12 / scale))
            d.rectangle((x, y, x + fs * len(str(n)) * 0.7 + 4, y + fs + 2), fill=(255, 0, 80))
            d.text((x + 2, y), str(n), fill="white", font_size=fs)
    if scale < 1:
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    SNAPS.mkdir(parents=True, exist_ok=True)
    stem = SNAPS / f"{time.strftime('%Y-%m-%d_%H%M%S')}_{tag}"
    jpg, png = stem.with_suffix(".jpg"), stem.with_suffix(".png")
    img.save(jpg, quality=70, optimize=True)
    img.save(png, optimize=True)
    keep, drop = (jpg, png) if jpg.stat().st_size <= png.stat().st_size else (png, jpg)
    drop.unlink()
    return {"image": str(keep).replace("\\", "/"), "kb": round(keep.stat().st_size / 1024)}


class Deadline:
    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds

    def left(self) -> float:
        return self.end - time.monotonic()

    def check(self):
        if self.left() <= 0:
            raise TimeoutError("a teljes futás időkorlátja lejárt")

