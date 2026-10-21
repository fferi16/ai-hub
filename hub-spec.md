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
- Kép megnézése előtt: `local_see` (ingyenes leírás); a képet Claude csak akkor nyissa meg, ha ez nem elég
- Eszközleírás legfeljebb 120 karakter (a Hub ennél vág); a használati szabály legyen elöl
- Hosszú szöveg: **előbb összefoglaló** (`media_digest` leirathoz, `local_summarize` másra), a teljes szövegből csak célzott keresés
- Fájl keresése mappában: előbb `index_search` (részlettel), csak a találatot nyisd meg
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
  - `docs` – dokumentum → tömör Markdown (MarkItDown), gyorsítótárral
  - `state` – munkamenet-átadó jegyzet projektenként (`out\state\<projekt>.jsonl`): végén `state_save`, közben `state_note`, elején `state_load`
  - `index` – teljes szöveges index (SQLite FTS5, ékezet-független, `out\index\index.db`): `index_add(mappa)` növekményes, időkerettel
    (90 s után `incomplete` → újrahívás folytatja), `index_search` → útvonal + rövid részlet; rejtett mappák, `node_modules` kihagyva
  - `local` képek: `local_see` (1–4 kép leírása/kérdés/összehasonlítás, qwen3-vl:30b Ollamán; hidegen ~1 perc, utána ~6 mp/kép),
    `local_see_folder` (mappa képei → .md, gyorsítótárral, időkerettel folytatható)
- **2. kör: kész.**
- **Ötletlistából kész (2026-10-20):** `tts` – magyar felolvasás (Windows OneCore Szabolcs, WinRT): `tts_speak`/`tts_stop`, `tts_save`, `tts_file` (dokumentum → MP3, darabolva);
  `voice` – magyar beszédfelismerés (whisper large-v3, GPU): diktálás Ctrl+Alt+D-vel a kurzor helyére (háttérfolyamat, a Hubbal indul/áll),
  `voice_listen` (mikrofon → szöveg), `voice_transcribe` (rövid hangfájl);
  hangasszisztens ébresztőszóra (`Figyelj, …`): kis whisper (base, CPU) keresi a szót, a large-v3 megerősíti, a kérést a `claude -p`
  válaszolja csak hub-eszközökkel (tartalék: helyi Ollama), a válasz felolvasva; `voice_assistant` szövegből; config:
  `"voice": {"dictation", "hotkey", "wake", "backend"}`; a `claude` parancssori módnak bejelentkezve kell lennie
  gyorsítás: a large-v3 állandóan betöltve (induláskor, a WinRT előtt – fordítva összeomlik); a válasz mondatonként szól
  (stream-json; eszközhívásnál „Egy pillanat, megnézem”); gyors ébresztőszó: openWakeWord-beágyazás + saját MLP
  (`wake_train.py`, nehéz negatív bányászattal), 80 ms-onként, ~0,1–0,2 s késéssel; `voice_enroll` = saját hangminták
  → újratanítás; `"wake_engine": "auto"` = 6+ saját mintától a gyors, addig a whisper-alapú detektor
  „Figyelj”-hallgatás kapcsolója (`out/voice/_hallgat.json`, újraindítás után is megmarad): Ctrl+Alt+F (`"wake_hotkey"`),
  tálcamenü pipa, `voice_dictation(action="wake_on"/"wake_off")`, hanggal „Figyelj, ne figyelj / hallgass el”; kikapcsolva
  a mikrofont is elengedi (a Ctrl+Alt+D diktálás megy); párbeszédben „elég / vége / mégse / zárd be” lezárja a beszélgetést;
  a whisper csend/zaj-hallucinációi („Köszönöm, hogy megnéztétek”) eldobva; a beszélgetés végén mély csippanás
  **Jarvis mód (2026-10-21):** `jarvis.py` – keret nélküli, mindig felül lévő ablak: arany holografikus 3D gömb
  (~10 000 izzó pixel, numpy-vetítés + izzás), tétlenül finoman lélegzik, beszéd közben a felolvasott hang valódi
  hangerő-burkolójára (`hud.envelope`) mozog és véletlenszerűen forog; egérrel forgatható, görgővel méretezhető
  (`"jarvis_size"`), pixeles felirat a gömbön (állapot, hallott kérés, futó eszköz, kimondott mondat, óra), naplópanel
  (eszközhívás kódként), programindításkor a program ikonja (IShellItemImageFactory), állapotjelentés-ívek
  (`system_status`), hologram-kivetítés (eszköz által adott kép arany keretben), kattintásra hallgat, gépelt kérés
  (néma válasz, 🔊 kapcsolóval felolvasva). Események: `hud.py` UDP 127.0.0.1:8771 (→ ablak) és :8772 (→ diktáló).
  Gyors út (`quick.py`): megnyitás, hangerő, idő/dátum, állapotjelentés és protokollok (`"protocols"`) helyben,
  a Hubon át, a Claude nélkül; ami nem biztos, a Claude-hoz megy. Magától szól (`"proactive"`): akku, lemez,
  memória, GPU-hőfok, internet, 2 percnél hosszabb munkák vége; csendes órákban (`"quiet_hours": "22-7"`) és
  kikapcsolt figyelésnél csak a gömb jelez. Jarvis-hangnem: `"persona": "jarvis"`. Néma próba:
  `plugins\voice\tests\jarvis_demo.py`
- **Kész (2026-10-20):** `system` – hogy a hangasszisztens (amely csak hub-eszközöket kap) végre is hajtsa a kéréseket:
  `system_open` (act: URL/domain → alapértelmezett böngésző; fájl/mappa → társított program; programnév → Start menü:
  Get-StartApps + .lnk, kis/nagybetű és ékezet nélkül; magyar becenevek: böngésző, jegyzettömb, számológép, fájlkezelő,
  beállítások, letöltések/dokumentumok/asztal mappa; futtatható fájlt útvonalról nem indít, csak http(s)/mailto/ms-settings
  protokoll; `dry_run` a tesztekhez), `system_apps` (read: Start menü-nevek, szűrhető), `system_volume` (act: Core Audio
  comtypes-szal, saját szálon/COM-lakásban; level, ±change, mute), `system_power` (**danger**: shutdown/restart `shutdown /t N`-nel
  – N>0 esetén a Windows a programokat is bezárja –, sleep 10 mp múlva, lock, cancel = `shutdown /a` + függő alvás törlése)
- **Ötletlista:** `audio`/`stems`/`score` (zene), `video` (vágás, fejezetek), `image`, `math`, `sim`, `quantum` (Qiskit), `chem`, `data`, `research`
- **CAD:** 1) **kész (2026-10-20):** `dxf` plugin (ezdxf + matplotlib): `dxf_info` → célzott `dxf_layers`/`dxf_texts`/`dxf_entities`,
  `dxf_bom` (darabjegyzék beágyazott blokkokkal, `merge` = cikkszám szerint), `dxf_attribs` (attribútum-táblázat → CSV), `dxf_write`/`dxf_add`
  (JSON-elemlistából), `dxf_render` (PNG), `dxf_open` (Alnico). Az Alnicóból átvett szabályok: az eredeti rajzot sosem írja felül
  (`dxf_add` → `<név>_mod.dxf`); hossz rétegenként mm-ben, blokkokba ágyazva is (a blokkon belüli „0” réteg a beszúrásét örökli);
  `*D…` névtelen blokk nem darab; CSV pontosvesszővel + UTF-8 BOM;
  2) **kész (2026-10-20), élőben kipróbálva, kiadva az Alnico 0.5.34-ben:** az Alnico helyi automatizálási felülete (`automata.js` + `renderer/cadautomata.js`):
  csak 127.0.0.1:8767, token a `%APPDATA%\alnico-pdf\automata.json`-ban; a Hub `alnico` pluginja: `alnico_status`, `alnico_drawing`,
  `alnico_open`, `alnico_cmd` (sorok a parancssorba, mint gépelve; vissza a kiírt sorok); 3) **kész (2026-10-20):** `stl` plugin – 2D profilból 3D nyomtatható test, új függőség nélkül (ezdxf earcut):
  `stl_extrude` (zárt kontúrok lyukakkal/szigetekkel, vonal/ív darabokból összefűzve is), `stl_revolve` (Y tengely körül, részszöggel véglapokkal),
  `stl_info` (vízzáró + következetes lapirány, térfogat, méret), `stl_preview` (PNG); kimenet mm-ben, Z-felfelé
- **3. kör:** mini PC mint állandó központ, a laptop/munkahelyi PC/telefon csomópontként; jóváhagyás telefonon; eseményindítók (e-mail, fájlváltozás, nyomtatás vége); Bambu nyomtató plugin
