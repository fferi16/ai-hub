# `look` – univerzális, költségkímélő képernyő-ellenőrző Claude Code-hoz

## Cél
Parancssori eszköz, amit a Claude Code (Bash-ból) hív meg, hogy **magától megnézzen és kipróbáljon** bármilyen programot vagy weboldalt a Windows laptopon, és tömör eredményt kapjon vissza.
Fő szempont: **minél kevesebb token**. Alapból szöveg, kép csak kérésre.

## Környezet
- Windows 11, Python 3.11+
- Claude Code ugyanezen a gépen fut
- Függőségek: `playwright`, `pywinauto`, `mss` (képernyőkép), `Pillow`, OCR: Windows beépített OCR (`winsdk`) vagy `rapidocr-onnxruntime` – ami helyben, ingyen fut

## Három „szem” (backend), automatikus választással
1. **web** – Playwright: weboldalak, Electron-appok (`--electron <exe>`)
2. **win** – pywinauto (UIA backend): natív Windows-ablakok (Excel, Explorer, saját programok)
3. **ocr** – képernyőkép + helyi OCR: ahol nincs kiolvasható elemfa (játék, CAD-nézet, vászon, távoli asztal)

Választás: URL → web; `--electron` → web; ablakcím/exe → win; ha a win elemfa üres vagy nagyon kevés → ocr.

## Negyedik szint: helyi vision-modell (Ollama, 30B VL)
Mielőtt kép Claude-hoz kerülne, a helyi modell dolgozza fel – ingyen.
- Sorrend: szöveges elemfa → helyi OCR → **helyi VL-modell** → Claude (csak ha muszáj)
- Feladatai: képernyő rövid leírása, „van-e hibaablak / megjelent-e X”, elem megkeresése leírás alapján (koordinátával), két kép összehasonlítása
- Parancs: `look ask "<kérdés>" [--el N | --region ...]` → `{"answer": "...", "confidence": "high|low"}`
- `run` lépésként: `{"do": "ask", "q": "Látszik hibaüzenet?"}`
- Ha `confidence: low`, vagy a kérdés döntő (pl. végső elfogadás), a kimenet jelzi: `"escalate": true` → akkor kér Claude képet
- Beállítás: `%USERPROFILE%\.look\config.json` → `{"vl_model": "<ollama modellnév>", "ollama_url": "http://localhost:11434", "max_image_px": 1024}`
- A modell első betöltése lassú lehet; maradjon betöltve (`keep_alive`) a futás végéig

## Parancsok
```
look open <url | exe | "ablakcím">        # megnyit/csatlakozik, visszaad: elemlista
look run <lépések.json | ->                # sok lépés egy hívásban, a végén egy válasz
look snap [--elem N | --region x,y,w,h] [--max 800]   # kép CSAK kérésre, kicsinyítve, vágva
look recipe save <név> | run <név> | list  # bevált útvonal mentése/futtatása
look close
```

## Lépésformátum (`run`)
```json
[
  {"do": "open",  "target": "https://asztrolab.hu"},
  {"do": "click", "el": 3},
  {"do": "type",  "el": 5, "text": "1989-08-24"},
  {"do": "key",   "keys": "Enter"},
  {"do": "wait",  "for": "text", "value": "Eredmény"},
  {"do": "expect","text": "Kos"},
  {"do": "snap",  "el": 7}
]
```
Elemhivatkozás a számozott elemlista szerint (`el`), vagy `"name": "Export"` / `"role": "button"`.

## Kimenet (mindig JSON, tömör)
```json
{
  "ok": true,
  "backend": "web",
  "window": "Asztroláb – Születési profil",
  "elements": [[1,"button","Mentés"],[2,"input","Dátum",""],[3,"link","Kártyák"]],
  "changed": ["új dialógus: 'Exportálás kész'"],
  "errors": ["console: TypeError ... (line 42)"],
  "expect": [{"text":"Kos","found":true}],
  "image": "C:/look/snaps/2026-10-13_1830_el7.png"
}
```
Szabályok a tokentakarékossághoz:
- `elements`: csak interaktív és hibaüzenet-elemek, max ~60 sor, rövid nevek (levágva 40 karakterre)
- `run` után alapból csak a **változást** (`changed`) adja vissza, a teljes listát csak `--full` kapcsolóval
- kép csak `snap` lépésre, alapból max 800 px széles, JPEG/PNG legkisebb méret
- `errors`: web esetén konzol- és hálózati hibák, win esetén hibaablakok szövege
- receptfuttatásnál, ha minden `expect` teljesül: csak `{"ok":true,"recipe":"név"}`

## Receptek
- Mappa: `%USERPROFILE%\.look\recipes\<név>.json`
- `recipe save` az utolsó sikeres `run` lépéseit menti el
- Futáskor elemszám helyett név/szerep alapján keres, hogy UI-változás után is működjön

## Számozott képek
`snap` a képre ráírja az elemek számát (kis címke), hogy a képből is számmal lehessen hivatkozni.

## Hibakezelés
- Minden hiba JSON-ban, `"ok": false` + rövid ok + javaslat (pl. „az el 5 nem található, hasonló: 'Dátum mező' (el 6)”)
- Időkorlát lépésenként (alap 10 s), a teljes futásra 120 s

## Ellenőrzés (bemenet → kimenet példákkal)
1. `look open https://example.com` → az elemlistában szerepel a „More information...” link
2. `look open notepad.exe`, `type` „teszt”, `expect` „teszt” → `ok: true`
3. Egy vásznas oldal (pl. egy rajzprogram) → automatikusan `ocr` backend, a kiírt szöveg megjelenik
4. `recipe save` majd `recipe run` → második futás csak `{"ok":true}`
5. Egy `snap` kép mérete < 150 KB

## Claude Code integráció
A projektek `CLAUDE.md`-jébe (vagy egy skillbe) ez kerüljön:
> Felhasználói felület ellenőrzéséhez használd a `look` parancsot. Először mindig szöveges elemlistát kérj; `snap`-et csak akkor, ha a szöveg nem elég. Több lépést egy `run`-ban küldj. Ha egy ellenőrzés ismétlődik, mentsd receptként.
