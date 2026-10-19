"""ocr backend – képernyőkép + Windows beépített OCR (helyi, ingyenes, magyarul is).
Egy másik backend (web/win) köré épül: tőle kapja a képet, a kattintást és a billentyűzetet."""
import asyncio
import concurrent.futures
import time

from .common import short


def recognize(img, lang: str = "hu") -> list[dict]:
    """→ [{"text", "box": [x, y, w, h]}] soronként, a kép koordinátáiban.
    Saját szálon fut, mert a Playwright szálán már van futó eseményhurok."""
    return _POOL.submit(asyncio.run, _recognize(img, lang)).result(60)


_POOL = concurrent.futures.ThreadPoolExecutor(1, "look-ocr")


async def _recognize(img, lang: str) -> list[dict]:
    from PIL import Image
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    engine = OcrEngine.try_create_from_language(Language(lang)) or OcrEngine.try_create_from_user_profile_languages()
    # Apró betűknél a nagyítás sokat javít; a dobozokat visszaskálázzuk.
    scale = 2.0 if max(img.size) * 2 <= OcrEngine.max_image_dimension and img.height < 1200 else 1.0
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    r, g, b, a = img.convert("RGBA").split()
    data = Image.merge("RGBA", (b, g, r, a)).tobytes()
    writer = DataWriter()
    writer.write_bytes(data)
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, img.width, img.height
    )
    result = await engine.recognize_async(bitmap)
    lines = []
    for line in result.lines:
        rects = [w.bounding_rect for w in line.words]
        if not rects:
            continue
        x0, y0 = min(r.x for r in rects), min(r.y for r in rects)
        x1, y1 = max(r.x + r.width for r in rects), max(r.y + r.height for r in rects)
        box = [round(v / scale) for v in (x0, y0, x1 - x0, y1 - y0)]
        lines.append({"text": line.text, "box": box})
    return lines


def wants_ocr(backend, els: list[dict], cfg: dict) -> bool:
    """Nincs/kevés kiolvasható elem, vagy a weboldalt egy nagy vászon tölti ki → ocr."""
    if backend.kind == "web":
        meta = getattr(backend, "meta", {})
        return meta.get("canvas_area", 0) >= 0.3 or (meta.get("canvas", 0) > 0 and len(els) < cfg["min_win_elements"])
    return len(els) < cfg["min_win_elements"]


class Ocr:
    kind = "ocr"

    def __init__(self, inner, cfg: dict):
        self.inner, self.cfg = inner, cfg
        self.lines: list[dict] = []

    # ---------- életciklus ----------
    def open(self, target: str, electron: bool = False) -> None:
        self.inner.open(target, electron=electron)

    def close(self) -> None:
        self.inner.close()

    def settle(self) -> None:
        self.inner.settle()

    def title(self) -> str:
        return self.inner.title()

    def drain_errors(self) -> list[str]:
        return self.inner.drain_errors()

    def drain_dialogs(self) -> list[str]:
        return self.inner.drain_dialogs()

    # ---------- olvasás ----------
    def _scan(self) -> list[dict]:
        self.lines = recognize(self.inner.screenshot())
        return self.lines

    def elements(self) -> list[dict]:
        return [{"n": i, "role": "text", "name": short(l["text"]), "box": l["box"]} for i, l in enumerate(self._scan(), 1)]

    def text(self) -> str:
        own = "\n".join(l["text"] for l in self._scan())
        try:
            return own + "\n" + self.inner.text()
        except Exception:
            return own

    def wait_text(self, value: str, timeout_ms: int) -> None:
        end = time.monotonic() + timeout_ms / 1000
        while value.lower() not in "\n".join(l["text"] for l in self._scan()).lower():
            if time.monotonic() > end:
                raise TimeoutError(f"nem jelent meg: '{value}'")
            time.sleep(0.5)

    # ---------- műveletek (koordinátás) ----------
    def locate(self, el: dict | None, name: str = "", role: str = ""):
        if el is not None:
            return el
        low = name.lower()
        lines = self.lines or self._scan()
        hit = next((l for l in lines if l["text"].lower() == low), None) or next((l for l in lines if low and low in l["text"].lower()), None)
        return {"box": hit["box"], "name": hit["text"]} if hit else None

    def click(self, el: dict, timeout_ms: int) -> None:
        x, y, w, h = el["box"]
        self.inner.click_xy(x + w / 2, y + h / 2)

    def type(self, el: dict, text: str, timeout_ms: int) -> None:
        self.click(el, timeout_ms)
        if self.inner.kind == "web":
            self.inner.page.keyboard.type(text)
        else:
            self.inner._paste(text)

    def key(self, keys: str, timeout_ms: int) -> None:
        self.inner.key(keys, timeout_ms)

    # ---------- kép ----------
    def screenshot(self, loc=None, region=None):
        if isinstance(loc, dict):
            x, y, w, h = loc["box"]
            return self.inner.screenshot(region=(x, y, w, h))
        return self.inner.screenshot(region=region)

    @staticmethod
    def image_box(el: dict):
        return el.get("box")

    def click_xy(self, x: float, y: float) -> None:
        self.inner.click_xy(x, y)
