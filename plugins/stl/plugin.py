"""stl – 2D profilból 3D test a nyomtatóhoz: DXF-kontúr → kihúzás / forgatás → bináris STL (mm).

Kontúrok a DXF-ből (ezdxf): zárt polivonal (íves szakaszokkal is), kör, ellipszis, spline, és az egymáshoz
illeszkedő vonal/ív darabokból összefűzött zárt hurkok. Egymásba ágyazás páros-páratlan szabállyal:
külső = tömör, benne = lyuk, a lyukban = újra tömör (sziget). Háromszögelés: ezdxf beépített earcut.
Új függőség nincs. Minden kimenet ellenőrizve: vízzáró és következetes irányú-e (minden élt pontosan két
háromszög használ, ellentétes irányban), térfogat, méret. Egymást METSZŐ kontúrokat nem egyesít (ilyenkor
a rajzot kell rendbe tenni). Az STL mm-ben, Z-felfelé áll (forgatott testnél a rajz Y-ja lesz a Z).
  stl_extrude – profil kihúzása adott magasságra (pl. tömítés, alátét, profilmetszet-darab)
  stl_revolve – profil forgatása a függőleges (Y) tengely körül (pl. persely, gomb, dugó); x = sugár
  stl_info    – egy STL ellenőrzése (háromszögek, méret, térfogat, vízzáróság)
  stl_preview – izometrikus PNG előnézet (a képet előbb local_see írja le, Claude csak ha kell)
"""
import math
import struct
from collections import Counter
from pathlib import Path

from hubkit import tool

OUT = Path(__file__).resolve().parents[2] / "out" / "stl"
MM_PER_UNIT = {1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0}
CURVES = ("LINE", "ARC", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ELLIPSE", "SPLINE")


# ---- kontúrok -----------------------------------------------------------------

def _entities(doc, layer: str, blocks: bool):
    layers = {s.strip().lower() for s in layer.split(";") if s.strip()}

    def walk(ents, inherit, depth):
        for e in ents:
            lay = e.dxf.layer if not (inherit and e.dxf.layer == "0") else inherit
            if e.dxftype() == "INSERT":
                if blocks and depth < 10 and not e.dxf.name.startswith("*"):
                    yield from walk(e.virtual_entities(), lay, depth + 1)
                continue
            if e.dxftype() in CURVES and (not layers or lay.lower() in layers):
                yield e

    yield from walk(doc.modelspace(), None, 0)


def _polylines(ents, tol: float) -> list[list[tuple]]:
    from ezdxf import path as zpath
    out = []
    for e in ents:
        try:
            pts = [(v.x, v.y) for v in zpath.make_path(e).flattening(tol)]
        except Exception:  # noqa: BLE001
            continue
        if len(pts) >= 2:
            out.append(pts)
    return out


def _close(a, b, eps):
    return abs(a[0] - b[0]) <= eps and abs(a[1] - b[1]) <= eps


def _loops(polys: list[list[tuple]], eps: float) -> tuple[list[list[tuple]], int]:
    """Zárt hurkok: a magukban zártak, plusz a végpontjaiknál összefűzött nyitott darabok."""
    loops, open_ = [], []
    for p in polys:
        (loops if len(p) >= 4 and _close(p[0], p[-1], eps) else open_).append(p)
    leftovers = 0
    while open_:
        chain = open_.pop()
        grown = True
        while grown and not _close(chain[0], chain[-1], eps):
            grown = False
            for i, q in enumerate(open_):
                if _close(chain[-1], q[0], eps):
                    chain += q[1:]
                elif _close(chain[-1], q[-1], eps):
                    chain += q[-2::-1]
                elif _close(chain[0], q[-1], eps):
                    chain = q[:-1] + chain
                elif _close(chain[0], q[0], eps):
                    chain = q[:0:-1] + chain
                else:
                    continue
                open_.pop(i)
                grown = True
                break
        if len(chain) >= 4 and _close(chain[0], chain[-1], eps):
            loops.append(chain)
        else:
            leftovers += 1
    clean = []
    for lp in loops:
        pts = [lp[0]]
        for p in lp[1:-1]:
            if not _close(p, pts[-1], eps * 0.01):
                pts.append(p)
        if len(pts) >= 3 and abs(_area(pts)) > eps * eps:
            clean.append(pts)
    return clean, leftovers


def _area(pts) -> float:
    return 0.5 * sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1] for i in range(len(pts))) * -1


def _inside(pt, poly) -> bool:
    x, y, inside = pt[0], pt[1], False
    for i in range(len(poly)):
        (x1, y1), (x2, y2) = poly[i - 1], poly[i]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _regions(loops):
    """Páros-páratlan egymásba ágyazás → [(külső hurok CCW, [lyukak CW])]."""
    order = sorted(range(len(loops)), key=lambda i: -abs(_area(loops[i])))
    depth, parent = {}, {}
    for k, i in enumerate(order):
        probe = loops[i][0]
        cont = [j for j in order[:k] if _inside(probe, loops[j])]
        depth[i] = len(cont)
        parent[i] = cont[-1] if cont else None  # a legkisebb tartalmazó (rendezés miatt az utolsó)
    regions = {}
    for i in order:
        lp = loops[i]
        if depth[i] % 2 == 0:
            regions[i] = (lp if _area(lp) > 0 else lp[::-1], [])
    for i in order:
        if depth[i] % 2 == 1 and parent[i] in regions:
            lp = loops[i]
            regions[parent[i]][1].append(lp if _area(lp) < 0 else lp[::-1])
    return list(regions.values())


def _profile(path: str, layer: str, tol: float, blocks: bool) -> tuple[list, dict]:
    import ezdxf
    from ezdxf import recover

    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"nincs ilyen fájl: {path}")
    if p.suffix.lower() != ".dxf":
        raise ValueError(f"nem DXF fájl: {p.suffix}")
    try:
        doc = ezdxf.readfile(p)
    except ezdxf.DXFStructureError:
        doc, _ = recover.readfile(p)
    mm = MM_PER_UNIT.get(doc.header.get("$INSUNITS", 0), 1.0)
    ents = list(_entities(doc, layer, blocks))
    if not ents:
        raise ValueError("nincs görbe a rajzban" + (f" a(z) {layer} rétegen" if layer else ""))
    polys = [[(x * mm, y * mm) for x, y in pl] for pl in _polylines(ents, tol / mm)]
    loops, leftovers = _loops(polys, max(0.01, tol))
    if not loops:
        raise ValueError("nincs zárt kontúr (a vonalak végpontjai nem érnek össze?)")
    regions = _regions(loops)
    info = {"loops": len(loops), "solids": len(regions), "holes": sum(len(h) for _, h in regions)}
    if leftovers:
        info["open_chains_ignored"] = leftovers
    return regions, info


# ---- háló ---------------------------------------------------------------------

def _cap(outer, holes, z, up: bool) -> list:
    from ezdxf.math.triangulation import mapbox_earcut_2d
    tris = []
    for a, b, c in mapbox_earcut_2d(outer, holes):
        ccw = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]) > 0
        if ccw != up:
            b, c = c, b
        tris.append(((a[0], a[1], z), (b[0], b[1], z), (c[0], c[1], z)))
    return tris


def _extrude(regions, h: float) -> list:
    tris = []
    for outer, holes in regions:
        tris += _cap(outer, holes, 0.0, False) + _cap(outer, holes, h, True)
        for loop in [outer] + holes:  # külső CCW, lyuk CW → a fal normálisa mindig kifelé (a testből)
            for i in range(len(loop)):
                (ax, ay), (bx, by) = loop[i], loop[(i + 1) % len(loop)]
                a0, b0, a1, b1 = (ax, ay, 0.0), (bx, by, 0.0), (ax, ay, h), (bx, by, h)
                tris += [(a0, b0, b1), (a0, b1, a1)]
    return tris


def _revolve(regions, angle: float, seg: int) -> list:
    full = angle >= 359.999
    n = max(3, seg if full else max(2, round(seg * angle / 360)))
    steps = [math.radians(angle) * k / n for k in range(n + (0 if full else 1))]

    def rot(p, t):  # a rajz Y tengelye körül (x = sugár, y = magasság); a kimenet Z-felfelé áll, mint a tálcán
        return (p[0] * math.cos(t), p[0] * math.sin(t), p[1])

    tris = []
    for outer, holes in regions:
        for loop in [outer] + holes:
            for k in range(n):
                t0, t1 = steps[k], steps[(k + 1) % len(steps)] if full else steps[k + 1]
                for i in range(len(loop)):
                    a, b = loop[i], loop[(i + 1) % len(loop)]
                    a0, b0, a1, b1 = rot(a, t0), rot(b, t0), rot(a, t1), rot(b, t1)
                    tris += [(a0, b1, b0), (a0, a1, b1)]
        if not full:  # véglapok a kezdő és a záró szögnél
            # a profilsík (u,v) → (x,z) leképezés miatt a kezdő lapnál a „felfelé" néző háromszög néz kifelé
            for t, up in ((steps[0], True), (steps[-1], False)):
                for tri in _cap(outer, holes, 0.0, up):
                    tris.append(tuple(rot((p[0], p[1]), t) for p in tri))
    return tris


def _key(p, q=1e-6):
    return (round(p[0] / q), round(p[1] / q), round(p[2] / q))


def _check(tris) -> dict:
    """Degenerált háromszögek ki; vízzáróság: minden irányított él pontosan egyszer szerepel, és a párja
    (ellenkező irányban) is pontosan egyszer – így a lapok iránya is következetes; térfogat, befoglaló."""
    good, edges, vol = [], Counter(), 0.0
    for t in tris:
        ka, kb, kc = _key(t[0]), _key(t[1]), _key(t[2])
        if ka == kb or kb == kc or ka == kc:
            continue
        ux, uy, uz = (t[1][i] - t[0][i] for i in range(3))
        vx, vy, vz = (t[2][i] - t[0][i] for i in range(3))
        if math.hypot(uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx) < 1e-12:
            continue
        good.append(t)
        for e in ((ka, kb), (kb, kc), (kc, ka)):
            edges[e] += 1
        (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = t
        vol += (x1 * (y2 * z3 - y3 * z2) - x2 * (y1 * z3 - y3 * z1) + x3 * (y1 * z2 - y2 * z1)) / 6
    bad = sum(1 for (a, b), c in edges.items() if c != 1 or edges.get((b, a)) != 1)
    xs = [p[i] for t in good for p in t for i in (0,)]
    ys = [p[1] for t in good for p in t]
    zs = [p[2] for t in good for p in t]
    size = [round(max(v) - min(v), 3) for v in (xs, ys, zs)] if good else [0, 0, 0]
    return {"tris": good, "triangles": len(good), "watertight": bad == 0, "bad_edges": bad,
            "volume_mm3": round(vol, 1), "size_mm": size}


def _write_stl(tris, target: Path, name: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as f:
        f.write(name.encode("ascii", "replace")[:80].ljust(80, b" "))
        f.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            ux, uy, uz = (b[i] - a[i] for i in range(3))
            vx, vy, vz = (c[i] - a[i] for i in range(3))
            nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            f.write(struct.pack("<12fH", nx / ln, ny / ln, nz / ln, *a, *b, *c, 0))


def _read_stl(path: Path) -> list:
    data = path.read_bytes()
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if 84 + n * 50 == len(data):
            return [tuple(tuple(struct.unpack_from("<3f", data, 84 + i * 50 + 12 + k * 12)) for k in range(3))
                    for i in range(n)]
    tris, cur = [], []  # ASCII STL
    for line in data.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if parts[:1] == ["vertex"]:
            cur.append(tuple(float(v) for v in parts[1:4]))
            if len(cur) == 3:
                tris.append(tuple(cur))
                cur = []
    return tris


def _finish(tris, src: str, out: str, suffix: str, info: dict) -> dict:
    res = _check(tris)
    target = Path(out).expanduser() if out else OUT / f"{Path(src).stem[:80]}_{suffix}.stl"
    if target.suffix.lower() != ".stl":
        return {"ok": False, "reason": "a kimenet kiterjesztése .stl legyen"}
    _write_stl(res.pop("tris"), target, f"AI HUB stl: {Path(src).name}")
    out_ = {"ok": True, "stl": str(target), "kb": round(target.stat().st_size / 1024), **info, **res}
    if not res["watertight"]:
        out_["hint"] = "nem vízzáró – egymást metsző kontúrok? (a szeletelő általában javítja)"
    return out_


@tool
def stl_extrude(path: str, height: float, layer: str = "", out: str = "", tol: float = 0.05,
                blocks: bool = False) -> dict:
    """DXF zárt kontúrjai (lyukakkal) kihúzva height mm-re → STL; layer=réteg(ek) ';'-vel; méret, vízzáróság."""
    try:
        h = float(height)
        if not 0 < h <= 10000:
            return {"ok": False, "reason": "a magasság 0 és 10000 mm között legyen"}
        regions, info = _profile(path, layer, max(0.001, float(tol)), blocks)
    except ValueError as e:
        return {"ok": False, "reason": str(e)}
    return _finish(_extrude(regions, h), path, out, f"h{h:g}", info)


@tool
def stl_revolve(path: str, angle: float = 360, segments: int = 96, layer: str = "", out: str = "",
                tol: float = 0.05) -> dict:
    """DXF zárt kontúrja az Y tengely körül forgatva (x = sugár ≥ 0) → STL; angle < 360: véglapokkal."""
    try:
        a = float(angle)
        if not 0 < a <= 360:
            return {"ok": False, "reason": "a szög 0 és 360 fok között legyen"}
        regions, info = _profile(path, layer, max(0.001, float(tol)), False)
    except ValueError as e:
        return {"ok": False, "reason": str(e)}
    if min(p[0] for outer, _ in regions for p in outer) < -1e-6:
        return {"ok": False, "reason": "a profil átnyúlik az Y tengelyen (x < 0) – forgatáshoz x ≥ 0 kell"}
    return _finish(_revolve(regions, a, max(8, min(int(segments), 720))), path, out, f"rev{a:g}", info)


@tool
def stl_info(path: str) -> dict:
    """STL ellenőrzése: háromszögek, méret mm-ben, térfogat, vízzáró-e (nyomtatás előtt)."""
    p = Path(path).expanduser()
    if not p.is_file() or p.suffix.lower() != ".stl":
        return {"ok": False, "reason": f"nincs ilyen STL fájl: {path}"}
    res = _check(_read_stl(p))
    res.pop("tris")
    return {"ok": True, "kb": round(p.stat().st_size / 1024), **res}


@tool
def stl_preview(path: str, out: str = "", max_px: int = 800) -> dict:
    """STL izometrikus PNG előnézete (árnyalt); csak útvonalat ad – előbb local_see írja le."""
    p = Path(path).expanduser()
    if not p.is_file() or p.suffix.lower() != ".stl":
        return {"ok": False, "reason": f"nincs ilyen STL fájl: {path}"}
    tris = _read_stl(p)
    if not tris:
        return {"ok": False, "reason": "üres STL"}
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    step = max(1, len(tris) // 60000)  # nagy hálónál ritkítva (csak előnézet)
    elev, azim = math.radians(28), math.radians(-55)
    cam = (math.cos(elev) * math.cos(azim), math.cos(elev) * math.sin(azim), math.sin(elev))
    shown, shade = [], []
    for a, b, c in tris[::step]:
        ux, uy, uz = (b[i] - a[i] for i in range(3))
        vx, vy, vz = (c[i] - a[i] for i in range(3))
        n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
        ln = math.sqrt(sum(v * v for v in n)) or 1.0
        facing = (n[0] * cam[0] + n[1] * cam[1] + n[2] * cam[2]) / ln
        if facing <= 0:  # hátlap: a matplotlib nem takar rendesen, ezért ki sem rajzoljuk
            continue
        light = max(0.0, (n[0] * 0.3 + n[1] * -0.4 + n[2] * 0.85) / ln)
        g = 0.35 + 0.6 * light
        shown.append((a, b, c))
        shade.append((g * 0.55, g * 0.7, g * 0.95))
    tris = shown
    px = max(300, min(int(max_px), 2000))
    fig = plt.figure(figsize=(px / 100, px / 100), dpi=100)
    ax = fig.add_subplot(projection="3d")
    ax.add_collection3d(Poly3DCollection(tris, facecolors=shade, edgecolors="none"))
    pts = [q for t in tris for q in t]
    lo = [min(q[i] for q in pts) for i in range(3)]
    hi = [max(q[i] for q in pts) for i in range(3)]
    span = max(h - l for l, h in zip(lo, hi)) / 2 or 1
    mid = [(l + h) / 2 for l, h in zip(lo, hi)]
    ax.set_xlim(mid[0] - span, mid[0] + span)
    ax.set_ylim(mid[1] - span, mid[1] + span)
    ax.set_zlim(mid[2] - span, mid[2] + span)
    ax.view_init(elev=math.degrees(elev), azim=math.degrees(azim))
    ax.set_axis_off()
    target = Path(out).expanduser() if out else p.with_suffix(".png")
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(target, dpi=100, facecolor="white", bbox_inches="tight")
    plt.close(fig)
    return {"ok": True, "png": str(target), "kb": round(target.stat().st_size / 1024)}
