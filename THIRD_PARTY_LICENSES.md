# Harmadik fél komponensei és licenceik

Az AI HUB **saját kódja MIT-licencű** (lásd `LICENSE`). A repó és a telepítő (`AI-HUB-Setup-*.exe`)
**nem tartalmaz harmadik féltől származó kódot vagy bináris fájlt**: a lenti csomagokat a telepítő
a felhasználó gépén, a hivatalos forrásból (PyPI) tölti le, változtatás nélkül. Mindegyikre a saját licence vonatkozik.

| Csomag | Mire kell | Licenc |
|---|---|---|
| mcp | MCP-szerver | MIT |
| uvicorn | HTTP-szerver | BSD-3-Clause |
| httpx | HTTP-kliens | BSD-3-Clause |
| pystray | tálcaikon | LGPL-3.0 (módosítás nélkül, külön csomagként használva) |
| Pillow | képek, ikon | MIT-CMU (HPND) |
| keyring | titkok a Windows Hitelesítőadat-kezelőben | MIT |
| windows-toasts | Windows-értesítések | Apache-2.0 |
| playwright | `look` – böngészővezérlés | Apache-2.0 |
| pywinauto | `look` – Windows-ablakok | BSD-3-Clause |
| mss | `look` – képernyőkép | MIT |
| winrt-* | `look` – Windows OCR | MIT |
| faster-whisper | `media` – beszédfelismerés | MIT |
| ctranslate2 (a faster-whisper függősége) | `media` – modellfuttatás | MIT |
| imageio-ffmpeg | `media` – hang/kép kinyerés (FFmpeg binárist tölt le) | BSD-2-Clause; az FFmpeg saját (LGPL/GPL) licencű |
| nvidia-cublas-cu12, nvidia-cudnn-cu12 | `media` – GPU-gyorsítás (csak NVIDIA GPU esetén települ) | NVIDIA szabadalmaztatott licenc (NVIDIA Software License) |
| openpyxl | `office` – Excel | MIT |
| pywin32 | `office` – Office COM | PSF |
| pdfplumber, pdfminer.six | `pdf` – szöveg, táblázatok | MIT |
| pypdfium2 (PDFium) | `pdf` – oldal képként | Apache-2.0 / BSD-3-Clause |
| genanki | `study` – Anki-pakli | MIT |
| markitdown (+ mammoth, python-pptx, pandas, xlrd, xlsxwriter, magika, onnxruntime, lxml, beautifulsoup4) | `docs` – dokumentum → Markdown | MIT / BSD / Apache-2.0 |

## Modellek és külső programok (nem részei a repónak)
- **Whisper large-v3** (OpenAI), a faster-whisper első használatkor tölti le a Hugging Face-ről – MIT.
- **Ollama** és a helyi modellek (pl. Qwen3 – Apache-2.0, Llama 3.1 – Llama Community License): külön telepítendők, a saját licencük szerint.
- **Microsoft Office** (Word/Excel/PowerPoint) – az `office` plugin a gépen lévő, licencelt Office-t vezérli.
- A telepítő az **Inno Setup**-pal készül (ingyenes, kereskedelmi célra is használható; a kimenetre nem terjed ki licenckötelezettség).

## Tesztfájlok
A `plugins/*/tests` mintafájljait (PDF, Word, Excel, rövid videó) a projekt maga állította elő.
A minta-PDF a Windows Arial betűtípusának beágyazott részhalmazát tartalmazza; ezt a betűtípus beágyazási engedélye megengedi.
A `media` teszt 5 másodperces hangja a Windows beépített beszédszintetizátorával készült (egy angol tesztmondat).
