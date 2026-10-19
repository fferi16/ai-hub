"""study – Tanulás: kártyák kinyerése jegyzetből, Anki-pakli (.apkg), gyakorló kvíz (egyfájlos HTML)

Kártyaformátum (JSON-lista): [{"front": "kérdés", "back": "válasz", "tags": ["1. fejezet"]}, ...]
A tanulójegyzetekből (Word) automatikusan kinyerhető:
- „Önellenőrző kérdések – N. fejezet” számozott kérdései + a „Válaszok…” fejezet „N. fejezet” alatti válaszai
- minden kétoszlopos táblázat, amelynek fejléce „Kérdés | Válasz”
"""
import hashlib
import html
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from hubkit import secret, tool  # noqa: F401

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "out" / "study"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _ptext(p) -> str:
    return "".join(t.text or "" for t in p.iter(f"{W}t")).strip()


def _heading(p) -> int:
    st = p.find(f"{W}pPr/{W}pStyle")
    m = re.match(r"(?:Heading|Cmsor)(\d)", st.get(f"{W}val", "")) if st is not None else None
    return int(m.group(1)) if m else 0


def _key(text: str) -> str:
    m = re.search(r"(\d+)\.\s*fejezet", text)
    return m.group(1) if m else text.strip()


def _cards_from_docx(path: Path) -> list[dict]:
    root = ET.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
    body = root.find(f"{W}body")
    chapter, mode, key, in_answers = "", None, None, False
    questions: dict[str, list[str]] = {}
    answers: dict[str, list[str]] = {}
    chapters: dict[str, str] = {}
    cards = []
    for el in body:
        if el.tag == f"{W}p":
            text, lvl = _ptext(el), _heading(el)
            if lvl:
                mode = None
                if lvl == 1:
                    chapter = text
                    in_answers = text.lower().startswith("válaszok")
                elif lvl == 2 and text.lower().startswith("önellenőrző kérdések"):
                    mode, key = "q", _key(text)
                    questions[key], chapters[key] = [], chapter
                elif lvl == 3 and in_answers:
                    mode, key = "a", _key(text)
                    answers[key] = []
                continue
            if text and mode == "q":
                questions[key].append(text)
            elif text and mode == "a":
                answers[key].append(text)
        elif el.tag == f"{W}tbl":
            rows = [[_ptext_cell(tc) for tc in tr.findall(f"{W}tc")] for tr in el.findall(f"{W}tr")]
            if rows and len(rows[0]) == 2 and rows[0][0].lower().startswith("kérdés") and rows[0][1].lower().startswith("válasz"):
                cards += [{"front": a, "back": b, "tags": ["Gyorsismétlő"]} for a, b in rows[1:] if a and b]
    for k, qs in questions.items():
        for i, q in enumerate(qs):
            a = answers.get(k, [])
            if i < len(a):
                cards.append({"front": q, "back": a[i], "tags": [chapters.get(k) or f"{k}. fejezet"]})
    return cards


def _ptext_cell(tc) -> str:
    return "\n".join(t for t in (_ptext(p) for p in tc.findall(f"{W}p")) if t)


def _load(cards: str, cards_path: str):
    try:
        data = json.loads(Path(cards_path).read_text("utf-8")) if cards_path else json.loads(cards or "[]")
    except (OSError, ValueError) as e:
        return None, {"ok": False, "reason": f"a kártyák nem olvashatók: {type(e).__name__}: {e}"[:200]}
    if isinstance(data, dict):
        data = data.get("cards", [])
    good = [c for c in data if isinstance(c, dict) and str(c.get("front", "")).strip() and str(c.get("back", "")).strip()]
    if not good:
        return None, {"ok": False, "reason": "nincs érvényes kártya (front + back kell)"}
    return good, None


@tool
def study_cards_from_docx(path: str, out: str = "") -> dict:
    """Kártyák kinyerése tanulójegyzetből (.docx): önellenőrző kérdések + válaszok, „Kérdés | Válasz” táblák → JSON."""
    p = Path(path).expanduser()
    if not p.is_file() or p.suffix.lower() != ".docx":
        return {"ok": False, "reason": f"nincs ilyen .docx: {path}"}
    cards = _cards_from_docx(p)
    if not cards:
        return {"ok": False, "reason": "nem találtam kérdés–válasz részt a dokumentumban"}
    target = Path(out) if out else OUT / f"{p.stem[:80]}_kartyak.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cards, ensure_ascii=False, indent=1), "utf-8")
    tags: dict[str, int] = {}
    for c in cards:
        for t in c["tags"]:
            tags[t] = tags.get(t, 0) + 1
    return {"ok": True, "cards": len(cards), "json": str(target), "by_tag": tags, "sample": cards[0]}


def _fmt(s: str) -> str:
    """Egyszerű jelölés → HTML: **félkövér**, sortörés."""
    s = html.escape(str(s))
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return s.replace("\n", "<br>")


@tool
def study_anki(deck: str, cards: str = "", cards_path: str = "", out: str = "") -> dict:
    """Anki-pakli (.apkg) készítése kártyákból (cards: JSON-szöveg vagy cards_path: JSON-fájl); Ankiban Fájl → Importálás."""
    import genanki
    data, err = _load(cards, cards_path)
    if err:
        return err
    if not deck.strip():
        return {"ok": False, "reason": "adj nevet a paklinak (deck)"}
    num = lambda s: int(hashlib.sha1(s.encode()).hexdigest()[:8], 16)  # noqa: E731 – stabil azonosító
    model = genanki.Model(num("hub-study-model-v1"), "Hub kártya", fields=[{"name": "Kérdés"}, {"name": "Válasz"}],
                          templates=[{"name": "Kártya", "qfmt": "<div class=q>{{Kérdés}}</div>",
                                      "afmt": "<div class=q>{{Kérdés}}</div><hr id=answer><div class=a>{{Válasz}}</div>"}],
                          css=".card{font-family:Segoe UI,Arial;font-size:20px;text-align:left;color:#222;background:#fff}"
                              ".q{font-weight:600}.a{margin-top:8px;line-height:1.45}")
    d = genanki.Deck(num("deck:" + deck), deck)
    for c in data:
        tags = [re.sub(r"\s+", "_", t.strip()) for t in c.get("tags", []) if str(t).strip()]
        d.add_note(genanki.Note(model=model, fields=[_fmt(c["front"]), _fmt(c["back"])], tags=tags,
                                guid=genanki.guid_for(deck, c["front"])))
    target = Path(out) if out else OUT / f"{re.sub(r'[^\w.-]+', '_', deck)[:80]}.apkg"
    target.parent.mkdir(parents=True, exist_ok=True)
    genanki.Package(d).write_to_file(str(target))
    return {"ok": True, "apkg": str(target), "cards": len(data), "kb": round(target.stat().st_size / 1024)}


@tool
def study_quiz(title: str, cards: str = "", cards_path: str = "", out: str = "") -> dict:
    """Gyakorló kvíz egyetlen HTML-fájlban (telefonon is): fordítható kártyák, keverés, fejezetszűrő, „tudtam/nem tudtam”."""
    data, err = _load(cards, cards_path)
    if err:
        return err
    page = (HERE / "quiz_template.html").read_text("utf-8")
    page = page.replace("__TITLE__", html.escape(title or "Gyakorló kvíz"))
    payload = json.dumps([{"f": _fmt(c["front"]), "b": _fmt(c["back"]), "t": [html.escape(str(t)) for t in c.get("tags", [])]}
                          for c in data],
                         ensure_ascii=False).replace("</", "<\\/")
    page = page.replace("__CARDS__", payload)
    target = Path(out) if out else OUT / f"{re.sub(r'[^\w.-]+', '_', title or 'kviz')[:80]}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, "utf-8")
    return {"ok": True, "html": str(target), "cards": len(data), "kb": round(target.stat().st_size / 1024)}
