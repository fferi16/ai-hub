"""Office → PDF külön folyamatban: python convert.py <job.json>

A job: {"src", "pdf", "docx_out"?, "update_toc"?, "pids_file"}.
Saját (DispatchEx) Office-példányt indít láthatatlanul, a forrás másolatát csak olvasásra nyitja
(így nincs „használatban van” kérdés), a riasztásokat kikapcsolja, a végén kilép.
Az új Office-folyamatok azonosítóit a pids_file-ba írja, hogy elakadáskor a szülő leállíthassa őket.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pythoncom
import win32com.client

APPS = {".docx": ("Word.Application", "WINWORD.EXE"), ".doc": ("Word.Application", "WINWORD.EXE"),
        ".rtf": ("Word.Application", "WINWORD.EXE"), ".odt": ("Word.Application", "WINWORD.EXE"),
        ".xlsx": ("Excel.Application", "EXCEL.EXE"), ".xlsm": ("Excel.Application", "EXCEL.EXE"),
        ".xls": ("Excel.Application", "EXCEL.EXE"), ".csv": ("Excel.Application", "EXCEL.EXE"),
        ".pptx": ("PowerPoint.Application", "POWERPNT.EXE"), ".ppt": ("PowerPoint.Application", "POWERPNT.EXE")}


def pids(image: str) -> set[int]:
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH", "/FI", f"IMAGENAME eq {image}"],
                         capture_output=True, text=True, creationflags=0x08000000).stdout
    return {int(l.split('","')[1]) for l in out.splitlines() if l.startswith('"') and '","' in l}


def main(job: dict) -> dict:
    src = Path(job["src"])
    progid, image = APPS[src.suffix.lower()]
    before = pids(image)
    pythoncom.CoInitialize()
    app = win32com.client.DispatchEx(progid)
    Path(job["pids_file"]).write_text(json.dumps(sorted(pids(image) - before)), "utf-8")
    with tempfile.TemporaryDirectory() as td:
        copy = Path(td) / ("forras" + src.suffix)
        shutil.copy2(src, copy)
        pdf = str(Path(job["pdf"]).resolve())
        try:
            if image == "WINWORD.EXE":
                app.Visible = False
                app.DisplayAlerts = 0
                app.Options.UpdateLinksAtOpen = False
                d = app.Documents.Open(str(copy), ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False,
                                       Visible=False, OpenAndRepair=False, NoEncodingDialog=True)
                if job.get("update_toc"):
                    for t in d.TablesOfContents:
                        t.Update()
                pages = d.ComputeStatistics(2)
                if job.get("docx_out"):
                    d.SaveAs2(str(Path(job["docx_out"]).resolve()), 16)
                d.ExportAsFixedFormat(pdf, 17, OpenAfterExport=False, OptimizeFor=0, CreateBookmarks=1)
                d.Close(False)
            elif image == "EXCEL.EXE":
                app.Visible = False
                app.DisplayAlerts = False
                app.AskToUpdateLinks = False
                wb = app.Workbooks.Open(str(copy), UpdateLinks=0, ReadOnly=True, AddToMru=False)
                wb.ExportAsFixedFormat(0, pdf)
                pages = None
                wb.Close(False)
            else:
                app.DisplayAlerts = 1  # ppAlertsNone
                pr = app.Presentations.Open(str(copy), ReadOnly=True, Untitled=False, WithWindow=False)
                pages = pr.Slides.Count
                pr.SaveAs(pdf, 32)
                pr.Close()
        finally:
            app.Quit()
    return {"ok": True, "pdf": pdf, "pages": pages, "docx": job.get("docx_out") or None}


if __name__ == "__main__":
    job = json.loads(Path(sys.argv[1]).read_text("utf-8"))
    try:
        res = main(job)
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "reason": f"{type(e).__name__}: {e}"[:400]}
    Path(job["result_file"]).write_text(json.dumps(res, ensure_ascii=False), "utf-8")
