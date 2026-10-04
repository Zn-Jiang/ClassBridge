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


def _install_exception_guard(logger) -> None:
    """把未捕获异常写进日志，而不是让 PyQt 直接终止整个客户端。

    PyQt6 遇到槽函数抛出的未捕获异常时会调用 `qFatal` 结束进程（表现为"点一下
    按钮客户端直接崩了"）。装上自己的 `sys.excepthook` 后，异常会被记录完整堆栈、
    客户端继续运行——教室里突然退出比报错难排查得多。
    """

    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.error(
            "未捕获异常（已拦截，客户端继续运行）",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = _hook


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
    _install_exception_guard(logger)
    logger.info("Client bootstrap complete")
    logger.info("Configured WebSocket target is %s", config.resolved_client_ws_url())

    if args.smoke_test:
        logger.info("Client smoke test passed")
        return 0

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    setThemeColor("#0f766e")

    # Start-up splash: plays client/animation/loading.mp4 and is guaranteed to
    # stay up for at least MIN_VISIBLE_MS so the animation is never cut short.
    splash = None
    try:
        from .splash import SplashScreen, center_on_screen

        splash = SplashScreen()
        center_on_screen(splash)
        splash.show()
        app.processEvents()
        # Show the very first frame, then hold it while the window is built:
        # playback starts *after* the heavy work, so the animation runs from
        # frame 0 without a stall in the middle (Qt widgets cannot live in
        # another thread, so keeping the GUI thread free is the only option).
        splash.prime()
        splash.start_status_sequence()
    except Exception as exc:  # pragma: no cover - splash must never block start-up
        logger.warning("无法显示启动动画（%s），直接启动主程序", exc)
        splash = None

    # Chunked start-up: the window shell is built synchronously, the remaining
    # work is spread over event-loop turns so nothing blocks the GUI thread for
    # long while the splash is on screen.
    window = MainWindow(config, chunked_startup=splash is not None)
    if splash is not None:
        splash.resume_playback()   # play from frame 0, undisturbed by start-up
        # The splash closes itself as soon as start-up is done, but never
        # before its minimum on-screen time (see client/splash.py).
        window.attach_splash(splash)
    # The window decides on its own whether to appear: it only shows up when the
    # client is in a break *and* there are unread messages, otherwise it stays
    # in the tray (see MainWindow.begin_startup).
    window.begin_startup()
    return app.exec()
