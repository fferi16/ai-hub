"""A config.json frissítési forrásának beállítása, a formázás megtartásával: python set_repo.py <config.json> <owner/repo>
Csak akkor ír, ha az érték tényleg változik."""
import json
import sys
from pathlib import Path

path, repo = Path(sys.argv[1]), sys.argv[2]
cfg = json.loads(path.read_text("utf-8"))
upd = cfg.setdefault("update", {"check_daily": True})
if upd.get("repo") != repo:
    upd["repo"] = repo
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"frissítési forrás: {repo}")
