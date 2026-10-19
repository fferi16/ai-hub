"""pdf – PDF-ek: szöveg, sorok/szavak koordinátával, táblázatok, regex-keresés, oldal képként

Motor: pdfplumber (MIT, pdfminer.six) a szöveghez/táblázatokhoz, pypdfium2 (Apache-2.0/BSD) a képpé alakításhoz –
megengedő licencűek (a PyMuPDF AGPL-licence miatt cseréltük le).
Oldalszámok mindig 1-től; a pages paraméter pl. "1", "2-4", "1,3,7-9", üres = mind.
Koordináták pontban (1 pt = 1/72 hüvelyk), bal felső sarok az origó; egy tizedesre kerekítve.
"""
import re
from pathlib import Path

from hubkit import secret, tool  # noqa: F401

OUT = Path(__file__).resolve().parents[2] / "out" / "pdf"
_NORM = str.maketrans({"­": "-", " ": " ", " ": " ", " ": " ", " ": " "})


def _c(s: str) -> str:
    """Normalizálás: lágy kötőjel → kötőjel (pl. „4-16-4” rétegrend), nem törő / keskeny szóközök → szóköz
    (pl. „14 900 Ft”), hogy a keresés és a regexek megbízhatóak legyenek."""
    return (s or "").translate(_NORM)


r1 = lambda v: round(float(v), 1)  # noqa: E731


def _open(path: str):
    import pdfplumber
    p = Path(path).expanduser()
    if not p.is_file():
        return None, {"ok": False, "reason": f"nincs ilyen fájl: {path}"}
    try:
        pdf = pdfplumber.open(p)
        len(pdf.pages)
        return pdf, None
    except Exception as e:  # noqa: BLE001
        return None, {"ok": False, "reason": f"nem nyitható PDF: {type(e).__name__}: {e}"[:200]}


def _pages(spec: str, n: int):
    if not spec.strip():
        return list(range(n)), None
    out = []
    for part in spec.replace(" ", "").split(","):
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            return None, {"ok": False, "reason": f"hibás oldalmegadás: {part}"}
        a, b = int(m[1]), int(m[2] or m[1])
        if not (1 <= a <= b <= n):
            return None, {"ok": False, "reason": f"oldal a tartományon kívül: {part} (1–{n})"}
        out.extend(range(a - 1, b))
    return list(dict.fromkeys(out)), None


def _page(pdf, page: int):
    if not 1 <= page <= len(pdf.pages):
        return None, {"ok": False, "reason": f"nincs ilyen oldal: {page} (1–{len(pdf.pages)})"}
    return pdf.pages[page - 1], None


def _bbox(bbox: str, page=None):
    if not bbox:
        return None, None
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox.split(","))
    except ValueError:
        return None, {"ok": False, "reason": "bbox formátuma: x0,y0,x1,y1"}
    if page is not None:  # az oldalra vágjuk, különben a pdfplumber hibát dob
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(float(page.width), x1), min(float(page.height), y1)
    if x1 <= x0 or y1 <= y0:
        return None, {"ok": False, "reason": "üres bbox"}
    return (x0, y0, x1, y1), None


@tool
def pdf_info(path: str) -> dict:
    """PDF adatai: oldalszám, méret, metaadatok, és mely oldalakon nincs szöveg (szkennelt → OCR kell)."""
    pdf, err = _open(path)
    if err:
        return err
    with pdf:
        no_text = [i + 1 for i, pg in enumerate(pdf.pages) if not (pg.extract_text() or "").strip()]
        first = pdf.pages[0] if pdf.pages else None
        meta = {k.lower(): _c(str(v)) for k, v in (pdf.metadata or {}).items()
                if v and k in ("Title", "Author", "Subject", "Creator", "Producer", "CreationDate", "ModDate")}
        return {"ok": True, "pages": len(pdf.pages), "size_pt": [r1(first.width), r1(first.height)] if first else None,
                "kb": round(Path(path).stat().st_size / 1024), "meta": meta, "pages_without_text": no_text[:50]}


@tool
def pdf_text(path: str, pages: str = "", layout: bool = False, max_chars: int = 20000) -> dict:
    """Oldalak szövege oldalankénti elválasztóval; nagy PDF-nél előbb pdf_info, majd pages-szűrő vagy pdf_search."""
    pdf, err = _open(path)
    if err:
        return err
    with pdf:
        idx, err = _pages(pages, len(pdf.pages))
        if err:
            return err
        parts = [f"--- {i + 1}. oldal ---\n" + _c(pdf.pages[i].extract_text(layout=layout)).strip() for i in idx]
    text = "\n".join(parts)
    return {"ok": True, "pages": [i + 1 for i in idx], "chars": len(text), "truncated": len(text) > max_chars,
            "text": text[:max_chars]}


@tool
def pdf_words(path: str, page: int = 1, bbox: str = "", lines: bool = True, max_items: int = 1500) -> dict:
    """Egy oldal sorai (lines=True) vagy szavai koordinátával [x0,y0,x1,y1]; bbox='x0,y0,x1,y1' szűkít."""
    pdf, err = _open(path)
    if err:
        return err
    with pdf:
        pg, err = _page(pdf, page)
        if err:
            return err
        box, err = _bbox(bbox, pg)
        if err:
            return err
        area = pg.within_bbox(box) if box else pg
        raw = area.extract_text_lines() if lines else area.extract_words()
        items = [{"text": _c(it["text"]), "bbox": [r1(it["x0"]), r1(it["top"]), r1(it["x1"]), r1(it["bottom"])]} for it in raw]
        items.sort(key=lambda it: (it["bbox"][1], it["bbox"][0]))
        return {"ok": True, "page": page, "size_pt": [r1(pg.width), r1(pg.height)], "count": len(items),
                "truncated": len(items) > max_items, "items": items[:max_items]}


@tool
def pdf_tables(path: str, pages: str = "1", max_rows: int = 300) -> dict:
    """Táblázatok felismerése az oldalakon (vonalas táblák): soronként a cellák szövege."""
    pdf, err = _open(path)
    if err:
        return err
    with pdf:
        idx, err = _pages(pages, len(pdf.pages))
        if err:
            return err
        tables = []
        for i in idx:
            for t in pdf.pages[i].find_tables():
                rows = [[_c(" ".join((c or "").split())) for c in row] for row in t.extract()]
                tables.append({"page": i + 1, "bbox": [r1(v) for v in t.bbox], "rows_n": len(rows),
                               "cols": max((len(r) for r in rows), default=0), "rows": rows[:max_rows]})
    return {"ok": True, "count": len(tables), "tables": tables}


@tool
def pdf_search(path: str, pattern: str, pages: str = "", ignore_case: bool = True, max_hits: int = 200) -> dict:
    """Regex-keresés soronként: oldal, a teljes sor szövege, a találat (csoportokkal) és a sor koordinátái."""
    try:
        rx = re.compile(pattern, re.I if ignore_case else 0)
    except re.error as e:
        return {"ok": False, "reason": f"hibás regex: {e}"}
    pdf, err = _open(path)
    if err:
        return err
    with pdf:
        idx, err = _pages(pages, len(pdf.pages))
        if err:
            return err
        hits = []
        for i in idx:
            for ln in pdf.pages[i].extract_text_lines():
                text = _c(ln["text"])
                for m in rx.finditer(text):
                    hits.append({"page": i + 1, "match": m.group(0), "groups": list(m.groups()), "line": text.strip(),
                                 "bbox": [r1(ln["x0"]), r1(ln["top"]), r1(ln["x1"]), r1(ln["bottom"])]})
                    if len(hits) >= max_hits:
                        return {"ok": True, "count": len(hits), "truncated": True, "hits": hits}
    return {"ok": True, "count": len(hits), "truncated": False, "hits": hits}


@tool
def pdf_render(path: str, page: int = 1, dpi: int = 110, bbox: str = "", out: str = "") -> dict:
    """Oldal (vagy bbox-kivágás) PNG-képként az out\\pdf mappába; csak az útvonalat adja vissza."""
    import pypdfium2 as pdfium
    p = Path(path).expanduser()
    if not p.is_file():
        return {"ok": False, "reason": f"nincs ilyen fájl: {path}"}
    try:
        doc = pdfium.PdfDocument(str(p))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"nem nyitható PDF: {type(e).__name__}: {e}"[:200]}
    try:
        if not 1 <= page <= len(doc):
            return {"ok": False, "reason": f"nincs ilyen oldal: {page} (1–{len(doc)})"}
        pg = doc[page - 1]
        w, h = pg.get_size()
        box, err = _bbox(bbox)
        if err:
            return err
        crop = (box[0], h - box[3], w - box[2], box[1]) if box else (0, 0, 0, 0)  # bal, alsó, jobb, felső levágás
        dpi = max(30, min(int(dpi), 300))
        img = pg.render(scale=dpi / 72, crop=crop).to_pil()
        target = Path(out) if out else OUT / f"{p.stem[:60]}_p{page}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        img.save(target)
        return {"ok": True, "png": str(target), "px": [img.width, img.height], "kb": round(target.stat().st_size / 1024)}
    finally:
        doc.close()
