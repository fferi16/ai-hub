"""Élő próba az Alnico automatizálási felületéhez (csak akkor, ha az új Alnico fut).

    .venv\\Scripts\\python.exe plugins\\alnico\\tests\\live_check.py [automata.json útvonala]

Megnyitja a dxf plugin tesztrajzát, lekéri az összefoglalót, rajzol egy téglalapot és egy vonalat
a parancssoron át, majd ellenőrzi, hogy a szerkesztések száma nőtt. Az eredeti fájlt nem menti.
"""
import json
import os
import sys
from pathlib import Path

if len(sys.argv) > 1:
    os.environ["ALNICO_AUTOMATA_FILE"] = sys.argv[1]
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[2]), str(HERE.parent)]
import plugin as p  # noqa: E402

MINTA = str(HERE.parents[1] / "dxf" / "tests" / "minta.dxf")
fails = []


def check(name, cond, res):
    print(("OK  " if cond else "HIBA") + f" {name}: {json.dumps(res, ensure_ascii=False)[:300]}")
    if not cond:
        fails.append(name)


st = p.alnico_status()
check("állapot", st.get("ok"), st)
if not st.get("ok"):
    sys.exit(1)
r = p.alnico_open(MINTA)
check("megnyitás", r.get("ok"), r)
d0 = p.alnico_drawing()
check("rajz", d0.get("ok") and any(l["n"] == "PROFIL" for l in d0.get("retegek", [])), d0)
c = p.alnico_cmd(["téglalap", "0,-3000", "@500,300", "<esc>", "vonal", "0,-3500", "@1000<0", "<enter>", "<esc>"])
check("parancsok", c.get("ok") and not c.get("kerdes"), c)
check("pontok visszaigazolva", sum(1 for s in c.get("sorok", []) if s.startswith("Pont:")) == 4, c)
d1 = p.alnico_drawing()
check("rajzolt elemek látszanak", (d1.get("rajzolt") or {}).get("db", 0) >= 2, d1.get("rajzolt"))
c3 = p.alnico_cmd("kör\n2000;-3000\n@150,0\n<esc>")
check("szöveges bemenet, ;-s koordináta", c3.get("ok") and any("kész" in s for s in c3.get("sorok", [])), c3)
c2 = p.alnico_cmd(["nincsilyenparancs"])
check("ismeretlen parancs jelezve", any("Nem ismerem" in s for s in c2.get("sorok", [])), c2)
st2 = p.alnico_status()
check("nincs függő eszköz", st2.get("ok") and not st2.get("eszkoz") and not st2.get("kerdes"), st2)
print("\nEREDMÉNY:", "minden rendben" if not fails else f"{len(fails)} hiba: {fails}")
sys.exit(1 if fails else 0)
