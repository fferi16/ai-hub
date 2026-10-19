"""web backend – Playwright: weboldalak és Electron-appok (CDP-n keresztül)."""
import io
import socket
import subprocess
import time

from .common import short

# Interaktív és hibaüzenet-elemek kigyűjtése, számozás data-look attribútummal.
ELEMENTS_JS = r"""
() => {
  const sel = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],'
    + '[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=textbox],[role=combobox],[role=switch],'
    + '[contenteditable=""],[contenteditable=true],[onclick],[role=alert],[role=alertdialog],[aria-invalid=true]';
  document.querySelectorAll('[data-look]').forEach(e => e.removeAttribute('data-look'));
  const out = []; let n = 0;
  for (const e of document.querySelectorAll(sel)) {
    const r = e.getBoundingClientRect(), st = getComputedStyle(e);
    if (r.width < 1 || r.height < 1 || st.visibility === 'hidden' || st.display === 'none') continue;
    if (++n > 300) break;
    e.setAttribute('data-look', n);
    const tag = e.tagName.toLowerCase(), type = (e.type || '').toLowerCase();
    let role = e.getAttribute('role');
    if (!role) role = tag === 'a' ? 'link' : tag === 'button' || tag === 'summary' ? 'button'
      : tag === 'select' ? 'select' : tag === 'textarea' ? 'input'
      : tag === 'input' ? (['checkbox', 'radio'].includes(type) ? type : ['submit', 'button', 'reset'].includes(type) ? 'button' : 'input')
      : e.isContentEditable ? 'input' : 'clickable';
    const isField = ['input', 'select', 'textarea'].includes(tag);
    const label = e.getAttribute('aria-label') || (e.labels && e.labels[0] && e.labels[0].innerText)
      || (isField ? (e.placeholder || e.title || e.name || e.id) : (e.innerText || e.value || e.title || e.alt))
      || '';
    const item = {n, role, name: label, box: [r.x, r.y, r.width, r.height]};
    if (['checkbox', 'radio'].includes(type)) item.value = e.checked ? 'be' : 'ki';
    else if (isField) item.value = e.value || '';
    out.push(item);
  }
  let area = 0;
  for (const c of document.querySelectorAll('canvas')) { const r = c.getBoundingClientRect(); area += Math.max(0, r.width) * Math.max(0, r.height); }
  return {els: out, canvas: document.querySelectorAll('canvas').length, canvas_area: area / (innerWidth * innerHeight),
          title: document.title, url: location.href};
}
"""

TEXT_JS = r"""
() => document.title + '\n' + document.body.innerText + '\n' + [...document.querySelectorAll('input,textarea,select')].map(e => e.value).join('\n')
"""

ARIA_ROLES = {"button", "link", "checkbox", "radio", "tab", "menuitem", "switch", "combobox"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Web:
    kind = "web"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.pw = self.browser = self.context = self.page = None
        self.proc = None  # Electron-folyamat, ha mi indítottuk
        self.errs: list[str] = []
        self.dialogs: list[str] = []
        self.meta: dict = {}

    # ---------- életciklus ----------
    def _hook(self, page):
        def console(m):
            if m.type == "error":
                self.errs.append("console: " + short(m.text, 150))

        page.on("console", console)
        page.on("pageerror", lambda e: self.errs.append("js: " + short(e, 150)))
        page.on("requestfailed", lambda r: self.errs.append(f"net: {short(r.url, 80)} {r.failure}"))
        page.on("response", lambda r: r.status >= 400 and self.errs.append(f"http {r.status}: {short(r.url, 80)}"))

        def dialog(d):
            self.dialogs.append(f"{d.type}: '{short(d.message, 80)}'")
            d.dismiss()

        page.on("dialog", dialog)

    def open(self, target: str, electron: bool = False, timeout_ms: int = 20000) -> None:
        from playwright.sync_api import sync_playwright

        if self.pw is None:
            self.pw = sync_playwright().start()
        if electron:
            self.close()
            self.pw = sync_playwright().start()
            port = free_port()
            self.proc = subprocess.Popen([target, f"--remote-debugging-port={port}"])
            end = time.monotonic() + timeout_ms / 1000
            while True:
                try:
                    self.browser = self.pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    break
                except Exception:
                    if time.monotonic() > end:
                        raise
                    time.sleep(0.5)
            ctx = self.browser.contexts[0]
            while not ctx.pages and time.monotonic() < end:
                time.sleep(0.3)
            self.page = ctx.pages[0]
            self._hook(self.page)
            self.page.wait_for_load_state("load", timeout=timeout_ms)
            return
        if self.browser is None:
            try:
                self.browser = self.pw.chromium.launch(channel=self.cfg["browser_channel"], headless=self.cfg["headless"])
            except Exception:
                self.browser = self.pw.chromium.launch(headless=self.cfg["headless"])
            self.context = self.browser.new_context(viewport={"width": 1280, "height": 800}, locale="hu-HU")
        if self.page is None or self.page.is_closed():
            self.page = self.context.new_page()
            self._hook(self.page)
        if target.lower().startswith("www."):
            target = "https://" + target
        self.page.goto(target, wait_until="load", timeout=timeout_ms)

    def close(self) -> None:
        for fn in (lambda: self.browser and self.browser.close(), lambda: self.pw and self.pw.stop()):
            try:
                fn()
            except Exception:
                pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.pw = self.browser = self.context = self.page = self.proc = None

    # ---------- olvasás ----------
    def elements(self) -> list[dict]:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        data = self.page.evaluate(ELEMENTS_JS)
        self.meta = {"canvas": data["canvas"], "canvas_area": data["canvas_area"], "url": data["url"]}
        els = []
        for e in data["els"]:
            e["name"] = short(e["name"])
            if "value" in e:
                e["value"] = short(e["value"])
            els.append(e)
        return els

    def title(self) -> str:
        return short(self.page.title(), 80)

    def text(self) -> str:
        return self.page.evaluate(TEXT_JS)

    def drain_errors(self) -> list[str]:
        errs, self.errs = self.errs[-10:], []
        return errs

    def drain_dialogs(self) -> list[str]:
        d, self.dialogs = self.dialogs, []
        return d

    # ---------- műveletek ----------
    def locate(self, el: dict | None, name: str = "", role: str = ""):
        """Elem: számozott lista szerint (data-look), vagy név/szerep alapján (receptek)."""
        if el is not None:
            return self.page.locator(f'[data-look="{el["n"]}"]')
        p = self.page
        if role in ARIA_ROLES:
            return p.get_by_role(role, name=name).first
        if role in ("input", "select"):
            return p.get_by_label(name).or_(p.get_by_placeholder(name)).or_(p.get_by_role("textbox", name=name)).first
        return p.get_by_role("button", name=name).or_(p.get_by_role("link", name=name)).or_(p.get_by_text(name)).first

    def click(self, loc, timeout_ms: int) -> None:
        loc.click(timeout=timeout_ms)

    def type(self, loc, text: str, timeout_ms: int) -> None:
        loc.fill(text, timeout=timeout_ms)

    def key(self, keys: str, timeout_ms: int) -> None:
        self.page.keyboard.press(keys)

    def wait_text(self, value: str, timeout_ms: int) -> None:
        self.page.get_by_text(value).first.wait_for(state="visible", timeout=timeout_ms)

    def settle(self) -> None:
        try:
            self.page.wait_for_load_state("load", timeout=3000)
        except Exception:
            pass

    # ---------- kép ----------
    def screenshot(self, loc=None, region=None):
        from PIL import Image

        if loc is not None:
            png = loc.screenshot(timeout=5000)
        elif region:
            x, y, w, h = region
            png = self.page.screenshot(clip={"x": x, "y": y, "width": w, "height": h})
        else:
            png = self.page.screenshot()
        return Image.open(io.BytesIO(png))

    @staticmethod
    def image_box(el: dict):
        """Az elem téglalapja a teljes képernyőképen (a feliratozáshoz)."""
        return el.get("box")

    def click_xy(self, x: float, y: float) -> None:
        self.page.mouse.click(x, y)
