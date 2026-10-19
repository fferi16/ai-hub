"""win backend – pywinauto (UIA): natív Windows-ablakok."""
import os
import re
import subprocess
import time

from .common import short

ROLES = {
    "Button": "button", "SplitButton": "button", "Edit": "input", "Document": "input", "ComboBox": "select",
    "CheckBox": "checkbox", "RadioButton": "radio", "MenuItem": "menuitem", "ListItem": "listitem",
    "TabItem": "tab", "Hyperlink": "link", "TreeItem": "treeitem", "DataItem": "cell", "Slider": "slider",
    "Spinner": "spinner",
}
KEYS = {
    "enter": "{ENTER}", "tab": "{TAB}", "escape": "{ESC}", "esc": "{ESC}", "backspace": "{BACKSPACE}",
    "delete": "{DELETE}", "space": "{SPACE}", "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
    "arrowup": "{UP}", "arrowdown": "{DOWN}", "arrowleft": "{LEFT}", "arrowright": "{RIGHT}",
    "home": "{HOME}", "end": "{END}", "pageup": "{PGUP}", "pagedown": "{PGDN}",
    **{f"f{i}": f"{{F{i}}}" for i in range(1, 13)},
}
MODS = {"control": "^", "ctrl": "^", "shift": "+", "alt": "%"}


def esc_keys(text: str) -> str:
    """Szöveg a pywinauto send_keys számára (a speciális karakterek kapcsos zárójelben)."""
    return re.sub(r"([+^%~(){}\[\]])", r"{\1}", text)


def to_keys(spec: str) -> str:
    """'Control+S' / 'Enter' → '^s' / '{ENTER}'"""
    parts = spec.split("+")
    mods = "".join(MODS.get(p.lower(), "") for p in parts[:-1])
    last = parts[-1]
    key = KEYS.get(last.lower(), esc_keys(last.lower() if mods else last))
    return mods + key


class Win:
    kind = "win"

    def __init__(self, cfg: dict):
        from pywinauto import Desktop

        self.cfg = cfg
        self.desktop = Desktop(backend="uia")
        self.win = None
        self.launched = False
        self.wrappers: dict[int, object] = {}
        self.seen_dialogs: set[int] = set()
        self.pending_dialogs: list[str] = []
        self.pending_errors: list[str] = []

    # ---------- életciklus ----------
    def _top(self):
        return [w for w in self.desktop.windows() if w.window_text()]

    def open(self, target: str, electron: bool = False, timeout_s: float = 15) -> None:
        m = re.match(r'^\s*("([^"]+\.(?:exe|lnk|bat|cmd|msc))"|(\S+\.(?:exe|lnk|bat|cmd|msc)))\s*(.*)$', target, re.I)
        if m:
            exe, args = m.group(2) or m.group(3), m.group(4)
            before = {w.handle for w in self._top()}
            subprocess.Popen(f'cmd /c start "" "{exe}" {args}', creationflags=subprocess.CREATE_NO_WINDOW)
            end = time.monotonic() + timeout_s
            stem = os.path.splitext(os.path.basename(exe))[0].lower()
            while time.monotonic() < end:
                new = [w for w in self._top() if w.handle not in before and w.is_visible()]
                if new:
                    pref = [w for w in new if self._proc_name(w) == stem]
                    self.win = (pref or new)[0]
                    break
                time.sleep(0.3)
            else:
                raise TimeoutError(f"nem jelent meg új ablak: {exe}")
            self.launched = True
        else:
            low = target.lower()
            cands = [w for w in self._top() if low in w.window_text().lower()]
            if not cands:
                titles = [short(w.window_text(), 30) for w in self._top()][:8]
                raise LookupError(f"nincs ilyen ablak: '{target}'; nyitott ablakok: {titles}")
            self.win = cands[0]
            self.launched = False
        self.seen_dialogs = {self.win.handle}
        try:
            self.win.set_focus()
        except Exception:
            pass
        time.sleep(0.5)

    @staticmethod
    def _proc_name(w) -> str:
        try:
            import psutil  # nem kötelező

            return os.path.splitext(psutil.Process(w.process_id()).name())[0].lower()
        except Exception:
            return ""

    def close(self) -> None:
        if self.win is not None and self.launched:
            try:
                self.win.close()
            except Exception:
                pass
        self.win, self.wrappers = None, {}

    # ---------- olvasás ----------
    @staticmethod
    def _value(w, ctype: str) -> str | None:
        try:
            if ctype == "Document":
                return w.iface_text.DocumentRange.GetText(2000)
            if ctype in ("Edit", "ComboBox"):
                return w.get_value() if hasattr(w, "get_value") else w.legacy_properties().get("Value", "")
            if ctype in ("CheckBox", "RadioButton"):
                return "be" if w.get_toggle_state() else "ki"
        except Exception:
            return None
        return None

    def elements(self) -> list[dict]:
        self._scan_dialogs()
        els, self.wrappers = [], {}
        n = 0
        for w in self.win.descendants():
            info = w.element_info
            ctype = info.control_type
            if ctype not in ROLES:
                continue
            try:
                if not w.is_visible():
                    continue
                r = info.rectangle
                if r.width() <= 0 or r.height() <= 0:
                    continue
            except Exception:
                continue
            n += 1
            item = {"n": n, "role": ROLES[ctype], "name": short(info.name), "box": [r.left, r.top, r.width(), r.height()]}
            val = self._value(w, ctype)
            if val is not None:
                item["value"] = short(val)
            els.append(item)
            self.wrappers[n] = w
            if n >= 300:
                break
        return els

    def title(self) -> str:
        return short(self.win.window_text(), 80)

    def text(self) -> str:
        parts = [self.win.window_text()]
        for w in self.win.descendants():
            info = w.element_info
            if info.name:
                parts.append(info.name)
            val = self._value(w, info.control_type)
            if val:
                parts.append(val)
        return "\n".join(parts)

    def _scan_dialogs(self) -> None:
        """Új ablakok ugyanattól a folyamattól (hibaüzenet, megerősítés) → errors/changed."""
        try:
            pid = self.win.process_id()
            for w in self._top():
                if w.handle in self.seen_dialogs or w.process_id() != pid:
                    continue
                self.seen_dialogs.add(w.handle)
                texts = [t.element_info.name for t in w.descendants(control_type="Text") if t.element_info.name]
                self.pending_dialogs.append(f"'{short(w.window_text(), 40)}'")
                self.pending_errors.append(f"ablak: {short(w.window_text() + ': ' + ' '.join(texts), 150)}")
        except Exception:
            pass

    def drain_errors(self) -> list[str]:
        self._scan_dialogs()
        e, self.pending_errors = self.pending_errors, []
        return e

    def drain_dialogs(self) -> list[str]:
        d, self.pending_dialogs = self.pending_dialogs, []
        return d

    # ---------- műveletek ----------
    def locate(self, el: dict | None, name: str = "", role: str = ""):
        if el is not None:
            return self.wrappers.get(el["n"])
        low = name.lower()
        best = None
        for w in self.win.descendants():
            info = w.element_info
            r = ROLES.get(info.control_type)
            if not r or (role and r != role):
                continue
            nm = (info.name or "").lower()
            if nm == low:
                return w
            if best is None and low and low in nm:
                best = w
        if best is None and role and not name:
            best = next((w for w in self.win.descendants() if ROLES.get(w.element_info.control_type) == role), None)
        return best

    def click(self, w, timeout_ms: int) -> None:
        try:
            w.invoke()
        except Exception:
            w.click_input()

    def _ensure_foreground(self, w=None) -> None:
        """Billentyű CSAK akkor mehet, ha a célablak biztosan előtérben van – különben más ablakba gépelnénk."""
        import ctypes

        try:
            (w or self.win).set_focus()
        except Exception:
            pass
        time.sleep(0.15)
        fg = ctypes.windll.user32.GetForegroundWindow()
        if fg != self.win.handle:
            raise RuntimeError("a célablak nincs előtérben – a billentyűzetes lépés kihagyva (biztonsági okból)")

    def type(self, w, text: str, timeout_ms: int) -> None:
        """Mezőbe: érték beállítása (UIA ValuePattern). Dokumentumba: a végére fűz, meglévő tartalmat nem töröl."""
        ctype = w.element_info.control_type
        if ctype in ("Edit", "ComboBox"):
            try:
                w.iface_value.SetValue(text)
                return
            except Exception:
                pass
        self._ensure_foreground(w)
        from pywinauto.keyboard import send_keys

        if ctype == "Document":
            send_keys("^{END}")
        self._paste(text)

    def _paste(self, text: str) -> None:
        """Beillesztés vágólapon át (megbízható ékezetekkel is); a vágólap eredeti tartalmát visszaállítja."""
        import win32clipboard as cb
        from pywinauto.keyboard import send_keys

        def get():
            cb.OpenClipboard()
            try:
                return cb.GetClipboardData(cb.CF_UNICODETEXT) if cb.IsClipboardFormatAvailable(cb.CF_UNICODETEXT) else None
            finally:
                cb.CloseClipboard()

        def put(t):
            cb.OpenClipboard()
            try:
                cb.EmptyClipboard()
                if t is not None:
                    cb.SetClipboardText(t, cb.CF_UNICODETEXT)
            finally:
                cb.CloseClipboard()

        old = get()
        put(text)
        try:
            self._ensure_foreground()
            send_keys("^v")
            time.sleep(0.2)
        finally:
            put(old)

    def key(self, keys: str, timeout_ms: int) -> None:
        from pywinauto.keyboard import send_keys

        self._ensure_foreground()
        send_keys(to_keys(keys), pause=0.02)

    def wait_text(self, value: str, timeout_ms: int) -> None:
        end = time.monotonic() + timeout_ms / 1000
        while value.lower() not in self.text().lower():
            if time.monotonic() > end:
                raise TimeoutError(f"nem jelent meg: '{value}'")
            time.sleep(0.3)

    def settle(self) -> None:
        time.sleep(0.3)

    # ---------- kép ----------
    def _rect(self):
        r = self.win.rectangle()
        return r.left, r.top, r.width(), r.height()

    def screenshot(self, loc=None, region=None):
        import mss
        from PIL import Image

        if loc is not None:
            r = loc.rectangle()
            box = (r.left, r.top, r.width(), r.height())
        else:
            wx, wy, ww, wh = self._rect()
            box = (wx + int(region[0]), wy + int(region[1]), int(region[2]), int(region[3])) if region else (wx, wy, ww, wh)
        with mss.mss() as m:
            shot = m.grab({"left": box[0], "top": box[1], "width": max(box[2], 1), "height": max(box[3], 1)})
        return Image.frombytes("RGB", shot.size, shot.rgb)

    def image_box(self, el: dict):
        wx, wy, _, _ = self._rect()
        x, y, w, h = el["box"]
        return (x - wx, y - wy, w, h)

    def click_xy(self, x: float, y: float) -> None:
        from pywinauto import mouse

        self._ensure_foreground()
        wx, wy, _, _ = self._rect()
        mouse.click(coords=(int(wx + x), int(wy + y)))
