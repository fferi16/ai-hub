"""{{name}} – {{description}}

Szabályok:
- minden eszköz @tool-lal dekorált, típusos paraméterű függvény, egysoros docstringgel
- az eszköz neve "{{prefix}}"-lal kezdődik, és szerepel a manifest.json-ban a szintjével
- kimenet: tömör dict, {"ok": true/false, ...}; hibánál {"ok": false, "reason": "..."}
- titok csak a secret("kulcs") hívással, soha nem a kódban
"""
from hubkit import secret, tool  # noqa: F401


@tool
def {{prefix}}example(text: str) -> dict:
    """Példa eszköz: visszaadja a szöveget (cseréld le)."""
    return {"ok": True, "text": text}
