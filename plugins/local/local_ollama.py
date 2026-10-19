"""Közös Ollama-hívások (a plugin és a háttér-összefoglaló is ezt használja)."""
import re

import httpx

URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:8b"   # 5,2 GB – a 6 GB-os RTX 4050-en jól fut
KEEP_ALIVE = "15m"           # ennyi ideig marad betöltve a modell a hívások között


def chat(prompt: str, model: str = DEFAULT_MODEL, system: str = "", max_tokens: int = 800,
         temperature: float = 0.3, timeout: float = 110, num_ctx: int = 8192) -> dict:
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": model, "messages": msgs, "stream": False, "think": False, "keep_alive": KEEP_ALIVE,
            "options": {"num_predict": max_tokens, "temperature": temperature, "num_ctx": num_ctx}}
    r = httpx.post(f"{URL}/api/chat", json=body, timeout=timeout)
    if r.status_code == 404:
        raise LookupError(f"nincs ilyen modell: {model}")
    r.raise_for_status()
    j = r.json()
    text = re.sub(r"<think>.*?</think>\s*", "", j.get("message", {}).get("content", ""), flags=re.S).strip()
    tps = j["eval_count"] / (j["eval_duration"] / 1e9) if j.get("eval_duration") else None
    return {"text": text, "tokens": j.get("eval_count"), "tok_s": round(tps, 1) if tps else None,
            "seconds": round(j.get("total_duration", 0) / 1e9, 1)}


def models() -> list[dict]:
    r = httpx.get(f"{URL}/api/tags", timeout=5)
    r.raise_for_status()
    return r.json().get("models", [])
