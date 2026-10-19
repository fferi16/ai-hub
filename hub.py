"""Hub – Claude helyi MCP-központja (streamable HTTP, csak 127.0.0.1)."""
import json
import logging
import os
import socket
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import NotificationOptions

import approval
import hub_tools
import tray
from core import Hub

log = logging.getLogger("hub")


def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text("utf-8"))


def setup_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    logging.basicConfig(
        filename=ROOT / "logs" / "hub.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        encoding="utf-8",
    )


def port_busy(host: str, port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex((host, port)) == 0


def build_server(cfg: dict) -> FastMCP:
    mcp = FastMCP("hub", host=cfg["host"], port=cfg["port"], streamable_http_path=cfg["path"])
    # A kliens kapjon értesítést, ha az eszközlista változik (hot reload).
    low = mcp._mcp_server
    orig = low.create_initialization_options
    low.create_initialization_options = lambda n=None, e=None: orig(n or NotificationOptions(tools_changed=True), e)
    return mcp


def run_http(mcp: FastMCP, cfg: dict) -> uvicorn.Server:
    config = uvicorn.Config(
        mcp.streamable_http_app(), host=cfg["host"], port=cfg["port"], log_level="warning", log_config=None
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, name="http", daemon=True).start()
    return server


def main() -> None:
    setup_logging()
    cfg = load_config()
    if cfg["host"] != "127.0.0.1":
        raise SystemExit("A Hub csak 127.0.0.1-en futhat.")
    if port_busy(cfg["host"], cfg["port"]):
        log.error("A %s port foglalt – már fut egy Hub?", cfg["port"])
        raise SystemExit(1)
    mcp = build_server(cfg)
    hub = Hub(ROOT, cfg, mcp)
    hub.approve = approval.ask
    hub_tools.install(hub)
    hub.load_enabled()
    run_http(mcp, cfg)
    log.info("Hub elindult: http://%s:%s%s", cfg["host"], cfg["port"], cfg["path"])
    tray.start_hotkey()
    import updater  # naponta egyszer: van-e új verzió (csak értesít, nem frissít magától)
    threading.Thread(target=updater.background_check, name="update-check", daemon=True).start()

    def status() -> str:
        return f"AI HUB v{updater.current()} – :{cfg['port']}, {len(hub.loaded)} plugin aktív"

    try:
        tray.run_tray(status, lambda: os.startfile(ROOT / "logs"))
    except Exception:
        log.exception("Tálcaikon nem indult, ikon nélkül fut tovább")
        threading.Event().wait()


if __name__ == "__main__":
    main()
