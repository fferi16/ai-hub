"""Hub – lebegő folyamatjelző. Indítás: hubkit.show_widget()  (pythonw widget.py [--test <mappa>])

A közös folyamatmappát (out\\progress\\*.json, hubkit.progress írja) mutatja:
minden futó munka egy sor százalékkal / mozgó csíkkal, eltelt és hátralévő idővel.
Mindig felül, a fejlécnél fogva áthelyezhető, a jobb alsó sarokban indul.
Egy példány fut (Windows mutex); ha 60 mp-ig nincs mit mutatni, bezárul.
"""
import ctypes
import json
import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEST = "--test" in sys.argv
PDIR = Path(sys.argv[sys.argv.index("--test") + 1]) if TEST else ROOT / "out" / "progress"
IDLE_CLOSE_S = 4 if TEST else 60
STALE_S = 2 * 3600  # ennyi ideje nem frissült futó bejegyzés → elhagyott, nem mutatjuk
W = 340
BG, FG, MUTED, TRACK = "#221a1f", "#f4ecef", "#b3a3aa", "#3d3037"
COL = {"running": "#d0628f", "done": "#4caf7a", "error": "#e05a4f"}

if not TEST:
    ctypes.windll.kernel32.CreateMutexW(None, False, "Hub.progress.widget")
    if ctypes.windll.kernel32.GetLastError() == 183:  # már fut
        sys.exit(0)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:  # noqa: BLE001
    pass


def dur(sec: float) -> str:
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec} mp"
    m = sec // 60
    return f"{m // 60} ó {m % 60} perc" if m >= 60 else f"{m} perc"


def rows(now: float) -> list[dict]:
    out = []
    for f in sorted(PDIR.glob("*.json")) if PDIR.exists() else []:
        try:
            r = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        age_upd = now - r.get("updated", now)
        if age_upd > 86400 and not TEST:  # egy napnál régebbi: takarítás
            f.unlink(missing_ok=True)
            continue
        state, pct = r.get("state", "running"), r.get("pct")
        run_s = (r.get("updated", now) if state != "running" else now) - r.get("started", now)
        if state == "running":
            if run_s < r.get("show_after_s", 0) or age_upd > STALE_S:
                continue
            parts = [r.get("detail") or "fut"]
            if pct is not None and pct > 2:
                parts.append(f"kb. {dur(run_s * (100 - pct) / pct)} van hátra")
            else:
                parts.append(dur(run_s))
            detail = " · ".join(p for p in parts if p)
        else:
            if age_upd > r.get("keep_s", 600) or run_s < r.get("show_after_s", 0):
                if not TEST and age_upd > r.get("keep_s", 600):
                    f.unlink(missing_ok=True)  # lejárt: takarítás
                continue
            detail = (r.get("detail") or ("Kész" if state == "done" else "Hiba")) + f" · {dur(run_s)}"
            pct = 100 if state == "done" else pct
        out.append({"title": r.get("title", f.stem), "state": state, "pct": pct, "detail": detail, "t": r.get("started", 0)})
    return sorted(out, key=lambda x: x["t"])


root = tk.Tk()
root.overrideredirect(True)
root.attributes("-topmost", True)
root.attributes("-alpha", 0.94)
root.configure(bg=BG)
scale = root.winfo_fpixels("1i") / 96

head = tk.Frame(root, bg=BG)
head.pack(fill="x", padx=12, pady=(10, 4))
title_lbl = tk.Label(head, text="Hub – folyamatok", bg=BG, fg=FG, font=("Segoe UI Semibold", 10))
title_lbl.pack(side="left")
close = tk.Label(head, text="✕", bg=BG, fg=MUTED, font=("Segoe UI", 10), cursor="hand2")
close.pack(side="right")
close.bind("<Button-1>", lambda e: root.destroy())
body = tk.Frame(root, bg=BG)
body.pack(fill="both", padx=12, pady=(0, 10))

drag, pos = {}, {}


def on_press(e):
    drag.update(x=e.x_root - root.winfo_x(), y=e.y_root - root.winfo_y())


def on_drag(e):
    x, y = e.x_root - drag["x"], e.y_root - drag["y"]
    pos.update(x=x, bottom=y + root.winfo_height())
    root.geometry(f"+{x}+{y}")


for w in (head, title_lbl):
    w.bind("<ButtonPress-1>", on_press)
    w.bind("<B1-Motion>", on_drag)


def work_area():
    class RECT(ctypes.Structure):
        _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]
    r = RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)
    return r.r, r.b


def short(t: str, n: int) -> str:
    return t if len(t) <= n else t[: n - 1] + "…"


state = {"idle": 0.0, "tick": 0, "sig": None, "bars": []}


def build(items):
    for c in body.winfo_children():
        c.destroy()
    state["bars"] = []
    if not items:
        tk.Label(body, text="Nincs futó folyamat.", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
    bw, bh = int((W - 24) * scale), int(8 * scale)
    for it in items:
        f = tk.Frame(body, bg=BG)
        f.pack(fill="x", pady=(6, 2))
        top = tk.Frame(f, bg=BG)
        top.pack(fill="x")
        tk.Label(top, text=short(it["title"], 40), bg=BG, fg=FG, font=("Segoe UI", 9)).pack(side="left")
        right = tk.Label(top, bg=BG, fg=COL[it["state"]] if it["state"] in COL else FG, font=("Segoe UI Semibold", 9))
        right.pack(side="right")
        cv = tk.Canvas(f, width=bw, height=bh, bg=TRACK, highlightthickness=0)
        cv.pack(fill="x", pady=(3, 2))
        sub = tk.Label(f, bg=BG, fg=MUTED, font=("Segoe UI", 8))
        sub.pack(anchor="w")
        state["bars"].append((it, right, cv, sub, bw, bh))
    root.update_idletasks()
    h = root.winfo_reqheight()
    if not pos:
        r, b = work_area()
        pos.update(x=r - int(W * scale) - 16, bottom=b - 16)
    root.geometry(f"{int(W * scale)}x{h}+{pos['x']}+{pos['bottom'] - h}")


def paint(items):
    for (old, right, cv, sub, bw, bh), it in zip(state["bars"], items):
        mark = {"done": " ✅", "error": " ❌"}.get(it["state"], "")
        right.configure(text=(f"{int(round(it['pct']))}%" if it["pct"] is not None else "…") + mark)
        sub.configure(text=short(it["detail"], 56))
        cv.delete("all")
        col = COL.get(it["state"], FG)
        if it["pct"] is None:  # nincs százalék: mozgó csík
            seg = bw // 4
            x = (state["tick"] * bw // 20) % (bw + seg) - seg
            cv.create_rectangle(max(0, x), 0, min(bw, x + seg), bh, fill=col, width=0)
        else:
            cv.create_rectangle(0, 0, int(bw * it["pct"] / 100), bh, fill=col, width=0)


def loop():
    state["tick"] += 1
    items = rows(time.time())
    sig = [(i["title"], i["state"], i["t"]) for i in items]
    if sig != state["sig"]:
        state["sig"] = sig
        build(items)
    paint(items)
    state["idle"] = state["idle"] + 0.2 if not items else 0
    if state["idle"] >= IDLE_CLOSE_S:
        root.destroy()
        return
    root.after(200, loop)


root.after(0, loop)
if TEST:
    root.after(6000, root.destroy)
root.mainloop()
