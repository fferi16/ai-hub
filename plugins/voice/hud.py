"""Események a Jarvis-ablaknak (jarvis.py): egy JSON-datagram UDP-n a 127.0.0.1-re, várakozás nélkül.
Ha az ablak nem fut, a csomag elvész – a küldőt ez semmiben nem zavarja.

    hud.send("state", state="listen")          # idle | wake | listen | think | tool | speak | done | off | error
    hud.send("level", v=0.42)                  # mikrofonszint 0–1 (a gyűrű rezgése)
    hud.send("heard", text="Nyisd meg a Chrome-ot")
    hud.send("tool", name="system_open", input={"target": "chrome"})
    hud.send("result", name="system_open", text="app: Google Chrome", ok=True)
    hud.send("say", text="Megnyitottam.")
"""
import json
import socket
import time

PORT = 8771
CMD_PORT = 8772          # visszafelé: az ablak → a diktáló (kattintásra hallgat, gépelt kérés)
_sock = None
_last_level = 0.0
VOWELS = set("aáeéiíoóöőuúüűAÁEÉIÍOÓÖŐUÚÜŰ")


def command(**kw) -> None:
    """Parancs a diktálónak: command(cmd="listen") vagy command(cmd="ask", text="…", voice=False)."""
    global _sock
    try:
        if _sock is None:
            _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _sock.sendto(json.dumps(kw, ensure_ascii=False).encode("utf-8")[:8000], ("127.0.0.1", CMD_PORT))
    except OSError:
        pass


def text_envelope(text: str, fps: int = 30) -> list[float]:
    """Kitalált „beszéd-burkoló" a szöveg ritmusából (magánhangzó = szótagcsúcs) – néma válasznál így is mozog."""
    vals = []
    for ch in text:
        v = 0.9 if ch in VOWELS else 0.05 if not ch.isalnum() else 0.45
        vals += [v, v * 0.8]                                # ~15 betű/mp
    sm, out = 0.0, []
    for v in vals:
        sm += (v - sm) * 0.5
        out.append(round(sm, 2))
    return out[:900]


def send(ev: str, **kw) -> None:
    global _sock
    try:
        if _sock is None:
            _sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _sock.sendto(json.dumps({"ev": ev, **kw}, ensure_ascii=False).encode("utf-8")[:8000], ("127.0.0.1", PORT))
    except OSError:
        pass


def envelope(wav: bytes, fps: int = 30) -> list[float]:
    """A felolvasott hang hangerő-burkolója (0–1, fps érték/mp) – a Jarvis-gömb pixelei ennek ütemére mozognak."""
    import io
    import wave

    import numpy as np
    with wave.open(io.BytesIO(wav)) as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        return []
    a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    hop = max(1, sr // fps)
    n = len(a) // hop
    if not n:
        return []
    rms = np.sqrt((a[: n * hop].reshape(n, hop) ** 2).mean(axis=1))
    peak = float(np.percentile(rms, 95)) or 1.0
    return [round(float(v), 2) for v in np.clip(rms / peak, 0, 1)[:900]]


def level(rms: float) -> None:
    """Mikrofonszint, legfeljebb 15-ször másodpercenként (a beszéd ~0,02–0,2 RMS → 0–1)."""
    global _last_level
    now = time.time()
    if now - _last_level >= 0.066:
        _last_level = now
        send("level", v=round(min(1.0, (max(0.0, rms) / 0.12) ** 0.6), 3))
