<p align="center"><img src="assets/ai-hub.png" width="96" alt="AI HUB"></p>

# AI HUB

**Helyi eszközközpont Claude-hoz Windowson.** Egyetlen, csak a saját gépen (127.0.0.1) elérhető MCP-szerver,
amelyen keresztül Claude programokat, fájlokat és helyi AI-modelleket használ – és amely **magától bővül**:
ha hiányzik egy képesség, Claude pluginként megírja, leteszteli és bekapcsolja.

## Mit tud?

| Plugin | Képesség |
|---|---|
| `media` | videó/hang **leirata** (faster-whisper large-v3, GPU-n vagy CPU-n), **diák/kulcsképkockák** kinyerése, Windows-értesítő |
| `office` | Word/Excel/PowerPoint → **PDF** (elakadásvédelemmel), Excel-olvasás és **cellaszintű összehasonlítás**, Word-szöveg |
| `pdf` | szöveg, sorok/szavak **koordinátával**, **táblázatok**, regex-keresés, oldal képként |
| `local` | **helyi LLM** (Ollama): ingyenes kérdés-válasz, hosszú szövegek háttér-összefoglalása, **képek leírása** és összehasonlítása helyi képi modellel |
| `study` | tanulókártyák a jegyzetből, **Anki-pakli** (.apkg), egyfájlos **HTML-kvíz** |
| `watch` | **mappafigyelő**: új videó → leirat + képkockák + összefoglaló, magától |
| `docs` | dokumentum (Word/Excel/PowerPoint/PDF/Outlook/HTML) → **tömör Markdown**, gyorsítótárral |
| `state` | **munkamenet-átadó jegyzet** projektenként: mi kész, mi a következő lépés, döntések, nyitott kérdések |
| `index` | **kereshető index** a mappákból (szöveg + Word/Excel/PPT/PDF/Outlook), ékezet-független, növekményes frissítéssel |
| `dxf` | **DXF-rajzok**: rétegek, szövegek, **darabjegyzék** (cikkszám szerint is), attribútum-CSV, rajzolás, PNG-kép, megnyitás a CAD-programban |
| `alnico` | a saját CAD-program (**Alnico**) **élő vezérlése**: megnyitott rajz lekérdezése, parancsok a parancssorba |
| `stl` | **2D profilból 3D nyomtatás**: DXF-kontúr kihúzása vagy forgatása → ellenőrzött, vízzáró STL, előnézettel |
| `tts` | **magyar felolvasás** helyben (Windows Szabolcs hang): azonnal a hangszórón, vagy szöveg/dokumentum → WAV/MP3 |
| `voice` | **magyar beszédfelismerés** helyben (whisper large-v3): **diktálás Ctrl+Alt+D-vel** bármelyik programba, és **hangasszisztens „Figyelj”-re** (Claude válaszol, hangosan; ki/be: Ctrl+Alt+F vagy a tálcamenü) |
| `system` | **a gép kezelése**: weboldal, fájl, mappa vagy program megnyitása név alapján (Start menü, ékezet nélkül is), **hangerő**, leállítás/újraindítás/alvás/zárolás (jóváhagyással) – így a hangasszisztens végre is hajtja a kérést |
| `look` | programok és weboldalak megnyitása, kiolvasása, kattintás (web, Windows, OCR, helyi képi modell) |

Mellette: **lebegő folyamatjelző** (minden hosszabb munka százalékkal / hátralévő idővel), jóváhagyás
Windows-értesítéssel a veszélyes műveletekhez, napló minden hívásról, vészleállító (`Ctrl+Alt+Shift+H`).

## Telepítés

**Legegyszerűbb:** töltsd le a legújabb `AI-HUB-Setup-x.y.z.exe`-t a [Releases](../../releases) oldalról, és futtasd.
Rendszergazdai jog nem kell (céges gépen is megy). A telepítő:

1. ha nincs, telepíti a Python 3.12-t (winget, felhasználói szintre),
2. létrehozza a `.venv`-et és letölti a csomagokat (NVIDIA GPU nélkül a CUDA-csomagok kimaradnak),
3. **asztali ikont** (AI HUB) és Start menü-bejegyzéseket készít,
4. beállítja az indítást bejelentkezéskor, és beköti a Claude Code-ba (ha telepítve van).

**Forrásból:** klónozd a repót (pl. `C:\HUB`-ba), és kattints duplán a `Telepites.cmd`-re.

Kézi bekötés Claude Code-ba: `claude mcp add --scope user --transport http hub http://127.0.0.1:8765/mcp`

## Frissítés

- **Tálcaikon → „Frissítés keresése…”**, vagy **Start menü → AI HUB frissítése**, vagy `Frissites.cmd`.
- A hub indításkor naponta egyszer magától is ellenőriz, és értesít, ha van új verzió (magától nem frissít).
- A frissítő a GitHub-kiadásokból dolgozik; **megtartja** a beállításokat (`config.json`), a naplót (`logs`) és a
  kimeneteket (`out`), csak a megváltozott csomagokat telepíti újra, majd újraindítja a hubot.
- Privát repóból frissítéshez GitHub-azonosítás kell (`gh auth login`, vagy `GITHUB_TOKEN`).
- Fejlesztői (git) példányon a frissítés: `git pull`.

## Használat

- **Asztali ikon:** elindítja a hubot; ha már fut, megnyitja a folyamatjelzőt.
- Claude-ban: „nézd meg a hub_list-et”, majd pl. „írd le ezt a videót”, „tedd PDF-be ezt a Word-fájlt”,
  „figyeld ezt a mappát”, „csinálj Anki-paklit a jegyzetből”.
- Új plugin: `hub_new_plugin` → kód + `tests/cases.json` → `hub_test` → `hub_enable` (részletek: `hub-spec.md`).

## Biztonság

- Csak `127.0.0.1`-en figyel, kívülről nem érhető el.
- Szintek: `read` / `act` / `danger` – a `danger` eszközök csak Windows-értesítéses jóváhagyással futnak.
- Titkok csak a Windows Hitelesítőadat-kezelőben (`keyring`), soha nem a kódban vagy a naplóban.

## Licenc

MIT – lásd [`LICENSE`](LICENSE). A használt külső csomagok licencei: [`THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md)
(a repó és a telepítő nem tartalmaz harmadik féltől származó kódot; a csomagokat a telepítő a PyPI-ról tölti le).
