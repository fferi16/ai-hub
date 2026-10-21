"""dxf – DXF rajzok olvasása, írása, renderelése (ezdxf, MIT) és megnyitása a saját CAD-programban (Alnico).

Olvasás tokentakarékosan: előbb dxf_info (áttekintés), utána célzottan dxf_layers / dxf_texts / dxf_entities /
dxf_bom (darabjegyzék) / dxf_attribs (attribútum-táblázat, CSV).
Írás egyszerű JSON-elemlistából: dxf_write (új rajz), dxf_add (meglévőhöz, ÚJ fájlba – az eredetit sosem írja felül).
Kép: dxf_render → PNG (a képet Claude csak szükség esetén nézi meg; előtte local_see is leírhatja).
Az Alnicóból átvett szabályok: az eredeti rajz érintetlen; a névtelen (*D…) blokkok nem darabok; gyártói
blokknév = cikkszám, a _2/_13 végződés változat; CSV pontosvesszővel + UTF-8 BOM (magyar Excel); egység → mm.

Elemformátum (dxf_write / dxf_add), koordináták a rajz egységében:
  {"t":"line","p":[[x,y],[x,y]]}  {"t":"poly","p":[[x,y],...],"closed":true}  {"t":"rect","p":[x,y],"w":..,"h":..}
  {"t":"circle","c":[x,y],"r":..}  {"t":"arc","c":[x,y],"r":..,"a":[kezdő°,vég°]}
  {"t":"text","p":[x,y],"text":"..","h":2.5,"rot":0}  {"t":"dim","p":[[x,y],[x,y]],"dist":10}
  {"t":"insert","block":"név","p":[x,y],"scale":1,"rot":0}
  bármelyikhez: "layer":"név"; a rétegek színe: layers={"név": ACI-szín}
"""
import math
import os
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from hubkit import tool

OUT = Path(__file__).resolve().parents[2] / "out" / "dxf"
ALNICO = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Alnico" / "Alnico.exe"
UNITS = {0: "egység nélkül", 1: "inch", 2: "láb", 4: "mm", 5: "cm", 6: "m"}
MM_PER_UNIT = {1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0}  # mint az Alnico cadfile.js-e; ismeretlen → 1
CIKK = re.compile(r"^(\d{4,9})(?:[_-]\d+)*$")  # gyártói blokknév = cikkszám (Alnico cadblokk.js)


def _r(v, n: int = 3) -> float:
    return round(float(v), n)


def _mm(doc) -> float:
    return MM_PER_UNIT.get(doc.header.get("$INSUNITS", 0), 1.0)


def _units(doc) -> str:
    return UNITS.get(doc.header.get("$INSUNITS", 0), "egyéb")


def _load(path: str):
    import ezdxf
    from ezdxf import recover

    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"nincs ilyen fájl: {path}")
    if p.suffix.lower() != ".dxf":
        raise ValueError(f"nem DXF fájl: {p.suffix}")
    try:
        return ezdxf.readfile(p), []
    except ezdxf.DXFStructureError:
        doc, auditor = recover.readfile(p)  # sérült fájl: helyreállítva olvassuk
        return doc, [f"helyreállítva: {len(auditor.fixes)} javítás, {len(auditor.errors)} hiba"]


def _fail(e: Exception) -> dict:
    if isinstance(e, (FileNotFoundError, ValueError)):
        return {"ok": False, "reason": str(e)}
    return {"ok": False, "reason": f"{type(e).__name__}: {e}"[:300]}


def _length(e) -> float:
    """Egy elem hossza a rajz egységében (vonal, ív, kör, polivonal, spline, ellipszis)."""
    t = e.dxftype()
    try:
        if t == "LINE":
            return e.dxf.start.distance(e.dxf.end)
        if t == "ARC":
            span = (e.dxf.end_angle - e.dxf.start_angle) % 360 or 360
            return math.radians(span) * e.dxf.radius
        if t == "CIRCLE":
            return 2 * math.pi * e.dxf.radius
        if t in ("LWPOLYLINE", "POLYLINE", "SPLINE", "ELLIPSE"):
            from ezdxf import path as zpath
            pts = list(zpath.make_path(e).flattening(0.01))
            return sum(a.distance(b) for a, b in zip(pts, pts[1:]))
    except Exception:  # noqa: BLE001
        return 0.0
    return 0.0


def _lengths(doc) -> defaultdict:
    """Vonalhossz rétegenként, a blokkokba ágyazott elemekkel együtt (kilapítva, mint az Alnicóban).
    A blokkon belüli '0' rétegű elem a beszúrás rétegét örökli."""
    total = defaultdict(float)

    def walk(entities, inherit: str | None, depth: int) -> None:
        for e in entities:
            layer = e.dxf.layer
            if inherit and layer == "0":
                layer = inherit
            if e.dxftype() == "INSERT":
                if depth < 10 and not e.dxf.name.startswith("*"):
                    try:
                        walk(e.virtual_entities(), layer, depth + 1)
                    except Exception:  # noqa: BLE001
                        pass
                continue
            total[layer] += _length(e)

    walk(doc.modelspace(), None, 0)
    return total


def _dim_text(e) -> str:
    t = e.dxf.get("text", "") or ""
    return "" if t == "<>" else t  # '<>' = a mért érték


def _text(e) -> str:
    t = e.dxftype()
    if t == "MTEXT":
        return e.plain_text().strip()
    if t in ("TEXT", "ATTRIB", "ATTDEF"):
        return (e.dxf.text or "").strip()
    return ""


def _brief(e) -> list:
    """Egy elem tömör leírása: [típus, réteg, geometria…]."""
    t, d = e.dxftype(), e.dxf
    row = [t, d.layer]
    try:
        if t == "LINE":
            row += [[_r(d.start.x), _r(d.start.y)], [_r(d.end.x), _r(d.end.y)]]
        elif t == "CIRCLE":
            row += [[_r(d.center.x), _r(d.center.y)], _r(d.radius)]
        elif t == "ARC":
            row += [[_r(d.center.x), _r(d.center.y)], _r(d.radius), _r(d.start_angle, 1), _r(d.end_angle, 1)]
        elif t == "LWPOLYLINE":
            pts = [[_r(x), _r(y)] for x, y in e.get_points("xy")]
            row += [pts[:30] + (["…"] if len(pts) > 30 else []), "zárt" if e.closed else "nyitott"]
        elif t in ("TEXT", "MTEXT"):
            row += [[_r(d.insert.x), _r(d.insert.y)], _text(e)[:80]]
        elif t == "INSERT":
            row += [d.name, [_r(d.insert.x), _r(d.insert.y)], _r(d.xscale), _r(d.rotation, 1)]
        elif t == "DIMENSION":
            row += [_r(e.get_measurement()), _dim_text(e)]
    except Exception:  # noqa: BLE001
        pass
    return row


@tool
def dxf_info(path: str) -> dict:
    """DXF áttekintés: verzió, egység, befoglaló méret, elemszám típusonként, rétegek/blokkok száma."""
    try:
        doc, notes = _load(path)
    except Exception as e:  # noqa: BLE001
        return _fail(e)
    msp = doc.modelspace()
    types = Counter(e.dxftype() for e in msp)
    out = {"ok": True, "version": doc.dxfversion, "units": _units(doc), "mm_per_unit": _mm(doc),
           "entities": sum(types.values()), "types": dict(types.most_common(15)), "layers": len(doc.layers),
           "blocks": sum(1 for b in doc.blocks if not b.name.startswith("*")),
           "layouts": [lay.name for lay in doc.layouts if lay.name != "Model"][:10]}
    try:
        from ezdxf import bbox
        ext = bbox.extents(msp, fast=True)
        if ext.has_data:
            out["extents"] = {"min": [_r(ext.extmin.x, 2), _r(ext.extmin.y, 2)],
                              "max": [_r(ext.extmax.x, 2), _r(ext.extmax.y, 2)],
                              "size": [_r(ext.size.x, 2), _r(ext.size.y, 2)]}
    except Exception:  # noqa: BLE001
        pass
    if notes:
        out["notes"] = notes
    return out


@tool
def dxf_layers(path: str) -> dict:
    """Rétegek: név, szín, be/ki, fagyasztott, elemszám, vonalhossz mm-ben a blokkokkal együtt (profilhossz)."""
    try:
        doc, _ = _load(path)
    except Exception as e:  # noqa: BLE001
        return _fail(e)
    cnt, length = Counter(e.dxf.layer for e in doc.modelspace()), _lengths(doc)
    rows = []
    for layer in doc.layers:
        n = layer.dxf.name
        row = {"n": n, "c": layer.color, "db": cnt.get(n, 0)}
        if length.get(n):
            row["len_mm"] = _r(length[n] * _mm(doc), 1)
        if not layer.is_on():
            row["off"] = True
        if layer.is_frozen():
            row["frozen"] = True
        rows.append(row)
    rows.sort(key=lambda r: -r["db"])
    return {"ok": True, "layers": rows}


@tool
def dxf_texts(path: str, pattern: str = "", limit: int = 100) -> dict:
    """Szövegek (TEXT/MTEXT, blokk-attribútumok) pozícióval; pattern = regex szűrő (kis/nagybetű mindegy)."""
    try:
        rx = re.compile(pattern, re.I) if pattern else None
        doc, _ = _load(path)
    except re.error as e:
        return {"ok": False, "reason": f"hibás regex: {e}"}
    except Exception as e:  # noqa: BLE001
        return _fail(e)
    rows, total = [], 0
    lim = max(1, min(int(limit), 500))
    for e in doc.modelspace():
        items = []
        if e.dxftype() in ("TEXT", "MTEXT"):
            items.append((_text(e), e.dxf.insert, e.dxf.layer, None))
        elif e.dxftype() == "INSERT":
            items += [(_text(a), a.dxf.insert, e.dxf.layer, a.dxf.tag) for a in e.attribs]
        for txt, ins, layer, tag in items:
            if not txt or (rx and not rx.search(txt)):
                continue
            total += 1
            if len(rows) < lim:
                rows.append([txt[:120], [_r(ins.x, 2), _r(ins.y, 2)], layer] + ([tag] if tag else []))
    return {"ok": True, "n": total, "more": total > len(rows), "texts": rows}


@tool
def dxf_entities(path: str, layer: str = "", type: str = "", limit: int = 50) -> dict:  # noqa: A002
    """Elemek tömören [típus, réteg, geometria]; szűrés réteg és típus szerint (pl. type='LINE;CIRCLE')."""
    try:
        doc, _ = _load(path)
    except Exception as e:  # noqa: BLE001
        return _fail(e)
    types = {t.strip().upper() for t in type.split(";") if t.strip()}
    rows, total = [], 0
    lim = max(1, min(int(limit), 300))
    for e in doc.modelspace():
        if layer and e.dxf.layer.lower() != layer.lower():
            continue
        if types and e.dxftype() not in types:
            continue
        total += 1
        if len(rows) < lim:
            rows.append(_brief(e))
    return {"ok": True, "n": total, "more": total > len(rows), "entities": rows}


def _block_counts(doc) -> tuple[Counter, Counter]:
    """Blokkpéldányok: közvetlenül a modelltérben (top) és összesen, a beágyazottakkal szorozva (all).
    A névtelen blokkok (*D méretezés, *U stb.) kimaradnak – nem önálló darabok."""
    top = Counter(e.dxf.name for e in doc.modelspace().query("INSERT"))
    inner = {b.name: Counter(e.dxf.name for e in b.query("INSERT")) for b in doc.blocks}
    total: Counter = Counter()

    def walk(name: str, mult: int, depth: int) -> None:
        if depth > 20:  # körkörös hivatkozás ellen
            return
        total[name] += mult
        for child, n in inner.get(name, {}).items():
            walk(child, mult * n, depth + 1)

    for name, n in top.items():
        walk(name, n, 0)

    def named(c: Counter) -> Counter:
        return Counter({k: v for k, v in c.items() if not k.startswith("*")})

    return named(top), named(total)


@tool
def dxf_bom(path: str, merge: bool = False) -> dict:
    """Darabjegyzék: blokkok darabszáma (beágyazottakkal); merge=true cikkszám szerint összevon; hossz mm/réteg."""
    try:
        doc, _ = _load(path)
    except Exception as e:  # noqa: BLE001
        return _fail(e)
    top, total = _block_counts(doc)
    length, dims = _lengths(doc), []
    for e in doc.modelspace().query("DIMENSION")[:100]:
        try:
            dims.append([_r(e.get_measurement(), 2), _dim_text(e), e.dxf.layer])
        except Exception:  # noqa: BLE001
            pass
    if merge:
        groups: dict = {}
        for name, n in total.items():
            m = CIKK.match(name)
            g = groups.setdefault(m.group(1) if m else name, {"db": 0, "var": 0, "cikk": bool(m)})
            g["db"] += n
            g["var"] += 1
        rows = [{"item": k, "db": g["db"], "valtozat": g["var"], "mibol": "cikkszám" if g["cikk"] else "név"}
                for k, g in sorted(groups.items(), key=lambda x: (-x[1]["db"], x[0]))]
    else:
        rows = [{"block": n, "db": c, **({"top": top[n]} if top.get(n, 0) != c else {})}
                for n, c in sorted(total.items(), key=lambda x: (-x[1], x[0]))]
    mm = _mm(doc)
    out = {"ok": True, "units": _units(doc), "n": len(rows), "rows": rows[:100],
           "length_mm_by_layer": {k: _r(v * mm, 1) for k, v in sorted(length.items(), key=lambda x: -x[1]) if v > 0}}
    if len(rows) > 100:
        out["more"] = True
    if not merge and sum(1 for n in total if CIKK.match(n)) >= 5:
        out["hint"] = "sok cikkszám-nevű blokk – merge=true összevonja a változatokat"
    if dims:
        out["dims"] = dims
    return out


def _csv_cell(v) -> str:
    v = str(v)
    return '"' + v.replace('"', '""') + '"' if re.search(r'[";\n]', v) else v


@tool
def dxf_attribs(path: str, block: str = "", csv: str = "", limit: int = 30) -> dict:
    """Blokk-attribútumok táblázatként (beszúrásonként egy sor, címkék oszlopok); csv=útvonal → Excel-barát CSV."""
    try:
        doc, _ = _load(path)
    except Exception as e:  # noqa: BLE001
        return _fail(e)
    cols, rows = [], []
    for e in doc.modelspace().query("INSERT"):
        if block and e.dxf.name.lower() != block.lower():
            continue
        vals = {}
        for a in e.attribs:
            tag = a.dxf.tag
            if tag not in cols:
                cols.append(tag)  # oszlopsorrend: első előfordulás szerint
            vals.setdefault(tag, _text(a))  # ugyanaz a címke kétszer: az első marad
        if vals:
            rows.append((e.dxf.name, round(e.dxf.insert.x), round(e.dxf.insert.y), vals))
    if not rows:
        return {"ok": False, "reason": "nincs attribútumos blokkbeszúrás" + (f" ({block})" if block else "")}
    rows.sort(key=lambda r: (r[0], -r[2], r[1]))
    head = ["Blokk", "X", "Y"] + cols
    table = [[b, x, y] + [v.get(c, "") for c in cols] for b, x, y, v in rows]
    out = {"ok": True, "n": len(table), "cols": head, "rows": table[:max(0, min(int(limit), 200))]}
    if csv:
        target = Path(csv).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [";".join(_csv_cell(v) for v in r) for r in [head] + table]
        # pontosvessző + UTF-8 BOM: a magyar Excel így oszlopokra bontva nyitja meg (mint az Alnico)
        with open(target, "w", encoding="utf-8-sig", newline="") as f:
            f.write("\r\n".join(lines) + "\r\n")
        out["csv"] = str(target)
    return out


def _add_entities(doc, entities: list, layers: dict) -> tuple[Counter, list]:
    msp = doc.modelspace()
    for name, color in (layers or {}).items():
        if name not in doc.layers:
            doc.layers.add(name, color=int(color))
        else:
            doc.layers.get(name).color = int(color)
    done, errors = Counter(), []
    for i, it in enumerate(entities):
        try:
            t = str(it.get("t", "")).lower()
            attr = {"layer": str(it.get("layer", "0"))}
            if attr["layer"] not in doc.layers:
                doc.layers.add(attr["layer"])
            if t == "line":
                a, b = it["p"]
                msp.add_line(a, b, dxfattribs=attr)
            elif t == "poly":
                msp.add_lwpolyline(it["p"], close=bool(it.get("closed")), dxfattribs=attr)
            elif t == "rect":
                x, y = it["p"]
                w, h = float(it["w"]), float(it["h"])
                msp.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], close=True, dxfattribs=attr)
            elif t == "circle":
                msp.add_circle(it["c"], float(it["r"]), dxfattribs=attr)
            elif t == "arc":
                a0, a1 = it["a"]
                msp.add_arc(it["c"], float(it["r"]), float(a0), float(a1), dxfattribs=attr)
            elif t == "text":
                txt = msp.add_text(str(it["text"]), height=float(it.get("h", 2.5)),
                                   rotation=float(it.get("rot", 0)), dxfattribs=attr)
                txt.set_placement(it["p"])
            elif t == "dim":
                (x1, y1), (x2, y2) = it["p"]
                dist = float(it.get("dist", 10))
                ang = math.atan2(y2 - y1, x2 - x1)
                base = ((x1 + x2) / 2 - math.sin(ang) * dist, (y1 + y2) / 2 + math.cos(ang) * dist)
                msp.add_linear_dim(base=base, p1=(x1, y1), p2=(x2, y2), angle=math.degrees(ang),
                                   dxfattribs=attr).render()
            elif t == "insert":
                if it["block"] not in doc.blocks:
                    raise ValueError(f"nincs ilyen blokk: {it['block']}")
                s = float(it.get("scale", 1))
                msp.add_blockref(it["block"], it["p"], dxfattribs={**attr, "xscale": s, "yscale": s,
                                                                  "rotation": float(it.get("rot", 0))})
            else:
                raise ValueError(f"ismeretlen elemtípus: {t!r}")
            done[t] += 1
        except Exception as e:  # noqa: BLE001
            errors.append({"i": i, "error": f"{type(e).__name__}: {e}"[:150]})
    return done, errors


def _save(doc, target: Path, done: Counter, errors: list) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(target)
    out = {"ok": True, "path": str(target), "added": dict(done)}
    if errors:
        out["errors"] = errors[:10]
    return out


@tool
def dxf_write(path: str, entities: list, layers: dict | None = None, units: str = "mm", overwrite: bool = False) -> dict:
    """Új DXF (R2018) JSON-elemlistából: line/poly/rect/circle/arc/text/dim; rétegszín: layers={'név': ACI}."""
    import ezdxf

    p = Path(path).expanduser()
    if p.suffix.lower() != ".dxf":
        return {"ok": False, "reason": "a kimenet kiterjesztése .dxf legyen"}
    if p.exists() and not overwrite:
        return {"ok": False, "reason": f"már létezik: {p}", "hint": "overwrite=true, vagy dxf_add új fájlba"}
    code = {"mm": 4, "cm": 5, "m": 6, "inch": 1}.get(units.lower())
    if code is None:
        return {"ok": False, "reason": f"ismeretlen egység: {units} (mm, cm, m, inch)"}
    doc = ezdxf.new("R2018", setup=True, units=code)
    done, errors = _add_entities(doc, entities or [], layers or {})
    if not done:
        return {"ok": False, "reason": "egy elem sem került a rajzba", "errors": errors[:10]}
    return _save(doc, p, done, errors)


@tool
def dxf_add(path: str, entities: list, layers: dict | None = None, out: str = "") -> dict:
    """Elemek hozzáadása meglévő DXF-hez ÚJ fájlba (alap: <név>_mod.dxf) – az eredetit sosem írja felül."""
    src = Path(path).expanduser()
    target = Path(out).expanduser() if out else src.with_name(f"{src.stem}_mod.dxf")
    if target.suffix.lower() != ".dxf":
        return {"ok": False, "reason": "a kimenet kiterjesztése .dxf legyen"}
    if src.exists() and target.resolve() == src.resolve():
        return {"ok": False, "reason": "az eredeti rajzot nem írom felül – adj meg másik out-ot"}
    try:
        doc, _ = _load(path)
    except Exception as e:  # noqa: BLE001
        return _fail(e)
    done, errors = _add_entities(doc, entities or [], layers or {})
    if not done:
        return {"ok": False, "reason": "egy elem sem került a rajzba", "errors": errors[:10]}
    return _save(doc, target, done, errors)


@tool
def dxf_render(path: str, out: str = "", max_px: int = 1600, layout: str = "Model") -> dict:
    """DXF → PNG (fehér háttér, fekete vonal); csak útvonalat ad – előbb local_see írja le, Claude csak ha kell."""
    try:
        doc, _ = _load(path)
    except Exception as e:  # noqa: BLE001
        return _fail(e)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    if layout == "Model":
        space = doc.modelspace()
    elif layout in doc.layouts:
        space = doc.layouts.get(layout)
    else:
        return {"ok": False, "reason": f"nincs ilyen elrendezés: {layout}"}
    if not len(space):
        return {"ok": False, "reason": "üres rajz / elrendezés"}
    px = max(400, min(int(max_px), 4000))
    fig = plt.figure(figsize=(px / 100, px / 100), dpi=100)
    ax = fig.add_axes([0, 0, 1, 1])
    cfg = Configuration(background_policy=BackgroundPolicy.WHITE, color_policy=ColorPolicy.BLACK)
    target = Path(out).expanduser() if out else OUT / f"{Path(path).stem[:80]}.png"
    try:
        Frontend(RenderContext(doc), MatplotlibBackend(ax), config=cfg).draw_layout(space, finalize=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(target, dpi=100, facecolor="white")
    except Exception as e:  # noqa: BLE001
        return _fail(e)
    finally:
        plt.close(fig)
    return {"ok": True, "png": str(target), "kb": round(target.stat().st_size / 1024)}


@tool
def dxf_open(path: str) -> dict:
    """DXF megnyitása a saját CAD-programban (Alnico); ha nincs telepítve, a Windows társított programjával."""
    p = Path(path).expanduser()
    if not p.is_file() or p.suffix.lower() != ".dxf":
        return {"ok": False, "reason": f"nincs ilyen DXF fájl: {path}"}
    if ALNICO.is_file():
        subprocess.Popen([str(ALNICO), str(p.resolve())], close_fds=True)
        return {"ok": True, "app": "Alnico", "path": str(p.resolve())}
    os.startfile(p.resolve())  # noqa: S606
    return {"ok": True, "app": "társított program", "path": str(p.resolve())}
