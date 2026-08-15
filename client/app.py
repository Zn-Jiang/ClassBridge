import argparse
import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication
from qfluentwidgets import setThemeColor

from shared.config import load_client_config
from shared.logging_utils import configure_logging
from shared.paths import ensure_client_appdata_dir

from .main_window import MainWindow

_CLIENT_DIR = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Class desktop client")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Initialize config and logging, then exit immediately.",
    )
    args = parser.parse_args()

    ensure_client_appdata_dir()
    config = load_client_config()
    logger = configure_logging("kg.client", "client.log", config.log_level)
    logger.info("Client bootstrap complete")
    logger.info("Configured WebSocket target is %s", config.resolved_client_ws_url())

    if args.smoke_test:
        logger.info("Client smoke test passed")
        return 0

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    setThemeColor("#0f766e")

    window = MainWindow(config)
    window.show()
    return app.exec()
