"""Csomagtelepítő a telepítőnek és a frissítőnek: python deps.py [--force]

A requirements.txt + minden plugin manifestjének deps listája → pip install a .venv-be.
NVIDIA GPU nélkül kihagyja az nvidia-* (CUDA) csomagokat (~1 GB) – a leirat akkor CPU-n fut.
Ha a csomaglista nem változott az előző futás óta (.venv\\deps.hash), nem csinál semmit.
"""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HASH = ROOT / ".venv" / "deps.hash"


def has_nvidia() -> bool:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return False
    try:
        return subprocess.run([exe, "-L"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def wanted() -> list[str]:
    pkgs = [l.strip() for l in (ROOT / "requirements.txt").read_text("utf-8").splitlines() if l.strip() and not l.startswith("#")]
    for mf in sorted((ROOT / "plugins").glob("*/manifest.json")):
        try:
            pkgs += json.loads(mf.read_text("utf-8")).get("deps", [])
        except ValueError:
            print(f"hibás manifest, kihagyva: {mf}")
    gpu = has_nvidia()
    out = []
    for p in dict.fromkeys(pkgs):
        if p.lower().startswith("nvidia-") and not gpu:
            continue
        out.append(p)
    return out


def main() -> int:
    pkgs = wanted()
    digest = hashlib.sha256("\n".join(sorted(pkgs)).encode()).hexdigest()
    if "--force" not in sys.argv and HASH.exists() and HASH.read_text().strip() == digest:
        print("A csomagok naprakészek.")
        return 0
    print(f"Csomagok telepítése ({len(pkgs)} db)…")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade-strategy",
                        "only-if-needed", *pkgs])
    if r.returncode == 0:
        HASH.write_text(digest)
        print("Csomagok rendben.")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
