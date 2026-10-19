"""Haladás-értesítő: python notify.py <step_pct> <dry 0|1> <job-mappa>...
Windows-értesítést küld minden step_pct %-nál, és amikor egy job kész vagy hibás; utána kilép."""
import json
import sys
import time
from pathlib import Path

STEP = max(1, int(sys.argv[1]))
DRY = sys.argv[2] == "1"
JOBS = [Path(a) for a in sys.argv[3:]]
NAMES = {"transcribe": "Leirat", "frames": "Képkockák"}


def toast(title: str, text: str) -> None:
    if DRY:
        print("TOAST", title, "|", text, flush=True)
        return
    from windows_toasts import Toast, WindowsToaster
    WindowsToaster("Hub – media").show_toast(Toast([title, text]))


def label(jdir: Path) -> str:
    spec = json.loads((jdir / "spec.json").read_text("utf-8"))
    stem = Path(spec["src"]).stem
    return f"{NAMES.get(spec['kind'], spec['kind'])}: {stem[:40]}{'…' if len(stem) > 40 else ''}"


def read(jdir: Path) -> dict:
    try:
        return json.loads((jdir / "status.json").read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


sent = {j: -1 for j in JOBS}
while sent:
    for j in list(sent):
        st = read(j)
        state, prog = st.get("state"), float(st.get("progress") or 0)
        if state == "done":
            extra = f"{st['segments']} szakasz, {st.get('elapsed_s', 0) // 60} perc alatt" if "segments" in st \
                else f"{st.get('kept', '?')} kép megtartva"
            toast(f"✅ Kész – {label(j)}", extra)
            del sent[j]
        elif state == "error":
            toast(f"❌ Hiba – {label(j)}", str(st.get("error", ""))[:200])
            del sent[j]
        elif int(prog * 100) // STEP * STEP > sent[j] and prog > 0:
            pct = int(prog * 100) // STEP * STEP
            sent[j] = pct
            eta = ""
            if st.get("elapsed_s") and prog > 0.02:
                eta = f", kb. {int(st['elapsed_s'] * (1 - prog) / prog / 60)} perc van hátra"
            toast(f"{pct}% – {label(j)}", f"{st.get('step', '')} {st.get('at', '')}{eta}".strip())
    if sent:
        time.sleep(5)
