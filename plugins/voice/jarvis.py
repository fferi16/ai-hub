"""Jarvis-ablak: a hangasszisztens grafikus felülete (pythonw jarvis.py [diktáló-pid] | --demo).

Arany holografikus gömb (mint a Bosszúállókban): izzó pixelekből és töredékekből álló 3D-s héj, belső ívek,
panelek, középen a „szem". A pixelek CSAK akkor mozognak, amikor ő beszél: a felolvasott hang valódi
hangerő-burkolója (hud.envelope) hajtja őket – szótagra kifelé lélegeznek, hangsúlyra szikráznak, a belső ívek
a beszéd erejével pörögnek, és a gömb ilyenkor lassan elfordul. Csendben áll. Bal egérrel a gömb forgatható.

Alul naplópanel: amit hallott, a futó eszközhívások kódként (λ system_open(target="chrome")), az eredmények és
a válasz; programindításkor a program ikonja a gömb közepén. Az eseményeket a hud.py küldi UDP-n
(127.0.0.1:8771). Mindig felül, keret nélkül; a naplópanelnél fogva húzható (a helyét megjegyzi), jobb egérrel
menü: mindig látszik / „Figyelj" hallgatás / elrejtés / bezárás. Tétlenül HIDE_S mp után elrejtőzik, a következő
eseményre előjön. Egy példány fut; kilép, ha a diktáló leáll.
"""
import ctypes
import json
import math
import os
import queue
import shutil
import socket
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from ctypes import wintypes
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
import hud  # noqa: E402

POS = ROOT / "out" / "voice" / "jarvis_pos.json"
LISTEN = ROOT / "out" / "voice" / "_hallgat.json"
HIDE_S = 20
TEST = "--demo" in sys.argv

BG = (5, 6, 9)
GOLD = np.array([1.0, 0.64, 0.20], np.float32)       # az izzás színe (R, G, B arány)
HOT = np.array([0.0, 0.45, 0.75], np.float32)        # túlvezérelt pontok fehéredése
STATES = {  # felirat, a gömb fényereje, a felirat színe
    "idle": ("KÉSZENLÉT", 0.75, "#c98a2e"), "wake": ("IGEN?", 1.15, "#ffc45a"),
    "listen": ("HALLGATLAK", 1.15, "#ffc45a"), "think": ("GONDOLKODOM", 1.0, "#ffb648"),
    "tool": ("VÉGREHAJTOM", 1.05, "#ffb648"), "speak": ("", 1.1, "#ffc45a"), "done": ("KÉSZ", 0.95, "#c98a2e"),
    "off": ("NÉMA", 0.4, "#6e7f8a"), "error": ("HIBA", 0.8, "#ff5656"),
}
LINE_COL = {"note": "#ffd27a", "heard": "#fff3dc", "tool": "#ffb648", "ok": "#7fe0a0", "fail": "#ff6b6b", "say": "#c9a979"}

if not TEST:
    # egy példány fut; újraindításkor a régi még pár mp-ig élhet (a szülője után lép ki) – megvárjuk
    _k32 = ctypes.windll.kernel32
    for _ in range(40):
        _mx = _k32.CreateMutexW(None, False, "AIHUB_voice_jarvis")
        if _k32.GetLastError() != 183:
            break
        _k32.CloseHandle(_mx)
        time.sleep(0.25)
    else:
        sys.exit(0)                                          # tartósan fut már egy
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:  # noqa: BLE001
    pass
try:
    DPI = ctypes.windll.user32.GetDpiForSystem() / 96
except Exception:  # noqa: BLE001
    DPI = 1.0

TERM_H = int(156 * DPI)        # naplópanel + beviteli sor


def _base_size() -> int:
    """A gömb oldala képpontban: "voice": {"jarvis_size": 440} (pont), vagy a görgővel beállított, megjegyzett méret."""
    try:
        return int(json.loads(POS.read_text("utf-8"))["size"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        pts = int(json.loads((ROOT / "config.json").read_text("utf-8")).get("voice", {}).get("jarvis_size", 440))
    except (OSError, ValueError, TypeError):
        pts = 440
    return int(max(220, min(1000, pts)) * DPI)


# ---------------------------------------------------------------- a gömb geometriája
def _unit(v):
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def build_sphere(seed: int = 7):
    """Pontfelhő: (pozíciók N×3, fényerő, fázis, sugárirányú mozgékonyság, csoport). Egységgömbben."""
    rng = np.random.default_rng(seed)
    P, B, M, G, FID = [], [], [], [], []

    def add(pts, bright, mob, grp):
        pts = np.asarray(pts, np.float32).reshape(-1, 3)
        P.append(pts)
        B.append(np.broadcast_to(np.float32(bright), len(pts)) if np.isscalar(bright) else np.asarray(bright, np.float32))
        M.append(np.broadcast_to(np.float32(mob), len(pts)) if np.isscalar(mob) else np.asarray(mob, np.float32))
        G.append(np.full(len(pts), grp, np.int8))
        FID.append(np.full(len(pts), len(FID), np.int32))   # egy töredék pontjai együtt mozognak

    up = np.array([0, 1, 0], np.float32)
    # 1) a héj szilánkjai: a szélességi körök mentén futó rövid, izzó töredékek
    for _ in range(760):
        u = _unit(rng.normal(size=3).astype(np.float32))
        r = 0.9 + 0.1 * rng.random()
        t = np.cross(u, up)
        t = _unit(t + 0.35 * rng.normal(size=3).astype(np.float32)) if np.linalg.norm(t) > 1e-3 else np.array([1, 0, 0])
        ln = 0.02 + 0.17 * rng.random() ** 2
        k = 2 + int(ln * 70)
        s = np.linspace(-ln / 2, ln / 2, k, dtype=np.float32)[:, None]
        add(u * r + t * s, 0.7 + 0.5 * rng.random(), 0.6 + 0.8 * rng.random(), 0)
    # 2) kifelé álló tüskék (a széleken kiálló fénycsíkok)
    for _ in range(140):
        u = _unit(rng.normal(size=3).astype(np.float32))
        k = 6
        s = np.linspace(0.97, 1.05 + 0.12 * rng.random(), k, dtype=np.float32)[:, None]
        add(u * s, np.linspace(0.9, 0.25, k), 1.6, 1)
    # 3) szaggatott szélességi gyűrűk a héjon
    for lat in np.radians([-62, -38, -14, 12, 35, 58]):
        r = math.cos(lat) * 0.97
        y = math.sin(lat) * 0.97
        a = np.linspace(0, 2 * math.pi, int(260 * r) + 20, endpoint=False)
        keep = (np.sin(a * rng.integers(3, 8) + rng.random() * 6) > -0.2)
        a = a[keep]
        add(np.stack([r * np.cos(a), np.full_like(a, y), r * np.sin(a)], 1), 0.7, 0.4, 2)
    # 4) belső, megdöntött nagy ívek (az örvény) – beszédnél pörögnek
    for k in range(4):
        r = 0.48 + 0.1 * k
        a = np.linspace(0, math.radians(200 + 40 * k), 240, dtype=np.float32)
        ring = np.stack([r * np.cos(a), r * np.sin(a), np.zeros_like(a)], 1)
        ax, ay = rng.random() * math.pi, rng.random() * math.pi
        rx = np.array([[1, 0, 0], [0, math.cos(ax), -math.sin(ax)], [0, math.sin(ax), math.cos(ax)]], np.float32)
        ry = np.array([[math.cos(ay), 0, math.sin(ay)], [0, 1, 0], [-math.sin(ay), 0, math.cos(ay)]], np.float32)
        add(ring @ rx.T @ ry.T, 0.95, 0.25, 3)
    # 5) lebegő panelek (rácsos téglalapok) a héj alatt, kifelé fordulva
    for _ in range(16):
        u = _unit(rng.normal(size=3).astype(np.float32))
        r = 0.62 + 0.22 * rng.random()
        e1 = _unit(np.cross(u, up))
        e2 = np.cross(u, e1)
        w, h = 0.08 + 0.1 * rng.random(), 0.05 + 0.08 * rng.random()
        pts = []
        for fy in np.linspace(-1, 1, 4):
            for fx in np.linspace(-1, 1, 12):
                pts.append(u * r + e1 * fx * w + e2 * fy * h)
        for fx in (-1, 1):
            for fy in np.linspace(-1, 1, 8):
                pts.append(u * r + e1 * fx * w + e2 * fy * h)
        add(pts, 0.5, 0.5, 4)
    # 6) a szem: sűrű kis gyűrűk és mag
    for r, n, b in ((0.17, 260, 1.2), (0.12, 200, 1.5), (0.07, 140, 1.9), (0.03, 60, 2.4)):
        a = np.linspace(0, 2 * math.pi, n, endpoint=False)
        add(np.stack([r * np.cos(a), r * np.sin(a), np.zeros_like(a)], 1), b, 0.15, 5)
    # 7) por a belsejében
    for _ in range(700):
        add(_unit(rng.normal(size=3).astype(np.float32)) * (rng.random() ** 0.5 * 0.88), 0.22, 1.0, 6)
    P = np.concatenate(P)
    fid = np.concatenate(FID)
    phase = (rng.random(len(FID)) * 6.283).astype(np.float32)[fid]
    tempo = (0.6 + 2.4 * rng.random(len(FID))).astype(np.float32)[fid]   # töredékenként saját ritmus
    return (P, np.concatenate(B).astype(np.float32), phase, tempo,
            np.concatenate(M).astype(np.float32), np.concatenate(G))


def _rot(yaw: float, pitch: float, roll: float = 0.0) -> np.ndarray:
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], np.float32)
    rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]], np.float32)
    rz = np.array([[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]], np.float32)
    return rz @ rx @ ry


# pixeles felirat a gömbön: kis betűméret, élsimítás nélkül → minden képpont egy izzó pont (LED-mátrix)
try:
    TEXT_FONT = ImageFont.truetype("consolab.ttf", 11)
except OSError:
    TEXT_FONT = ImageFont.load_default()
TEXT_H = 13
TEXT_STEP = 0.02            # egy betűpixel szélessége radiánban a gömb egyenlítőjén


def text_pixels(text: str):
    """A felirat világító képpontjai: (oszlop, sor) tömbök és a szélesség pixelben."""
    w = max(1, int(TEXT_FONT.getlength(text)) + 2)
    img = Image.new("1", (w, TEXT_H), 0)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    d.text((1, 0), text, font=TEXT_FONT, fill=1)
    ys, xs = np.nonzero(np.asarray(img))
    return xs.astype(np.float32), ys.astype(np.float32), w


class Sphere:
    def __init__(self, size: int):
        self.n = size
        self.P, self.B, self.PH, self.F, self.MOB, self.G = build_sphere()
        self.inner = self.G == 3
        self.sparks = np.zeros(len(self.P), np.float32)
        self._text, self._tpix = None, None
        self.resize(size)

    def resize(self, size: int) -> None:
        self.n = size
        self.blur_s = ImageFilter.GaussianBlur(max(0.8, size / 270))
        self.blur_l = ImageFilter.GaussianBlur(size / 42)

    def _text_points(self, text: str, age: float, amp: float, t: float):
        """A felirat 3D pontjai a NÉZŐ felé forduló sávban (a gömb forgatásától függetlenül olvasható)."""
        if text != self._text:
            self._text, self._tpix = text, text_pixels(text) if text else None
        if self._tpix is None:
            return None
        xs, ys, w = self._tpix
        span = w * TEXT_STEP
        if span <= 1.9:
            off = -span / 2                                   # elfér: középen áll
        else:                                                 # hosszú: jobbról befut és körbefut
            off = 1.05 - (age * 0.55) % (span + 2.1)
        th = off + xs * TEXT_STEP
        ph = -0.42 + (TEXT_H / 2 - ys) * TEXT_STEP * 1.15     # a szem alatt, az alsó harmadban
        if amp > 0.003:
            ph = ph + amp * 0.012 * np.sin(t * 11 + xs * 0.7)  # beszéd közben enyhén hullámzik
        r = 1.05
        Q = np.stack([r * np.cos(ph) * np.sin(th), r * np.sin(ph), r * np.cos(ph) * np.cos(th)], 1).astype(np.float32)
        front = np.clip((Q[:, 2] - 0.25) / 0.5, 0, 1)          # a széleken elhalványul
        return Q, (2.4 * front).astype(np.float32)

    def render(self, yaw, pitch, swirl, amp, t, gain, rng, roll=0.0, text="", text_age=0.0) -> Image.Image:
        """amp: a mozgás ereje (beszéd, vagy a tétlen lélegzés); text: pixeles felirat a gömb elején."""
        P = self.P
        if swirl:
            c, s = math.cos(swirl), math.sin(swirl)
            rz = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], np.float32)
            P = P.copy()
            P[self.inner] = P[self.inner] @ rz.T
        bright = self.B
        if amp > 0.003:
            # lélegzés: minden pont a saját ritmusában mozdul kifelé, a beszéd erejével arányosan
            wob = 0.5 + 0.5 * np.sin(t * (2.5 + 4.0 * self.F) + self.PH)
            scale = 1.0 + amp * self.MOB * (0.025 + 0.11 * wob)
            P = P * scale[:, None]
            # hangsúlyra szikrák: néhány pont felvillan és lassan kihűl
            self.sparks *= 0.82
            hit = rng.random(len(P)) < amp ** 2 * 0.02
            self.sparks[hit] = 1.0
            bright = self.B * (0.8 + 0.45 * amp * wob) + self.sparks * 1.4
            bright = np.where(self.G == 5, bright * (1 + 1.2 * amp), bright)
        else:
            self.sparks *= 0.0
        Q = P @ _rot(yaw, pitch, roll).T
        tp = self._text_points(text, text_age, amp, t)
        if tp is not None:                                    # a felirat pontjai, 2×2-es „LED"-ként
            Q = np.concatenate([Q, tp[0]])
            bright = np.concatenate([bright, tp[1]])
        z = Q[:, 2]
        persp = 3.2 / (3.2 - z)
        rad = self.n * 0.40
        x = (self.n / 2 + Q[:, 0] * rad * persp).astype(np.int32)
        y = (self.n / 2 - Q[:, 1] * rad * persp).astype(np.int32)
        w = bright * (0.3 + 0.7 * (z + 1) / 2) * gain     # a hátsó oldal halványabb
        if tp is not None:
            k = len(tp[1])
            tx, ty, tw = x[-k:], y[-k:], w[-k:]
            x = np.concatenate([x, tx + 1, tx, tx + 1])
            y = np.concatenate([y, ty, ty + 1, ty + 1])
            w = np.concatenate([w, tw, tw, tw])
        ok = (x >= 0) & (x < self.n) & (y >= 0) & (y < self.n)
        buf = np.bincount(y[ok] * self.n + x[ok], weights=w[ok], minlength=self.n * self.n)
        I = np.clip(buf.reshape(self.n, self.n).astype(np.float32), 0, 4)
        L8 = Image.fromarray((np.clip(I, 0, 1) * 255).astype(np.uint8))
        g1 = np.asarray(L8.filter(self.blur_s), np.float32) / 255
        g2 = np.asarray(L8.filter(self.blur_l), np.float32) / 255
        tot = I * 1.15 + g1 * 2.0 + g2 * 3.6
        rgb = tot[..., None] * GOLD + np.clip(tot - 1.0, 0, 2)[..., None] * HOT
        rgb = np.clip(rgb * 255 + np.array(BG, np.float32), 0, 255).astype(np.uint8)
        return Image.fromarray(rgb, "RGB").convert("RGBA")


# ---------------------------------------------------------------- állapotjelentés a gömb körül
STATUS_S = 12


def status_items(d: dict) -> list[tuple[str, float, bool]]:
    """(felirat, ívhossz 0–100, rossz-e) – a „rossz" mérésenként más: a teli akku jó, a teli CPU nem."""
    cpu, ram = d.get("cpu_pct", 0), d.get("ram_pct", 0)
    items = [(f"CPU {cpu}%", cpu, cpu >= 85), (f"RAM {ram}%", ram, ram >= 85)]
    g = d.get("gpu")
    if g:
        items.append((f"GPU {g['pct']}% · {g['temp_c']}°", max(g["pct"], g["temp_c"] - 20),
                      g["pct"] >= 85 or g["temp_c"] >= 85))
    b = d.get("battery")
    if b:
        items.append((f"AKKU {b['pct']}%" + (" (töltő)" if b.get("plugged") else ""), b["pct"],
                      b["pct"] <= 20 and not b.get("plugged")))
    c = d.get("disk_c") or {}
    items.append((f"C: {c.get('free_gb', 0)} GB", c.get("pct", 0), c.get("pct", 0) >= 90))
    on = bool(d.get("online"))
    items.append(("ONLINE" if on else "OFFLINE", 100 if on else 0, not on))
    return items


def draw_status(img: Image.Image, items, age: float) -> None:
    """Arany ívek a gömb körül (mint a páncél diagnosztikája): feltöltődnek, majd elhalványulnak."""
    n = img.width
    k = 2                                                    # túlmintavétel a sima ívekért
    ov = Image.new("RGBA", (n * k, n * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    fill = 1 - (1 - min(1.0, age / 0.8)) ** 3
    fade = 1.0 if age < STATUS_S - 0.7 else max(0.0, (STATUS_S - age) / 0.7)
    c, R, w = n * k / 2, n * k * 0.462, max(2, int(n * k * 0.011))
    try:
        font = ImageFont.truetype("bahnschrift.ttf", int(n * k * 0.03))
    except OSError:
        font = ImageFont.load_default()
    seg = 360 / len(items)
    for i, (label, val, bad) in enumerate(items):
        a0 = -90 + i * seg + 4
        a1 = a0 + seg - 8
        box = (c - R, c - R, c + R, c + R)
        d.arc(box, a0, a1, fill=(120, 80, 30, int(150 * fade)), width=max(1, w // 3))
        col = (255, 96, 64) if bad else (255, 190, 80)
        d.arc(box, a0, a0 + (a1 - a0) * max(0.0, min(100.0, val)) / 100 * fill, fill=(*col, int(255 * fade)), width=w)
        mid = math.radians((a0 + a1) / 2)
        tx, ty = c + R * 0.87 * math.cos(mid), c + R * 0.87 * math.sin(mid)
        d.text((tx + 2, ty + 2), label, font=font, fill=(0, 0, 0, int(200 * fade)), anchor="mm")
        d.text((tx, ty), label, font=font, fill=(255, 236, 200, int(255 * fade)), anchor="mm")
    img.alpha_composite(ov.resize((n, n), Image.LANCZOS))


# ---------------------------------------------------------------- program ikonja (Windows shell)
def shell_image(name: str, size: int, icon_only: bool = True) -> Image.Image | None:
    """Egy fájl / program / „shell:AppsFolder\\…" képe az Intéző szerint (IShellItemImageFactory)."""
    import comtypes
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

    class IShellItemImageFactory(IUnknown):
        _iid_ = GUID("{BCC18B79-BA16-442F-80C4-8A59C30C463B}")
        _methods_ = [COMMETHOD([], HRESULT, "GetImage", (["in"], SIZE, "size"), (["in"], ctypes.c_int, "flags"),
                               (["out"], ctypes.POINTER(wintypes.HBITMAP), "phbm"))]

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                    ("bmWidthBytes", ctypes.c_long), ("bmPlanes", ctypes.c_ushort),
                    ("bmBitsPixel", ctypes.c_ushort), ("bmBits", ctypes.c_void_p)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", ctypes.c_ushort), ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]

    comtypes.CoInitialize()
    shell32, gdi32, user32 = ctypes.windll.shell32, ctypes.windll.gdi32, ctypes.windll.user32
    shell32.SHCreateItemFromParsingName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(GUID),
                                                    ctypes.POINTER(ctypes.c_void_p)]
    gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_uint]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    user32.GetDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    ptr = ctypes.c_void_p()
    if shell32.SHCreateItemFromParsingName(name, None, ctypes.byref(IShellItemImageFactory._iid_),
                                           ctypes.byref(ptr)) != 0 or not ptr.value:
        return None
    fac = ctypes.cast(ptr, ctypes.POINTER(IShellItemImageFactory))
    try:
        hbm = fac.GetImage(SIZE(size, size), 0x4 if icon_only else 0x0)  # SIIGBF_ICONONLY
    except OSError:
        return None
    try:
        bm = BITMAP()
        gdi32.GetObjectW(hbm, ctypes.sizeof(BITMAP), ctypes.byref(bm))
        w, h = bm.bmWidth, abs(bm.bmHeight)
        bi = BITMAPINFOHEADER(biSize=40, biWidth=w, biHeight=-h, biPlanes=1, biBitCount=32, biCompression=0)
        buf = ctypes.create_string_buffer(w * h * 4)
        hdc = user32.GetDC(None)
        gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(bi), 0)
        user32.ReleaseDC(None, hdc)
    finally:
        gdi32.DeleteObject(hbm)
    img = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1).copy()
    if img.getchannel("A").getextrema()[1] == 0:  # alfa nélküli kép
        img.putalpha(255)
    return img


_system = None


def _system_plugin():
    global _system
    if _system is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("system_plugin_jarvis", ROOT / "plugins" / "system" / "plugin.py")
        _system = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_system)
    return _system


def icon_for(target: str, size: int) -> tuple[Image.Image | None, str]:
    """A system_open célpontjának ikonja és neve (ugyanazzal a feloldással, mint a system_open)."""
    sysp = _system_plugin()
    r = sysp.resolve(target)
    if "error" in r:
        return None, ""
    how, what = r["run"]
    name = None
    if how == "appid":
        name = "shell:AppsFolder\\" + what
    elif how == "lnk":
        name = what
    elif how == "exe":
        name = what if os.path.isabs(what) else shutil.which(what)
    elif r["kind"] == "url":
        b = sysp._default_browser()
        name = b[1] if b else None
    elif r["kind"] in ("file", "folder"):
        name = what
    if not name:
        return None, r["name"]
    return shell_image(name, size, icon_only=r["kind"] != "file"), r["name"]


# ---------------------------------------------------------------- az ablak
class Jarvis:
    def __init__(self, parent_pid: int):
        self.parent_pid = parent_pid
        self.q = queue.Queue()
        self.rng = np.random.default_rng()
        self.px = _base_size()
        self.sphere = Sphere(self.px)
        self.state = "idle"
        self.yaw, self.pitch, self.roll, self.swirl = 0.6, -0.25, 0.0, 0.0
        self.v = np.zeros(3)                  # forgási sebesség (yaw, pitch, roll)
        self.v_goal, self.v_next = np.zeros(3), 0.0
        self.text, self.text_t = "", time.time()
        self.heard = ""
        self.env, self.env_t0, self.env_fps = [], 0.0, 30
        self.amp, self.idle_amp = 0.0, 0.0
        self.lines: list[list] = []          # [szöveg, fajta, kiírt karakterek]
        self.icon, self.icon_t = None, 0.0
        self.status, self.status_t = None, 0.0
        self.panel, self.panel_t, self.panel_alpha = None, 0.0, 0.0
        self.last_event = time.time()
        self.pinned = False
        self.visible = True
        self.dirty = True
        self.last_t = time.time()

        self.root = tk.Tk()
        self.root.title("Jarvis")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.985)
        bg = "#%02x%02x%02x" % BG
        self.root.configure(bg=bg)
        self.cv = tk.Canvas(self.root, bg=bg, highlightthickness=0, bd=0)
        self.cv.pack()
        self.img_id = self.cv.create_image(0, 0, anchor="nw")
        self.label_id = self.cv.create_text(0, 0, text="", font=("Bahnschrift", 12), fill="#c98a2e")
        self.font = tkfont.Font(font=("Consolas", 9))
        self.sep_id = self.cv.create_line(0, 0, 1, 1, fill="#4a3412")
        self.head_id = self.cv.create_text(0, 0, anchor="w", text="J.A.R.V.I.S  ·  NAPLÓ", font=("Bahnschrift", 8),
                                           fill="#8a6326")
        self.text_ids = [self.cv.create_text(0, 0, anchor="w", text="", font=self.font, fill="#ffb648")
                         for _ in range(6)]
        # gépelt kérés: beviteli sor (Enter küldi); a 🔇/🔊 kapcsolóval felolvasva is
        self.voice_on = False
        self.entry = tk.Entry(self.root, bg="#0d1117", fg="#fff3dc", insertbackground="#ffc45a", relief="flat",
                              font=("Consolas", 10), highlightthickness=1, highlightbackground="#4a3412",
                              highlightcolor="#ffb648")
        self.entry_win = self.cv.create_window(0, 0, anchor="w", window=self.entry)
        self.entry.bind("<Return>", self._send_typed)
        self.entry.bind("<Button-1>", lambda _e: (self.root.focus_force(), self.entry.focus_set()))
        self.entry.bind("<FocusIn>", lambda _e: self._placeholder(False))
        self.entry.bind("<FocusOut>", lambda _e: self._placeholder(True))
        self.voice_id = self.cv.create_text(0, 0, text="🔇", font=("Segoe UI Emoji", 12), fill="#c98a2e")
        self.cv.tag_bind(self.voice_id, "<Button-1>", self._toggle_voice)
        self._placeholder(True)
        self._layout()
        self.root.geometry(self._initial_pos())
        self.root.update_idletasks()
        self._round_corners()
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._motion)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.cv.bind("<Button-3>", self._menu)
        self.cv.bind("<MouseWheel>", self._wheel)
        self.menu = tk.Menu(self.root, tearoff=0)
        self.pin_var, self.listen_var = tk.BooleanVar(value=False), tk.BooleanVar(value=self._listening())
        self.menu.add_checkbutton(label="Mindig látszik", variable=self.pin_var, command=self._toggle_pin)
        self.menu.add_checkbutton(label="„Figyelj” hallgatás", variable=self.listen_var, command=self._toggle_listen)
        self.menu.add_separator()
        self.menu.add_command(label="Elrejtés", command=self._hide)
        self.menu.add_command(label="Bezárás", command=self.root.destroy)
        threading.Thread(target=self._udp, daemon=True).start()
        self.root.after(30, self._tick)
        if not TEST and self.parent_pid:
            self.root.after(2000, self._watch_parent)

    # ---- ablakkezelés
    def _layout(self) -> None:
        px = self.px
        self.cv.configure(width=px, height=px + TERM_H)
        self.cv.coords(self.label_id, px // 2, px - int(16 * DPI))
        y0 = px + int(4 * DPI)
        self.cv.coords(self.sep_id, int(10 * DPI), y0, px - int(10 * DPI), y0)
        self.cv.coords(self.head_id, int(12 * DPI), y0 + int(11 * DPI))
        for i, tid in enumerate(self.text_ids):
            self.cv.coords(tid, int(12 * DPI), px + int((30 + 15 * i) * DPI))
        ey = px + TERM_H - int(20 * DPI)
        self.cv.coords(self.entry_win, int(10 * DPI), ey)
        self.cv.itemconfigure(self.entry_win, width=px - int(52 * DPI), height=int(24 * DPI))
        self.cv.coords(self.voice_id, px - int(24 * DPI), ey)

    def _placeholder(self, show: bool) -> None:
        hint = "Írj ide… (Enter)"
        if show and not self.entry.get():
            self.entry.insert(0, hint)
            self.entry.configure(fg="#6b5530")
        elif not show and self.entry.get() == hint:
            self.entry.delete(0, "end")
            self.entry.configure(fg="#fff3dc")

    def _send_typed(self, _e=None):
        text = self.entry.get().strip()
        if not text or self.entry.cget("fg") == "#6b5530":
            return
        self.entry.delete(0, "end")
        hud.command(cmd="ask", text=text, voice=self.voice_on)

    def _toggle_voice(self, _e=None):
        self.voice_on = not self.voice_on
        self.cv.itemconfigure(self.voice_id, text="🔊" if self.voice_on else "🔇")

    def _wheel(self, e):
        """Görgő a gömb fölött: nagyítás / kicsinyítés (a méretet megjegyzi)."""
        if e.y >= self.px:
            return
        px = int(self.px * (1.08 if e.delta > 0 else 1 / 1.08))
        px = max(int(220 * DPI), min(int(1000 * DPI), px)) // 2 * 2
        if px == self.px:
            return
        self.px = px
        self.sphere.resize(px)
        self._layout()
        self.dirty = True
        self._save_pos()

    def _save_pos(self) -> None:
        try:
            POS.parent.mkdir(parents=True, exist_ok=True)
            POS.write_text(json.dumps({"x": self.root.winfo_x(), "y": self.root.winfo_y(), "size": self.px}), "utf-8")
        except OSError:
            pass

    def _initial_pos(self) -> str:
        try:
            p = json.loads(POS.read_text("utf-8"))
            return f"+{int(p['x'])}+{int(p['y'])}"
        except (OSError, ValueError, KeyError):
            sw = self.root.winfo_screenwidth()
            return f"+{sw - self.px - int(24 * DPI)}+{int(24 * DPI)}"   # jobb felső sarok (a folyamatjelző lent van)

    def _round_corners(self) -> None:
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            pref = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
        except Exception:  # noqa: BLE001
            pass

    def _press(self, e):
        self._mode = "rotate" if e.y < self.px else "move"
        self._x0, self._y0 = e.x_root, e.y_root
        self._moved = 0
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _motion(self, e):
        if self._mode == "rotate":                     # a gömb forgatása egérrel
            self._moved += abs(e.x_root - self._x0) + abs(e.y_root - self._y0)
            self.yaw += (e.x_root - self._x0) * 0.012
            self.pitch = max(-1.3, min(1.3, self.pitch + (e.y_root - self._y0) * 0.012))
            self._x0, self._y0 = e.x_root, e.y_root
            self.dirty = True
        else:
            self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _release(self, _e):
        if self._mode == "move":
            self._save_pos()
        elif self._moved < 5:                          # kattintás (húzás nélkül) a gömbre: hallgat
            hud.command(cmd="listen")

    def _menu(self, e):
        self.listen_var.set(self._listening())
        self.menu.tk_popup(e.x_root, e.y_root)

    def _toggle_pin(self):
        self.pinned = self.pin_var.get()
        if self.pinned:
            self._show()

    @staticmethod
    def _listening() -> bool:
        try:
            return bool(json.loads(LISTEN.read_text("utf-8")).get("on", True))
        except (OSError, ValueError, AttributeError):
            return True

    def _toggle_listen(self):
        LISTEN.parent.mkdir(parents=True, exist_ok=True)
        LISTEN.write_text(json.dumps({"on": bool(self.listen_var.get())}), "utf-8")

    def _show(self):
        if not self.visible:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.visible = True
            self.dirty = True

    def _hide(self):
        if self.visible:
            self.root.withdraw()
            self.visible = False

    def _watch_parent(self):
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, self.parent_pid)
        alive = False
        if h:
            code = wintypes.DWORD()
            k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            alive = code.value == 259
        if not alive:
            self.root.destroy()
            return
        self.root.after(2000, self._watch_parent)

    # ---- események
    def _udp(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for _ in range(40):                  # a régi példány még foghatja a portot
            try:
                s.bind(("127.0.0.1", hud.PORT))
                break
            except OSError:
                time.sleep(0.25)
        else:
            self.q.put({"ev": "quit"})       # más tartósan figyel ezen a porton
            return
        while True:
            data, _ = s.recvfrom(16384)
            try:
                self.q.put(json.loads(data.decode("utf-8")))
            except ValueError:
                pass

    def _line(self, text: str, kind: str):
        self.lines.append([text, kind, 0])
        self.lines = self.lines[-6:]

    def _set_state(self, st: str):
        if st in STATES and st != self.state:
            self.state = st
            self.dirty = True

    def _event(self, m: dict):
        ev = m.get("ev")
        now = time.time()
        if ev == "quit":
            self.root.destroy()
            return
        if ev == "level":              # a mikrofonszintre szándékosan nem mozog: csak a saját beszédére
            return
        self.last_event = now
        if ev == "state":
            self._set_state(m.get("state", "idle"))
            st = self.state
            if st == "think" and self.heard:
                self._set_text("GONDOLKODOM · " + self.heard)
            elif st in ("wake", "listen", "think", "off", "error"):
                self._set_text(STATES[st][0])
            elif st == "idle":
                self.heard = ""
            if self.state in ("wake", "listen", "think", "tool", "speak", "error"):
                self._show()
        elif ev == "heard":
            self._line("› " + m.get("text", ""), "heard")
            self.heard = m.get("text", "").strip()
            self._set_text(self.heard)
            self._show()
        elif ev == "tool":
            args = m.get("input") or {}
            inner = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in list(args.items())[:3])
            self._line(f"λ {m.get('name', '?')}({inner})", "tool")
            first = next(iter(args.values()), "") if args else ""
            tail = f" · {first}" if isinstance(first, (str, int, float)) and first != "" else ""
            self._set_text(f"λ {m.get('name', '?')}{tail}")
            self._set_state("tool")
            self._show()
            if m.get("name", "").endswith("system_open") and args.get("target"):
                threading.Thread(target=self._load_icon, args=(str(args["target"]),), daemon=True).start()
        elif ev == "result":
            ok = m.get("ok", True)
            self._line(("✓ " if ok else "✗ ") + m.get("text", ""), "ok" if ok else "fail")
        elif ev == "say":
            self._line("« " + m.get("text", ""), "say")
            self._set_text(m.get("text", ""))
            env = m.get("env") or []
            if env:
                self.env, self.env_t0, self.env_fps = env, now, int(m.get("fps", 30))
            self._set_state("speak")
            self._show()
        elif ev == "notify":               # magától szól: jelzés a gépről / kész munkáról
            text = m.get("text", "")
            self._line("! " + text, "note")
            self._set_text(text)
            if m.get("env"):                   # csendben: a gömb a szöveg ritmusára „mondja"
                self.env, self.env_t0, self.env_fps = m["env"], now, int(m.get("fps", 30))
                self._set_state("speak")
            self._show()
        elif ev == "status" and isinstance(m.get("data"), dict):
            self.status, self.status_t = status_items(m["data"]), now
            self._line("λ állapot: " + " · ".join(lbl for lbl, _v, _b in self.status), "tool")
            self._show()
        elif ev == "project" and m.get("path"):
            self._project(str(m["path"]), str(m.get("title") or Path(str(m["path"])).name))
        elif ev == "hush":             # elhallgattatták
            self.env = []
        elif ev == "icon" and m.get("target"):
            threading.Thread(target=self._load_icon, args=(str(m["target"]),), daemon=True).start()

    # ---- hologram-kivetítés: kép arany keretben az ablak mellett
    PANEL_S = 12

    def _project(self, path: str, title: str) -> None:
        try:
            img = Image.open(path).convert("RGBA")
        except OSError:
            return
        maxw, maxh = int(self.px * 1.1), int(self.px * 0.95)
        img.thumbnail((maxw, maxh), Image.LANCZOS)
        pad, head = int(10 * DPI), int(24 * DPI)
        pw, ph = img.width + 2 * pad, img.height + 2 * pad + head
        if self.panel is None:
            self.panel = tk.Toplevel(self.root)
            self.panel.overrideredirect(True)
            self.panel.attributes("-topmost", True)
            self.pcv = tk.Canvas(self.panel, bg="#%02x%02x%02x" % BG, highlightthickness=0, bd=0)
            self.pcv.pack()
            self.pcv.bind("<Button-1>", lambda _e: self._panel_close())
        self.pcv.delete("all")
        self.pcv.configure(width=pw, height=ph)
        self.pcv.create_rectangle(1, 1, pw - 2, ph - 2, outline="#ffb648", width=max(1, int(DPI)))
        self.pcv.create_line(pad, head, pw - pad, head, fill="#4a3412")
        self.pcv.create_text(pad, head // 2 + 1, anchor="w", text="▣ " + title[:60], fill="#ffc45a",
                             font=("Bahnschrift", 9))
        self._panel_img = ImageTk.PhotoImage(img)
        self.pcv.create_image(pad, head + pad, anchor="nw", image=self._panel_img)
        rx, ry = self.root.winfo_x(), self.root.winfo_y()
        x = rx - pw - int(12 * DPI)
        if x < 0:                                            # bal oldalt nincs hely: jobbra
            x = rx + self.root.winfo_width() + int(12 * DPI)
        self.panel.geometry(f"{pw}x{ph}+{x}+{ry}")
        self.panel_alpha = 0.0
        self.panel.attributes("-alpha", 0.0)
        self.panel.deiconify()
        self.panel_t = time.time()
        self._line("▣ kivetítés: " + title, "tool")
        self._show()

    def _panel_close(self) -> None:
        self.panel_t = time.time() - self.PANEL_S + 0.4       # gyors elhalványulás

    def _panel_tick(self, t: float) -> None:
        if self.panel is None or not self.panel.winfo_viewable():
            return
        age = t - self.panel_t
        target = 0.97 if age < self.PANEL_S - 0.4 else 0.0
        self.panel_alpha += (target - self.panel_alpha) * 0.3
        if target == 0.0 and self.panel_alpha < 0.03:
            self.panel.withdraw()
            return
        self.panel.attributes("-alpha", round(self.panel_alpha, 3))

    def _load_icon(self, target: str):
        try:
            img, _name = icon_for(target, int(self.px * 0.3))
        except Exception:  # noqa: BLE001 – az ikon csak díszítés
            img = None
        if img is not None:
            self.q.put({"ev": "_icon", "img": img})

    # ---- rajzolás
    def _set_text(self, text: str) -> None:
        if text != self.text:
            self.text, self.text_t = text, time.time()
            self.dirty = True

    def _sphere_text(self, t: float) -> str:
        if self.state in ("idle", "done") and t - self.text_t > 6:
            return time.strftime("%H:%M")                      # tétlenül az óra
        return self.text

    def _speech_amp(self, t: float) -> float:
        if not self.env:
            return 0.0
        i = int((t - self.env_t0) * self.env_fps)
        if i >= len(self.env):
            self.env = []
            if self.state == "speak":
                self._set_state("done")
            return 0.0
        return float(self.env[max(0, i)])

    def _frame(self, t: float) -> Image.Image:
        _label, gain, _col = STATES.get(self.state, STATES["idle"])
        img = self.sphere.render(self.yaw, self.pitch, self.swirl, max(self.amp, self.idle_amp), t, gain, self.rng,
                                 roll=self.roll, text=self._sphere_text(t), text_age=t - self.text_t)
        if self.icon is not None and t - self.icon_t < 6:
            age = t - self.icon_t
            k = 1 - (1 - min(1.0, age / 0.35)) ** 3            # gyors kinyílás
            fade = 1.0 if age < 5.3 else max(0.0, (6 - age) / 0.7)
            s = max(1, int(self.icon.width * (0.6 + 0.4 * k)))
            ic = self.icon.resize((s, s), Image.LANCZOS)
            if fade < 1:
                ic.putalpha(ic.getchannel("A").point(lambda v: int(v * fade)))
            img.alpha_composite(ic, (self.px // 2 - s // 2, self.px // 2 - s // 2))
        if self.status and t - self.status_t < STATUS_S:
            draw_status(img, self.status, t - self.status_t)
        return img

    def _draw_terminal(self):
        maxw = self.px - int(24 * DPI)
        for i, tid in enumerate(self.text_ids):
            if i < len(self.lines):
                text, kind, shown = self.lines[i]
                if shown < len(text):
                    self.lines[i][2] = shown = min(len(text), shown + 3)
                s = text[:shown]
                while len(s) > 3 and self.font.measure(s) > maxw:
                    s = s[:-2] + "…"
                cursor = "▌" if shown < len(text) and i == len(self.lines) - 1 else ""
                self.cv.itemconfigure(tid, text=s + cursor, fill=LINE_COL.get(kind, "#ffb648"))
            else:
                self.cv.itemconfigure(tid, text="")

    def _tick(self):
        t = time.time()
        dt, self.last_t = min(0.1, t - self.last_t), t
        while True:
            try:
                m = self.q.get_nowait()
            except queue.Empty:
                break
            if m.get("ev") == "_icon":
                self.icon, self.icon_t = m["img"], t
                self._show()
            else:
                self._event(m)
        target = self._speech_amp(t)
        # gyors felfutás, lassú lecsengés: élő, nem gépies mozgás
        self.amp += (target - self.amp) * (0.55 if target > self.amp else 0.12)
        if self.amp < 0.004:
            self.amp = 0.0
        # állandó, finom élet: lassú forgás és lélegzés; beszéd közben erre jön rá az erős mozgás
        calm = 0.35 if self.state == "off" else 1.0
        # beszéd közben véletlenszerű bolyongás: időnként új, véletlen forgási cél mindhárom tengelyen, simán
        # ráállva; egy rugó a dőlést és a csavarást az alaphelyzet közelében tartja
        if t >= self.v_next:
            self.v_goal = self.rng.normal(0, 1, 3) * np.array([1.4, 0.9, 0.7])
            self.v_next = t + self.rng.uniform(0.35, 1.3)
        goal = self.v_goal * self.amp
        goal[0] += 0.05 * calm                                       # tétlenül: lassú, egyenletes forgás
        goal[1] += (-0.25 - self.pitch) * 0.8
        goal[2] += (0.0 - self.roll) * 0.8
        self.v += (goal - self.v) * min(1.0, dt * 3.5)
        self.yaw += dt * self.v[0]
        self.pitch = max(-1.3, min(1.3, self.pitch + dt * self.v[1]))
        self.roll += dt * self.v[2]
        self.swirl += dt * (0.12 * calm + 2.2 * self.amp)
        self.idle_amp = calm * 0.11 * (0.65 + 0.35 * math.sin(t * 0.85))
        if self.visible:
            self.dirty = True
        icon_anim = (self.icon is not None and t - self.icon_t < 6.2) or (self.status and t - self.status_t < STATUS_S)
        if icon_anim:
            self.dirty = True
        panel_on = self.panel is not None and t - self.panel_t < self.PANEL_S
        hide = (not self.pinned and not TEST and self.state in ("idle", "done", "off") and not icon_anim and not panel_on
                and self.amp == 0 and t - self.last_event > HIDE_S)
        if hide:
            self._hide()
        self._panel_tick(t)
        if self.visible:
            self.cv.itemconfigure(self.label_id, text="")       # a felirat a gömbön van
            if self.dirty:
                self._photo = ImageTk.PhotoImage(self._frame(t))
                self.cv.itemconfigure(self.img_id, image=self._photo)
                self.dirty = False
            self._draw_terminal()
        busy = self.amp > 0 or icon_anim or any(sh < len(tx) for tx, _k, sh in self.lines)
        self.root.after(33 if busy else 83, self._tick)       # tétlenül 12 kép/mp elég a lassú mozgáshoz

    def run(self):
        if TEST:
            self.root.after(300, self._demo)
        self.root.mainloop()

    def _demo(self):
        """Bemutató hálózat nélkül: python jarvis.py --demo (a beszédet egy kitalált burkoló helyettesíti)."""
        env = [round(max(0.0, math.sin(i * 0.33)) * (0.5 + 0.5 * abs(math.sin(i * 0.05))), 2) for i in range(150)]
        seq = [(0.0, {"ev": "state", "state": "wake"}), (0.8, {"ev": "state", "state": "listen"}),
               (2.6, {"ev": "heard", "text": "Nyisd meg a Chrome-ot!"}), (2.7, {"ev": "state", "state": "think"}),
               (3.8, {"ev": "tool", "name": "system_open", "input": {"target": "chrome"}}),
               (4.3, {"ev": "result", "name": "system_open", "text": "app: Google Chrome", "ok": True}),
               (4.6, {"ev": "say", "text": "Megnyitottam a Google Chrome-ot.", "env": env, "fps": 30}),
               (10.0, {"ev": "state", "state": "idle"})]
        t0 = time.time()

        def step():
            while seq and time.time() - t0 >= seq[0][0]:
                self.q.put(seq.pop(0)[1])
            if seq:
                self.root.after(30, step)
        step()


if __name__ == "__main__":
    pid = int(next((a for a in sys.argv[1:] if a.isdigit()), "0"))
    Jarvis(pid).run()
