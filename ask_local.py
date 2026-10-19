#!/usr/bin/env python3
"""ask_local.py – Claude Code ezzel kérdezi a helyi Ollama-modelleket.

Csak a Python standard könyvtárát használja (nincs telepítendő csomag).

Példák:
  python ask_local.py "Fordítsd magyarra: Dobrý deň"
  python ask_local.py "Van hibaüzenet a képen?" --image snap.png --model vl
  python ask_local.py "Mik a tűzvédelmi követelmények?" --file spec.txt
  type log.txt | python ask_local.py "Melyik sorban van az első hiba?"
  python ask_local.py --list

Kimenet: mindig egy tömör JSON sor (stdout).
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

# --- Beállítások (felülírhatók környezeti változóval vagy config.json-nal) ---
DEFAULTS = {
    "ollama_url": "http://localhost:11434",
    "text_model": "",        # pl. "qwen3:30b" – ha üres, az első telepített modell
    "vl_model": "",          # a 30B VL modell neve az Ollamában
    "chunk_chars": 12000,    # nagy fájl darabolása ennyi karakterenként
    "max_out_chars": 4000,   # ennél hosszabb válasz levágva + fájlba mentve
    "timeout_s": 600,
    "keep_alive": "15m",
}
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".ask_local.json")


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:  # rossz config ne állítsa le
            print(json.dumps({"ok": False, "error": f"config hiba: {e}"}, ensure_ascii=False))
            sys.exit(1)
    for key in cfg:
        env = os.environ.get("ASK_LOCAL_" + key.upper())
        if env:
            cfg[key] = int(env) if isinstance(DEFAULTS[key], int) else env
    return cfg


def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def http(cfg, path, payload=None):
    url = cfg["ollama_url"].rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=cfg["timeout_s"]) as r:
        return json.loads(r.read().decode("utf-8"))


def list_models(cfg):
    return [m["name"] for m in http(cfg, "/api/tags").get("models", [])]


def pick_model(cfg, want, has_image):
    models = list_models(cfg)
    if not models:
        raise RuntimeError("nincs telepített Ollama-modell")
    kind = want if want in ("vl", "text") else ("vl" if has_image else "text")
    if want in ("vl", "text") or not want:
        want = cfg["vl_model"] if kind == "vl" else cfg["text_model"]
    if not want:
        # nincs beállítva: a név alapján választ (vision vagy szöveges modell)
        vision_tags = ("vl", "vision", "llava", "moondream", "minicpm-v", "gemma3")
        vis = [m for m in models if any(t in m.lower() for t in vision_tags)]
        txt = [m for m in models if m not in vis]
        pool = (vis or models) if kind == "vl" else (txt or models)
        return pool[0]
    if want in models:
        return want
    # részleges egyezés: "qwen3-vl" -> "qwen3-vl:30b-a3b"
    hits = [m for m in models if want.lower() in m.lower()]
    if hits:
        return hits[0]
    raise RuntimeError(f"a modell nem található: {want}; telepítve: {models}")


def chat(cfg, model, prompt, system=None, images=None):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    user = {"role": "user", "content": prompt}
    if images:
        user["images"] = images
    msgs.append(user)
    res = http(cfg, "/api/chat", {
        "model": model,
        "messages": msgs,
        "stream": False,
        "keep_alive": cfg["keep_alive"],
    })
    return res.get("message", {}).get("content", "").strip()


def read_text(path):
    for enc in ("utf-8", "utf-8-sig", "cp1250", "latin-1"):
        try:
            with open(path, encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"nem olvasható szövegként: {path}")


def chunks(text, size):
    # bekezdéshatáron vág, ha lehet
    i = 0
    while i < len(text):
        end = min(i + size, len(text))
        if end < len(text):
            cut = text.rfind("\n", i + size // 2, end)
            if cut > i:
                end = cut
        yield text[i:end]
        i = end


def ask_over_text(cfg, model, question, text, system):
    """Nagy szöveg: darabonként kérdez, a végén összesít (map-reduce)."""
    parts = list(chunks(text, cfg["chunk_chars"]))
    if len(parts) == 1:
        return chat(cfg, model, f"{question}\n\n--- SZÖVEG ---\n{parts[0]}", system), 1
    notes = []
    for n, part in enumerate(parts, 1):
        ans = chat(cfg, model,
                   f"Kérdés: {question}\n\nEz egy hosszú szöveg {n}/{len(parts)}. része. "
                   "Írd ki tömören, ami ebben a részben a kérdéshez tartozik "
                   "(szó szerinti kulcsadatok, számok, oldal/sor ha látszik). "
                   "Ha semmi, válaszolj csak ennyit: NINCS\n\n--- RÉSZ ---\n" + part,
                   system)
        if ans and ans.strip().upper() != "NINCS":
            notes.append(f"[{n}. rész] {ans}")
    if not notes:
        return "A szövegben nincs a kérdéshez tartozó információ.", len(parts)
    final = chat(cfg, model,
                 f"Kérdés: {question}\n\nEzek a jegyzetek egy hosszú szöveg részeiből készültek. "
                 "Ezek alapján válaszolj tömören; ha ellentmondás van, jelezd.\n\n" + "\n".join(notes),
                 system)
    return final, len(parts)


def save_long(text):
    folder = os.path.join(os.path.expanduser("~"), ".ask_local_out")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, time.strftime("%Y%m%d_%H%M%S") + ".txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Kérdés a helyi Ollama-modellnek, JSON válasz.")
    p.add_argument("prompt", nargs="?", help="a kérdés / feladat")
    p.add_argument("--file", action="append", default=[], help="szövegfájl (többször is megadható)")
    p.add_argument("--image", action="append", default=[], help="kép a VL-modellnek (többször is)")
    p.add_argument("--model", default="", help="modellnév, vagy 'vl' / 'text'")
    p.add_argument("--system", default="", help="rendszerutasítás")
    p.add_argument("--list", action="store_true", help="telepített modellek listája")
    a = p.parse_args()
    cfg = load_config()
    t0 = time.time()

    try:
        if a.list:
            out({"ok": True, "models": list_models(cfg)})
            return

        stdin_text = ""
        if not sys.stdin.isatty():
            stdin_text = sys.stdin.read()
        if not a.prompt:
            out({"ok": False, "error": "hiányzik a kérdés"})
            sys.exit(2)

        images = []
        for path in a.image:
            with open(path, "rb") as f:
                images.append(base64.b64encode(f.read()).decode("ascii"))

        model = pick_model(cfg, a.model, bool(images))
        texts = [read_text(f) for f in a.file]
        if stdin_text.strip():
            texts.append(stdin_text)
        body = "\n\n".join(texts)

        if body:
            answer, n_chunks = ask_over_text(cfg, model, a.prompt, body, a.system or None)
        else:
            answer, n_chunks = chat(cfg, model, a.prompt, a.system or None, images or None), 0

        res = {"ok": True, "model": model, "answer": answer,
               "seconds": round(time.time() - t0, 1)}
        if n_chunks > 1:
            res["chunks"] = n_chunks
        if len(answer) > cfg["max_out_chars"]:
            res["answer"] = answer[: cfg["max_out_chars"]] + " …[levágva]"
            res["full_answer_file"] = save_long(answer)
        out(res)

    except urllib.error.URLError as e:
        out({"ok": False, "error": f"az Ollama nem érhető el ({cfg['ollama_url']}): {e.reason}",
             "hint": "indítsd el: ollama serve"})
        sys.exit(1)
    except Exception as e:
        out({"ok": False, "error": str(e)})
        sys.exit(1)


if __name__ == "__main__":
    main()
