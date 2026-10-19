"""Háttér-összefoglaló: python summarize.py <job-mappa>  (spec.json → status.json, eredmény .md)

A szöveget sorhatáron ~chunk_chars méretű szakaszokra bontja, mindegyiket a helyi modellel
pontokba szedi (számok, definíciók, felsorolások megtartásával), végül egy áttekintőt ír.
Ha a sorok [óó:pp:mm] időbélyeggel kezdődnek (media-leirat), a szakaszok időtartományát is jelzi.
"""
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))  # C:\HUB → hubkit
from local_ollama import chat  # noqa: E402

try:
    from hubkit import progress
except Exception:  # noqa: BLE001
    progress = None

JDIR = Path(sys.argv[1])
SPEC = json.loads((JDIR / "spec.json").read_text("utf-8"))
REPORT = progress is not None and "_test" not in SPEC["out"]
TITLE = "Összefoglaló: " + Path(SPEC["src"]).stem

SYSTEM = ("Magyar nyelvű jegyzetkészítő asszisztens vagy. Csak a megadott szövegből dolgozz, ne találj ki semmit. "
          "A félrehallott, beszélt nyelvi leiratot értelmezd, de a szakkifejezéseket, számokat, definíciókat, "
          "felsorolásokat pontosan tartsd meg.")
PART = ("{instr}\n\nFoglald össze a következő szakaszt tömör magyar pontokban (Markdown felsorolás, legfeljebb {n} pont). "
        "Ha a szakasz csak szervezési csevegés, írd: „(nincs szakmai tartalom)”.\n\n--- SZAKASZ ---\n{text}")
FINAL = ("{instr}\n\nAz alábbiak egy hosszabb anyag szakaszonkénti összefoglalói. Írj belőlük áttekintést magyarul: "
         "1) egy bekezdés arról, miről szól az anyag; 2) a fő témák listája; 3) a legfontosabb tények/számok/definíciók.\n\n{text}")


def status(**kw):
    tmp = JDIR / "status.tmp"
    tmp.write_text(json.dumps(kw, ensure_ascii=False), "utf-8")
    os.replace(tmp, JDIR / "status.json")
    if REPORT:
        st = kw.get("state")
        try:
            progress(f"local-{SPEC['job']}", TITLE, float(kw.get("progress") or 0) * 100, kw.get("step", ""),
                     "error" if st == "error" else "done" if st == "done" else "running")
        except Exception:  # noqa: BLE001
            pass


def read_text(p: Path) -> str:
    if p.suffix.lower() == ".docx":
        import zipfile
        xml = zipfile.ZipFile(p).read("word/document.xml").decode("utf-8")
        return "\n".join("".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", para))
                         for para in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S))
    if p.suffix.lower() == ".pdf":
        import pdfplumber  # MIT – a pdf plugin is ezt használja
        with pdfplumber.open(p) as pdf:
            text = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
        return text.replace("­", "-").replace(" ", " ")
    return p.read_text("utf-8", errors="replace")


def chunks(text: str, size: int):
    buf, n = [], 0
    for line in text.splitlines():
        if n + len(line) > size and buf:
            yield "\n".join(buf)
            buf, n = [], 0
        buf.append(line)
        n += len(line) + 1
    if buf:
        yield "\n".join(buf)


def span(part: str) -> str:
    ts = re.findall(r"^\[(\d\d:\d\d:\d\d)\]", part, flags=re.M)
    return f" ({ts[0]} – {ts[-1]})" if ts else ""


def main():
    p = SPEC["params"]
    src, out = Path(SPEC["src"]), Path(SPEC["out"])
    status(state="running", step="szöveg beolvasása", progress=0)
    parts = [c for c in chunks(read_text(src), int(p["chunk_chars"])) if c.strip()]
    instr = p.get("instructions") or ""
    t0, sums = time.time(), []
    for i, part in enumerate(parts, 1):
        status(state="running", step=f"{i}/{len(parts)}. szakasz", progress=round((i - 1) / (len(parts) + 1), 3),
               elapsed_s=int(time.time() - t0))
        r = chat(PART.format(instr=instr, n=p["points"], text=part), p["model"], SYSTEM, max_tokens=700, timeout=300)
        sums.append((f"{i}. szakasz{span(part)}", r["text"]))
    overview = ""
    if len(sums) > 1:
        status(state="running", step="áttekintés", progress=round(len(parts) / (len(parts) + 1), 3),
               elapsed_s=int(time.time() - t0))
        joined = "\n\n".join(f"## {h}\n{s}" for h, s in sums)[:24000]
        overview = chat(FINAL.format(instr=instr, text=joined), p["model"], SYSTEM, max_tokens=1200,
                        timeout=300, num_ctx=12288)["text"]
    out.parent.mkdir(parents=True, exist_ok=True)
    md = [f"# Összefoglaló – {src.name}", f"_Helyi modell: {p['model']} · {len(parts)} szakasz · "
          f"{int(time.time() - t0)} mp_", ""]
    if overview:
        md += ["## Áttekintés", overview, ""]
    md += [f"## {h}\n{s}\n" for h, s in sums]
    out.write_text("\n".join(md), "utf-8")
    status(state="done", progress=1, step="kész", md=str(out), parts=len(parts), chars=out.stat().st_size,
           elapsed_s=int(time.time() - t0))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        status(state="error", step="hiba", error=f"{type(e).__name__}: {e}"[:300])
