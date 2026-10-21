"""index – kereshető teljes szöveges index a fájlokból (SQLite FTS5, ékezet-független).

index_add(mappa) → szövegfájlok és dokumentumok (Word/Excel/PowerPoint/PDF/Outlook, MarkItDown-nal) indexelése.
Növekményes: csak az új/változott fájlokat olvassa újra, a törölteket kiveszi. Időkeret (budget_s) után megáll,
és "incomplete": true-t ad – ilyenkor ugyanaz a hívás folytatja.
index_search(kérdés) → találatok útvonallal és rövid szövegrészlettel (a fájlt Claude csak szükség esetén nyitja meg).
Adatbázis: out/index/index.db.
"""
import fnmatch
import os
import re
import sqlite3
import time
from pathlib import Path

from hubkit import progress, tool

DB = Path(__file__).resolve().parents[2] / "out" / "index" / "index.db"
TEXT_EXT = {".txt", ".md", ".py", ".json", ".csv", ".tsv", ".html", ".htm", ".xml", ".ps1", ".js", ".ts", ".css",
            ".ini", ".cfg", ".conf", ".log", ".yaml", ".yml", ".toml", ".sql", ".bat", ".cmd", ".srt", ".vtt", ".rst"}
DOC_EXT = {".docx", ".pptx", ".xlsx", ".xls", ".pdf", ".msg", ".epub"}
SKIP_DIRS = {"node_modules", "__pycache__", "site-packages", "$recycle.bin", "system volume information"}
TEXT_MAX = 5 * 2**20     # szövegfájl felső mérethatára
DOC_MAX = 50 * 2**20     # dokumentum felső mérethatára
BODY_MAX = 2 * 2**20     # fájlonként legfeljebb ennyi karakter kerül az indexbe
_md = None


def _db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS roots(path TEXT PRIMARY KEY, pattern TEXT, updated REAL);
        CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, root TEXT, path TEXT UNIQUE, mtime REAL, size INTEGER,
                                         chars INTEGER, err TEXT, orig TEXT);
        CREATE INDEX IF NOT EXISTS files_root ON files(root);
        CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name, body, tokenize='unicode61 remove_diacritics 2');
    """)
    return con


def _norm_root(folder: str) -> str:
    return os.path.normcase(str(Path(folder).expanduser().resolve()))


def _converter():
    global _md
    if _md is None:
        from markitdown import MarkItDown
        _md = MarkItDown(enable_plugins=False)
    return _md


def _read_text(f: Path) -> str:
    raw = f.read_bytes()[:BODY_MAX * 2]
    for enc in ("utf-8-sig", "cp1250"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _extract(f: Path) -> str:
    text = _read_text(f) if f.suffix.lower() in TEXT_EXT else (_converter().convert(str(f)).text_content or "")
    return re.sub(r"\s+\n", "\n", text.replace("\xad", "").replace("\xa0", " "))[:BODY_MAX]


def _walk(root: Path, pats: list[str]):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in SKIP_DIRS]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in TEXT_EXT and ext not in DOC_EXT or fn.startswith("~$"):
                continue
            if pats and not any(fnmatch.fnmatch(fn.lower(), p) for p in pats):
                continue
            yield Path(dirpath) / fn


def _sync(con: sqlite3.Connection, root: str, pattern: str, budget_s: float, max_files: int) -> dict:
    pats = [p.strip().lower() for p in pattern.split(";") if p.strip()]
    t0 = time.time()
    known = {p: (m, s) for p, m, s in con.execute("SELECT path, mtime, size FROM files WHERE root=?", (root,))}
    seen, todo, too_big = set(), [], 0
    for f in _walk(Path(root).resolve(), pats):
        try:
            st = f.stat()
        except OSError:
            continue
        key = os.path.normcase(str(f))
        if len(seen) >= max_files:
            break
        seen.add(key)
        if st.st_size > (TEXT_MAX if f.suffix.lower() in TEXT_EXT else DOC_MAX):
            too_big += 1
            continue
        if known.get(key) != (st.st_mtime, st.st_size):
            todo.append((f, key, st))
    removed = [p for p in known if p not in seen]
    for p in removed:
        (fid,) = con.execute("SELECT id FROM files WHERE path=?", (p,)).fetchone()
        con.execute("DELETE FROM fts WHERE rowid=?", (fid,))
        con.execute("DELETE FROM files WHERE id=?", (fid,))
    added = errors = 0
    pkey = "index-" + re.sub(r"\W+", "_", root)[-40:]
    for i, (f, key, st) in enumerate(todo):
        if time.time() - t0 > budget_s:
            break
        if i % 20 == 0 and len(todo) > 50:
            progress(pkey, f"Indexelés: {Path(root).name}", 100 * i / len(todo), f"{i}/{len(todo)} fájl")
        try:
            body, err = _extract(f), None
        except Exception as e:  # noqa: BLE001
            body, err = "", f"{type(e).__name__}: {e}"[:150]
            errors += 1
        row = con.execute("SELECT id FROM files WHERE path=?", (key,)).fetchone()
        if row:
            con.execute("DELETE FROM fts WHERE rowid=?", (row[0],))
            con.execute("UPDATE files SET mtime=?, size=?, chars=?, err=?, orig=? WHERE id=?",
                        (st.st_mtime, st.st_size, len(body), err, str(f), row[0]))
            fid = row[0]
        else:
            fid = con.execute("INSERT INTO files(root, path, mtime, size, chars, err, orig) VALUES(?,?,?,?,?,?,?)",
                              (root, key, st.st_mtime, st.st_size, len(body), err, str(f))).lastrowid
        con.execute("INSERT INTO fts(rowid, name, body) VALUES(?,?,?)", (fid, f.name, body))
        added += 1
        if added % 50 == 0:
            con.commit()
    if len(todo) > 50:
        progress(pkey, f"Indexelés: {Path(root).name}", 100, f"{added} fájl", state="done", keep_s=30)
    con.execute("INSERT OR REPLACE INTO roots(path, pattern, updated) VALUES(?,?,?)", (root, pattern, time.time()))
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM files WHERE root=?", (root,)).fetchone()[0]
    out = {"root": root, "files": total, "updated": added, "removed": len(removed), "errors": errors,
           "s": round(time.time() - t0, 1)}
    if too_big:
        out["too_big"] = too_big
    if len(seen) >= max_files:
        out["capped_at"] = max_files
    if added < len(todo):
        out.update(incomplete=True, left=len(todo) - added, hint="hívd újra ugyanígy a folytatáshoz")
    return out


@tool
def index_add(folder: str, pattern: str = "", budget_s: int = 90, max_files: int = 20000) -> dict:
    """Mappa (rekurzív) indexelése / frissítése; szöveg + Word/Excel/PPT/PDF/Outlook. Növekményes, időkerettel."""
    d = Path(folder).expanduser()
    if not d.is_dir():
        return {"ok": False, "reason": f"nincs ilyen mappa: {folder}"}
    root = _norm_root(folder)
    con = _db()
    try:
        inner = [r for (r,) in con.execute("SELECT path FROM roots") if r != root and root.startswith(r + os.sep)]
        if inner:
            return {"ok": False, "reason": f"már indexelve egy szülőmappával: {inner[0]}", "hint": "index_refresh azt"}
        return {"ok": True, **_sync(con, root, pattern, max(5, min(int(budget_s), 110)), max(1, int(max_files)))}
    finally:
        con.close()


@tool
def index_refresh(folder: str = "", budget_s: int = 90) -> dict:
    """Az indexelt mappák (vagy egy megadott) frissítése: új/változott fájlok be, törölt fájlok ki."""
    con = _db()
    try:
        roots = con.execute("SELECT path, pattern FROM roots").fetchall()
        if folder:
            want = _norm_root(folder)
            roots = [r for r in roots if r[0] == want]
            if not roots:
                return {"ok": False, "reason": f"nincs indexelve: {folder}"}
        res, t0 = [], time.time()
        for path, pattern in roots:
            left = budget_s - (time.time() - t0)
            if left < 3:
                res.append({"root": path, "incomplete": True, "hint": "elfogyott az időkeret – hívd újra"})
                continue
            if not Path(path).is_dir():
                res.append({"root": path, "missing": True})
                continue
            res.append(_sync(con, path, pattern or "", left, 20000))
        return {"ok": True, "roots": res}
    finally:
        con.close()


def _fts_query(q: str, mode: str) -> str:
    words = re.findall(r"\w+", q, flags=re.UNICODE)
    return f" {'OR' if mode == 'any' else 'AND'} ".join(f'"{w}"*' for w in words)


@tool
def index_search(query: str, folder: str = "", ext: str = "", limit: int = 10, mode: str = "all") -> dict:
    """Keresés az indexben (ékezet-független, szóeleji egyezés): útvonal + rövid részlet; mode=any: bármely szó."""
    q = _fts_query(query, mode)
    if not q:
        return {"ok": False, "reason": "üres keresés"}
    sql = ("SELECT COALESCE(f.orig, f.path), snippet(fts, 1, '«', '»', '…', 14), bm25(fts, 5.0, 1.0) AS r FROM fts "
           "JOIN files f ON f.id = fts.rowid WHERE fts MATCH ?")
    args: list = [q]
    if folder:
        sql += " AND (f.path = ? OR f.path LIKE ? ESCAPE '\\')"
        base = _norm_root(folder)
        like = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args += [base, like + "\\\\%"]
    exts = [e.strip().lower().lstrip("*") for e in ext.split(";") if e.strip()]
    if exts:
        sql += " AND (" + " OR ".join("f.path LIKE ?" for _ in exts) + ")"
        args += ["%" + (e if e.startswith(".") else "." + e) for e in exts]
    lim = max(1, min(int(limit), 50))
    con = _db()
    try:
        rows = con.execute(sql + " ORDER BY r LIMIT ?", (*args, lim + 1)).fetchall()
        if not con.execute("SELECT 1 FROM files LIMIT 1").fetchone():
            return {"ok": False, "reason": "üres index", "hint": "előbb index_add(mappa)"}
    finally:
        con.close()
    hits = [{"p": p, "s": re.sub(r"\s+", " ", s)[:220]} for p, s, _ in rows[:lim]]
    return {"ok": True, "n": len(hits), "more": len(rows) > lim, "hits": hits}


@tool
def index_list() -> dict:
    """Indexelt mappák: útvonal, fájlszám, hibás fájlok, utolsó frissítés; az adatbázis mérete."""
    con = _db()
    try:
        rows = []
        for path, pattern, upd in con.execute("SELECT path, pattern, updated FROM roots ORDER BY path"):
            n, err = con.execute("SELECT COUNT(*), SUM(err IS NOT NULL) FROM files WHERE root=?", (path,)).fetchone()
            row = {"root": path, "files": n, "upd": time.strftime("%Y-%m-%d %H:%M", time.localtime(upd))}
            if err:
                row["errors"] = err
            if pattern:
                row["pattern"] = pattern
            rows.append(row)
    finally:
        con.close()
    return {"ok": True, "roots": rows, "db_mb": round(DB.stat().st_size / 2**20, 1) if DB.exists() else 0}


@tool
def index_remove(folder: str) -> dict:
    """Mappa kivétele az indexből (a fájlokat nem érinti)."""
    root = _norm_root(folder)
    con = _db()
    try:
        if not con.execute("SELECT 1 FROM roots WHERE path=?", (root,)).fetchone():
            return {"ok": False, "reason": f"nincs indexelve: {folder}"}
        con.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM files WHERE root=?)", (root,))
        n = con.execute("DELETE FROM files WHERE root=?", (root,)).rowcount
        con.execute("DELETE FROM roots WHERE path=?", (root,))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "root": root, "removed_files": n}
