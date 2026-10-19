"""_probe – próba-eszközök a Hub ellenőrzéséhez."""
from hubkit import tool


@tool
def probe_echo(text: str) -> dict:
    """Visszaadja a kapott szöveget."""
    return {"ok": True, "text": text}


@tool
def probe_big(n: int = 10000) -> dict:
    """n karakteres szöveget ad vissza (a levágás teszteléséhez)."""
    return {"ok": True, "text": ("0123456789" * (n // 10 + 1))[:n]}


@tool
def probe_danger(note: str = "") -> dict:
    """Danger szintű próba: nem csinál semmit, csak jóváhagyást kér."""
    return {"ok": True, "did": "semmi", "note": note}
