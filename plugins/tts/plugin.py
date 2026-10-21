"""tts – magyar felolvasás a Windows beépített hangjával (Microsoft Szabolcs, hu-HU), helyben, ingyen.

  tts_speak  – azonnal felolvassa a hangszórón (háttérben szól; tts_stop leállítja)
  tts_save   – szöveg → WAV vagy MP3 fájl
  tts_file   – dokumentum (txt/md/docx/pdf/…) → hangfájl; hosszú szöveg bekezdésenként, folyamatjelzővel
  tts_voices – telepített hangok
A Markdown jelölőit (#, *, `, linkek) nem olvassa fel. Sebesség (rate 0.5–3), hangmagasság (pitch 0–2).
Más nyelvű hang a voice paraméterrel (név vagy nyelvkód, pl. 'en-US'). MP3-hoz a media plugin ffmpeg-je kell.
"""
import asyncio
import io
import re
import subprocess
import time
import wave
from pathlib import Path

from hubkit import progress, tool

OUT = Path(__file__).resolve().parents[2] / "out" / "tts"
MAX_SAY = 20000          # tts_speak / tts_save szöveghatára (karakter)
MAX_FILE = 500000        # tts_file szöveghatára
CHUNK = 3000             # ekkora darabokban szintetizálunk (bekezdéshatáron)
DOC_EXT = {".txt", ".md", ".srt", ".docx", ".pdf", ".pptx", ".html", ".htm", ".epub", ".msg"}


def _voices():
    from winrt.windows.media.speechsynthesis import SpeechSynthesizer
    return list(SpeechSynthesizer.all_voices)


def _pick(voice: str):
    vs = _voices()
    want = (voice or "hu-HU").lower()
    for v in vs:
        if want in (v.display_name.lower(), v.language.lower()) or want in v.display_name.lower():
            return v
    for v in vs:
        if v.language.lower().startswith(want[:2]):
            return v
    return None


def clean(text: str) -> str:
    """Markdown/jelölések ki, hogy ne olvassa fel őket; sorvégek egyszerűsítve."""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)                 # kódblokk
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)                      # kép
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)                    # link → csak a szöveg
    t = re.sub(r"https?://\S+", " ", t)                              # puszta URL
    t = re.sub(r"^\s{0,3}(#{1,6}|>|[-*+]|\d+\.)\s+", "", t, flags=re.M)  # címsor, idézet, felsorolás
    t = re.sub(r"[*_`|]+", " ", t)                                   # kiemelés, kód, táblázat
    t = re.sub(r"^\s*-{3,}\s*$", " ", t, flags=re.M)                  # vízszintes vonal
    t = re.sub(r"[ \t\xa0]+", " ", t.replace("\xad", ""))
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()


def _chunks(text: str, size: int = CHUNK) -> list[str]:
    parts, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > size:  # túl hosszú bekezdés: mondathatáron vágjuk
            cut = max(para.rfind(". ", 0, size), para.rfind("! ", 0, size), para.rfind("? ", 0, size))
            cut = cut + 1 if cut > size // 3 else size
            parts.append(para[:cut].strip())
            para = para[cut:]
        if cur and len(cur) + len(para) + 2 > size:
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur.strip():
        parts.append(cur)
    return [p for p in parts if p.strip()]


async def _synth(texts: list[str], voice, rate: float, pitch: float, volume: float, on_part=None) -> bytes:
    from winrt.windows.media.speechsynthesis import SpeechSynthesizer
    from winrt.windows.storage.streams import DataReader

    s = SpeechSynthesizer()
    s.voice = voice
    s.options.speaking_rate = rate
    s.options.audio_pitch = pitch
    s.options.audio_volume = volume
    params, frames = None, []
    for i, t in enumerate(texts):
        st = await s.synthesize_text_to_stream_async(t)
        n = st.size
        r = DataReader(st.get_input_stream_at(0))
        await r.load_async(n)
        buf = bytearray(n)
        r.read_bytes(buf)
        with wave.open(io.BytesIO(bytes(buf))) as w:
            params = params or w.getparams()
            frames.append(w.readframes(w.getnframes()))
        if on_part:
            on_part(i + 1, len(texts))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams(params)
        for f in frames:
            w.writeframes(f)
    return out.getvalue()


def _render(text: str, voice: str, rate: float, pitch: float, volume: float, on_part=None):
    v = _pick(voice)
    if v is None:
        return None, {"ok": False, "reason": f"nincs ilyen hang: {voice or 'hu-HU'}", "hint": "tts_voices"}
    parts = _chunks(text)
    if not parts:
        return None, {"ok": False, "reason": "nincs felolvasható szöveg"}
    rate = max(0.5, min(float(rate), 3.0))
    pitch = max(0.0, min(float(pitch), 2.0))
    volume = max(0.0, min(float(volume), 1.0))
    data = asyncio.run(_synth(parts, v, rate, pitch, volume, on_part))
    return (data, v.display_name, len(parts)), None


def _duration(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav)) as w:
        return round(w.getnframes() / w.getframerate(), 1)


def _write(wav: bytes, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix.lower() == ".mp3":
        import imageio_ffmpeg
        r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
                            "-codec:a", "libmp3lame", "-q:a", "4", str(target)], input=wav, capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=300)
        if r.returncode != 0:
            raise RuntimeError("MP3-kódolás: " + r.stderr.decode("utf-8", "replace")[-200:])
    else:
        target.write_bytes(wav)
    return target


def _target(out: str, stem: str, fmt: str) -> Path | None:
    fmt = (fmt or "wav").lower().lstrip(".")
    p = Path(out).expanduser() if out else OUT / f"{stem}.{fmt}"
    return p if p.suffix.lower() in (".wav", ".mp3") else None


def _stem(text: str) -> str:
    words = re.findall(r"\w+", text)[:5]
    return (time.strftime("%Y%m%d-%H%M%S_") + "_".join(words))[:80]


@tool
def tts_voices() -> dict:
    """Telepített felolvasó hangok (név, nyelv); az alap a magyar (Microsoft Szabolcs)."""
    try:
        vs = _voices()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"a Windows beszédszintézis nem érhető el: {type(e).__name__}: {e}"[:200]}
    return {"ok": True, "voices": [{"name": v.display_name, "lang": v.language} for v in vs]}


@tool
def tts_save(text: str, out: str = "", format: str = "wav", voice: str = "", rate: float = 1.0,  # noqa: A002
             pitch: float = 1.0, volume: float = 1.0) -> dict:
    """Szöveg felolvasva hangfájlba (WAV/MP3), magyar hanggal; out kiterjesztése dönt, különben format."""
    t = clean(text or "")
    if not t:
        return {"ok": False, "reason": "üres szöveg"}
    if len(t) > MAX_SAY:
        return {"ok": False, "reason": f"túl hosszú (>{MAX_SAY} karakter) – fájlba írva tts_file"}
    target = _target(out, _stem(t), format)
    if target is None:
        return {"ok": False, "reason": "a kimenet .wav vagy .mp3 legyen"}
    res, err = _render(t, voice, rate, pitch, volume)
    if err:
        return err
    wav, name, parts = res
    _write(wav, target)
    return {"ok": True, "path": str(target), "voice": name, "seconds": _duration(wav),
            "kb": round(target.stat().st_size / 1024)}


_player: dict = {}


@tool
def tts_speak(text: str, voice: str = "", rate: float = 1.0, pitch: float = 1.0) -> dict:
    """Azonnal felolvassa a hangszórón (háttérben szól, a hívás nem vár a végére); tts_stop leállítja."""
    import winsound

    t = clean(text or "")
    if not t:
        return {"ok": False, "reason": "üres szöveg"}
    if len(t) > MAX_SAY:
        return {"ok": False, "reason": f"túl hosszú (>{MAX_SAY} karakter) – tts_file, majd lejátszás"}
    res, err = _render(t, voice, rate, pitch, 1.0)
    if err:
        return err
    wav, name, _ = res
    path = OUT / "_most.wav"  # a lejátszó fájlból játssza le aszinkron (memóriából nem tud)
    tts_stop()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav)
    winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    _player.update(path=path, until=time.time() + _duration(wav))
    _signal(_player["until"])
    return {"ok": True, "voice": name, "seconds": _duration(wav), "playing": True}


@tool
def tts_stop() -> dict:
    """A tts_speak felolvasásának leállítása."""
    import winsound

    was = time.time() < _player.get("until", 0)
    winsound.PlaySound(None, 0)
    _player["until"] = 0
    _signal(0)
    return {"ok": True, "stopped": was}


SPEAKING = OUT / "_szol.json"


def _signal(until: float) -> None:
    """Meddig szól a felolvasás – a voice ébresztőszó-figyelése ezalatt szünetel (ne ébredjen a saját hangjára)."""
    try:
        SPEAKING.parent.mkdir(parents=True, exist_ok=True)
        SPEAKING.write_text('{"until": %.2f}' % until, "utf-8")
    except OSError:
        pass


def _read_doc(p: Path) -> str:
    if p.suffix.lower() in (".txt", ".md", ".srt"):
        raw = p.read_bytes()
        for enc in ("utf-8-sig", "cp1250"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", "replace")
        if p.suffix.lower() == ".srt":  # feliratból csak a szöveg
            text = re.sub(r"^\d+\s*$|^\d\d:\d\d:\d\d[,.]\d+ --> .*$", "", text, flags=re.M)
        return text
    from markitdown import MarkItDown
    return MarkItDown(enable_plugins=False).convert(str(p)).text_content or ""


@tool
def tts_file(path: str, out: str = "", format: str = "mp3", voice: str = "", rate: float = 1.0,  # noqa: A002
             pitch: float = 1.0) -> dict:
    """Dokumentum (txt/md/srt/docx/pdf/pptx/html/epub) felolvasva hangfájlba; hosszú szöveg darabolva, folyamatjelzővel."""
    p = Path(path).expanduser()
    if not p.is_file():
        return {"ok": False, "reason": f"nincs ilyen fájl: {path}"}
    if p.suffix.lower() not in DOC_EXT:
        return {"ok": False, "reason": f"nem támogatott formátum: {p.suffix}", "supported": sorted(DOC_EXT)}
    target = _target(out, p.stem[:80], format)
    if target is None:
        return {"ok": False, "reason": "a kimenet .wav vagy .mp3 legyen"}
    try:
        t = clean(_read_doc(p))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"a dokumentum nem olvasható: {type(e).__name__}: {e}"[:200]}
    if not t:
        return {"ok": False, "reason": "nincs felolvasható szöveg (szkennelt PDF?)"}
    if len(t) > MAX_FILE:
        return {"ok": False, "reason": f"túl hosszú ({len(t)} karakter > {MAX_FILE}) – bontsd részekre"}
    key = "tts-" + re.sub(r"\W+", "_", p.stem)[:40]

    def on_part(i, n):
        if n > 3:
            progress(key, f"Felolvasás: {p.name}", 100 * i / n, f"{i}/{n} rész")

    res, err = _render(t, voice, rate, pitch, 1.0, on_part)
    if err:
        return err
    wav, name, parts = res
    try:
        _write(wav, target)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": str(e)[:300]}
    if parts > 3:
        progress(key, f"Felolvasás: {p.name}", 100, "kész", state="done", keep_s=60)
    return {"ok": True, "path": str(target), "voice": name, "chars": len(t), "parts": parts,
            "seconds": _duration(wav), "kb": round(target.stat().st_size / 1024)}
