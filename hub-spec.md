# Hub – Claude helyi központja (1. kör: a laptopon)

## Cél
Egyetlen helyi szerver a Windows laptopon, amin keresztül Claude (Claude Code, később más kliens is) **minden programot, adatot és eszközt elér**, és ami **magától bővül**: ha Claude-nak hiányzik egy képesség, megírja pluginként, leteszteli és regisztrálja.

## Környezet
- Windows 11 laptop (MSI Thin 15), Python 3.11+, egy közös venv: `C:\Hub\.venv`
- MCP-szerver: hivatalos `mcp` Python SDK (FastMCP), **streamable HTTP**, csak `127.0.0.1:8765` címen (kívülről nem elérhető)
- Claude Code bekötése:
  `claude mcp add --transport http hub http://127.0.0.1:8765/mcp`
- Automatikus indítás bejelentkezéskor (Feladatütemező), tálcaikon állapottal

## Mappaszerkezet
```
C:\Hub\
  hub.py               # a szerver
  config.json          # port, szintek, beállítások
  plugins\
    <név>\
      manifest.json    # név, leírás, verzió, eszközök + szintjeik, függőségek
      plugin.py        # az eszközök
      tests\cases.json # bemenet → elvárt kimenet példák
  logs\audit.jsonl     # minden hívás naplója
  out\                 # nagy kimenetek (képek, hosszú szövegek)
```

## Plugin-szerződés
`manifest.json`:
```json
{
  "name": "look",
  "version": "0.1.0",
  "description": "Programok és weboldalak megnyitása, kiolvasása, kattintás",
  "tools": [
    {"name": "look_open", "level": "read"},
    {"name": "look_run",  "level": "act"}
  ],
  "deps": ["playwright", "pywinauto"]
}
```
`plugin.py`: minden eszköz egy dekorált függvény, típusos paraméterekkel és egysoros leírással. A Hub a manifest alapján tölti be őket.

## Jogosultsági szintek
| Szint | Példa | Működés |
|---|---|---|
| `read` | fájl olvasása, képernyő kiolvasása | automatikus |
| `act` | kattintás, fájl írása, program indítása | automatikus, naplózva |
| `danger` | küldés, törlés, fizetés, rendszerbeállítás | **Feri jóváhagyása kell**: Windows-értesítés Igen/Nem gombbal, 2 perc után automatikus Nem |

- A szintet a manifest adja meg, a Hub kényszeríti ki (a plugin nem kerülheti meg)
- Kétes esetben a magasabb szint érvényes

## A Hub saját eszközei
- `hub_list` – pluginok és eszközök tömör listája (név, szint, egysoros leírás)
- `hub_new_plugin(name, description)` – plugin-váz létrehozása sablonból
- `hub_install_deps(plugin)` – függőségek telepítése (`act` szint, pip)
- `hub_test(plugin)` – a `tests\cases.json` lefuttatása, eredmény: hány ment át, melyik bukott el és miért
- `hub_enable(plugin)` / `hub_disable(plugin)` – be/ki kapcsolás, újraindítás nélkül (hot reload)
- `hub_log(n)` – az utolsó n napló-bejegyzés

## Plugin-gyár folyamat
1. Claude észreveszi, hogy hiányzik egy képesség
2. `hub_new_plugin` → megírja a kódot és a bemenet→kimenet teszteket
3. `hub_test` → addig javít, amíg minden teszt át nem megy
4. Engedélyezés:
   - csak `read`/`act` eszközök → automatikusan bekapcsol
   - van `danger` eszköz → Feri jóváhagyása kell a bekapcsoláshoz
5. A plugin innentől minden munkamenetben elérhető

## Folyamatjelző (lebegő ablak)
- `widget.py`: mindig felül lévő mini-ablak a jobb alsó sarokban; a `out\progress\*.json` bejegyzéseket mutatja (százalék vagy mozgó csík, eltelt/hátralévő idő). Egy példány fut, 60 mp tétlenség után bezárul.
- Minden eszközhívás automatikusan bekerül, ha 3 mp-nél tovább fut (`core.invoke`); a kész hívás 20 mp-ig látszik.
- Hosszú munkát végző plugin (vagy háttérfolyamata) jelezze a haladást: `from hubkit import progress` → `progress(kulcs, cím, pct, részlet, state)`; `pct=None` mozgó csíkot ad. Az első bejegyzés magától megnyitja az ablakot.
- Megnyitás kézzel: tálcamenü „Folyamatjelző megnyitása”, vagy a `hub_progress(action="open")` eszköz; `hub_progress()` szövegesen adja az állapotot.
- Tesztek: `python tests\test_progress.py`.

## Tokentakarékosság (minden pluginra kötelező)
- Kimenet mindig tömör JSON
- 4000 karakter fölött a Hub levágja, a teljes szöveget az `out\` mappába menti, és csak az útvonalat + egy rövid kivonatot ad vissza
- Kép soha nem megy automatikusan, csak útvonal; Claude dönti el, megnézi-e
- Eszközleírás legfeljebb 120 karakter (a Hub ennél vág); a használati szabály legyen elöl
- Hosszú szöveg: **előbb összefoglaló** (`media_digest` leirathoz, `local_summarize` másra), a teljes szövegből csak célzott keresés
- Nagy fájl: előbb áttekintés (`pdf_info`, `office_xl_sheets`), majd szűrt olvasás (`pages`, `cells`, `pdf_search`)
- `hub_list` alapból tömör (eszköznevek); leírások: `hub_list(plugin=…)` vagy `detail=true`
- Mérés: `hub_stats` – eszközönkénti kimenet-fogyasztás a naplóból; a legnagyobbat optimalizáld először
- Próba-pluginok (`_probe`, `hello`) éles használatban kikapcsolva

## Biztonság
- Titkok (API-kulcsok, jelszavak) csak a Windows Hitelesítőadat-kezelőben (`keyring`), soha nem a kódban, kimenetben vagy naplóban
- `audit.jsonl`: időpont, eszköz, paraméterek (titkok nélkül), szint, eredmény, futásidő
- Vészleállító: tálcaikon → „Hub leállítása”, és a `Ctrl+Alt+Shift+H` gyorsbillentyű
- Céges fiókokat és gépeket csak a céges szabályok szerint lehet bekötni

## Első plugin: `look`
A részletes leírás a `look-spec.md` fájlban van. Eszközként:
`look_open`, `look_run`, `look_snap`, `look_ask` (helyi 30B VL-modell), `look_recipe`.

## Elfogadási tesztek
1. A Hub elindul, a Claude Code `hub_list` hívása visszaadja a `look` plugint
2. `hub_new_plugin("hello")` + egy `hello_say(name)` eszköz → `hub_test` átmegy → `hub_enable` → újraindítás nélkül hívható
3. Egy `danger` szintű próbaeszköz hívása Windows-értesítést dob; Nem → `{"ok":false,"reason":"denied"}`
4. 10 000 karakteres kimenet → levágva, a teljes szöveg az `out\` mappában
5. Minden fenti hívás megjelenik az `audit.jsonl`-ban
6. Vészleállító után a Claude Code hívása kapcsolódási hibát ad

## CLAUDE.md sor (globális: `%USERPROFILE%\.claude\CLAUDE.md`)
> Elérhető a `hub` MCP-szerver. Munka előtt nézd meg a `hub_list`-et. Ha hiányzik egy képesség, és a feladat újra elő fog fordulni, írd meg pluginként a plugin-gyár folyamat szerint, tesztekkel.

## Ütemterv
- **1. kör (kész):** Hub váz, plugin-gyár, szintek, napló, `look`
- **2. kör – kész (2026-10-19):**
  - `media` – videó/hang leirat (faster-whisper large-v3, GPU), kulcsképkockák, Windows-értesítő
  - folyamatjelző widget (hub-szintű, `hubkit.progress`)
  - `office` – Word/Excel/PowerPoint → PDF elakadásvédelemmel, Excel-olvasás és cellaszintű diff, Word-szöveg (az `xl` helyett)
  - `pdf` – szöveg, szavak/sorok koordinátával, táblázatok, regex-keresés (nbsp/lágy kötőjel normalizálva), oldal PNG-ként
  - `local` – Ollama (alap: qwen3:8b): kérdés-válasz, hosszú szöveg háttér-összefoglalása szakaszonként
  - `study` – kártyák a tanulójegyzetből, Anki-pakli (.apkg), egyfájlos HTML-kvíz
  - `watch` – mappafigyelő: új fájl → media (+ helyi összefoglaló), eredmények visszamásolása
- **2. kör – hátra:** `state` (munkamenet-átadó jegyzet), `index` (kereshető index a fájlokból); `local` VL-modell (képek)
- **Ötletlista:** `audio`/`stems`/`score` (zene), `video` (vágás, fejezetek), `image`, `tts` (magyar felolvasás), `cad`/`cad3d`, `math`, `sim`, `quantum` (Qiskit), `chem`, `data`, `research`
- **3. kör:** mini PC mint állandó központ, a laptop/munkahelyi PC/telefon csomópontként; jóváhagyás telefonon; eseményindítók (e-mail, fájlváltozás, nyomtatás vége); Bambu nyomtató plugin
