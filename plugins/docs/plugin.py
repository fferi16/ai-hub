"""docs – Dokumentum → tömör Markdown (Microsoft MarkItDown, MIT)

Word, Excel, PowerPoint, PDF, Outlook (.msg), HTML, CSV, JSON, XML, EPUB, ZIP → Markdown fájlba.
A kimenet csak útvonal + rövid előnézet (tokentakarékos); a teljes .md-t Claude csak szükség esetén olvassa.
Gyorsítótár: ha a .md frissebb a forrásnál, nem konvertál újra (force=True kényszeríti).
A szkennelt (szöveg nélküli) PDF üres eredményt ad – ahhoz OCR kell.
"""
import fnmatch
import re
from pathlib import Path

from hubkit import secret, tool  # noqa: F401

OUT = Path(__file__).resolve().parents[2] / "out" / "docs"
EXT = {".docx", ".pptx", ".xlsx", ".xls", ".pdf", ".msg", ".html", ".htm", ".csv", ".json", ".xml", ".epub", ".zip", ".txt"}
_md = None


def _converter():
    global _md
    if _md is None:
        from markitdown import MarkItDown
        _md = MarkItDown(enable_plugins=False)
    return _md


def _norm(text: str) -> str:
    text = text.translate(str.maketrans({"­": "-", " ": " ", " ": " "}))
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def _convert(src: Path, target: Path, force: bool) -> dict:
    if target.is_file() and target.stat().st_mtime >= src.stat().st_mtime and not force:
        text, cached = target.read_text("utf-8"), True
    else:
        text, cached = _norm(_converter().convert(str(src)).text_content or ""), False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, "utf-8")
    return {"md": str(target), "chars": len(text), "tokens_est": round(len(text) / 3.5), "cached": cached,
            "src_kb": round(src.stat().st_size / 1024), "empty": not text.strip()}


@tool
def docs_to_md(path: str, out: str = "", preview_chars: int = 600, force: bool = False) -> dict:
    """Dokumentum (Word/Excel/PowerPoint/PDF/Outlook/HTML…) → Markdown fájlba; csak útvonal + előnézet, gyorsítótárral."""
    src = Path(path).expanduser()
    if not src.is_file():
        return {"ok": False, "reason": f"nincs ilyen fájl: {path}"}
    if src.suffix.lower() not in EXT:
        return {"ok": False, "reason": f"nem támogatott formátum: {src.suffix}", "supported": sorted(EXT)}
    target = Path(out).expanduser() if out else OUT / f"{src.stem[:100]}.md"
    try:
        res = _convert(src, target, force)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"konverziós hiba: {type(e).__name__}: {e}"[:300]}
    text = target.read_text("utf-8")
    res["preview"] = text[:max(0, min(int(preview_chars), 3000))]
    if res["empty"]:
        res["hint"] = "üres eredmény – szkennelt PDF? (OCR kell)"
    return {"ok": True, **res}


@tool
def docs_folder_to_md(folder: str, pattern: str = "*.docx;*.pdf;*.xlsx;*.pptx;*.msg", out_dir: str = "",
                      force: bool = False, max_files: int = 100) -> dict:
    """Mappa dokumentumai → Markdown (a minták ';'-vel); fájlonként útvonal, méret, ~token – előnézet nélkül."""
    d = Path(folder).expanduser()
    if not d.is_dir():
        return {"ok": False, "reason": f"nincs ilyen mappa: {folder}"}
    pats = [p.strip().lower() for p in pattern.split(";") if p.strip()]
    files = sorted(f for f in d.iterdir() if f.is_file() and f.suffix.lower() in EXT
                   and any(fnmatch.fnmatch(f.name.lower(), p) for p in pats))
    outd = Path(out_dir).expanduser() if out_dir else OUT / d.name
    done, failed = [], []
    for f in files[:max(1, min(int(max_files), 500))]:
        try:
            r = _convert(f, outd / f"{f.stem[:100]}.md", force)
            done.append({"file": f.name, "md": r["md"], "chars": r["chars"], "cached": r["cached"], "empty": r["empty"]})
        except Exception as e:  # noqa: BLE001
            failed.append({"file": f.name, "error": f"{type(e).__name__}: {e}"[:150]})
    return {"ok": True, "converted": len(done), "failed": failed, "skipped_over_limit": max(0, len(files) - len(done) - len(failed)),
            "tokens_est": round(sum(x["chars"] for x in done) / 3.5), "out_dir": str(outd), "files": done}
