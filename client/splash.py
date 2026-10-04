"""Start-up splash that plays ``client/animation/loading.mp4``.

The client does a fair amount of work before it can decide whether the main
window should appear at all (config, log, ClassIsland bridge, first snapshot).
This splash covers that window:

* video playback through ``QMediaPlayer`` while the decoded frames are painted
  **manually** into a label (see :meth:`SplashScreen._on_video_frame`);
* a styled fallback (title + status text) when multimedia is unavailable, so
  the client still starts on machines without a media backend;
* a **minimum on-screen time** (:data:`MIN_VISIBLE_MS`, 5s) so the animation is
  never cut off mid-way — the "client is ready" signal only closes the splash
  once both the work is done *and* the minimum has elapsed;
* a hard maximum so a stalled backend can never block start-up forever.

Why manual painting instead of ``QVideoWidget``
-----------------------------------------------
Measured on the developer machine (see ``other/debug_splash_render.py``):
``QVideoWidget`` decoded all 601 frames of this clip while the *composited*
window kept showing a single frozen image — screen captures were byte-identical
for 13 seconds in both a ``SplashScreen``-flagged window and a plain frameless
one.  Handling ``QVideoSink.videoFrameChanged`` and blitting the frames into a
``QLabel`` produced 52 distinct on-screen images in 4 seconds, so the splash
uses that path and keeps Qt Multimedia optional.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPropertyAnimation,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QLabel,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger("kg.client.splash")

#: 启动画面上显示的产品名（与主窗口标题保持一致）
APP_NAME = "家校沟通客户端"
#: 动画至少要显示这么久才允许退出（用户要求“至少播放到第 5 秒”，当前设为 6 秒）
MIN_VISIBLE_MS = 6000
#: 硬上限：媒体后端异常时也不能一直挡住启动
MAX_VISIBLE_MS = 20000
#: Rendering size of the splash video (the clip itself is 1920×1080 / 16:9).
VIDEO_WIDTH = 800
VIDEO_HEIGHT = 450
STATUS_HEIGHT = 30
#: Upper bound on frame conversions per second.  A 1080p frame has to be
#: converted and scaled on the GUI thread (measured ~8ms), so the splash drops
#: frames instead of competing with start-up work.
MAX_FRAME_RATE = 40.0
#: Status texts rotated while the client starts up (cross-faded).
DEFAULT_STATUS_TEXTS = (
    "正在读取配置…",
    "正在校准时间…",
    "正在连接服务器…",
    "正在连接 ClassIsland…",
    "正在测试桥接器可用性…",
    "正在同步历史消息…",
    "即将进入主界面…",
)
#: How long each status text stays on screen.
STATUS_ROTATE_MS = 800
#: Fade duration for the cross-fade (half out, half in).
STATUS_FADE_MS = 240
_ANIMATION_PATH = Path(__file__).resolve().parent / "animation" / "loading.mp4"


class SplashScreen(QWidget):
    """Frameless splash window with the loading animation.

    Signals
    -------
    closed :
        Emitted once, when the splash is actually gone (start-up may proceed).
    """

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None, animation_path: Optional[Path] = None) -> None:
        super().__init__(parent, Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("splash_screen")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setWindowTitle("ClassBridge 客户端 正在启动")
        self._path = Path(animation_path) if animation_path else _ANIMATION_PATH
        self._player = None
        self._sink = None
        self._last_frame_at = 0.0
        self._frames_painted = 0
        self._min_elapsed = False
        self._max_reached = False
        self._pending_close = False
        self._closed_emitted = False

        self._build_ui()
        self._start_media()
        QTimer.singleShot(MIN_VISIBLE_MS, self._on_min_elapsed)
        QTimer.singleShot(MAX_VISIBLE_MS, self._on_max_elapsed)

    # ------------------------------------------------------------------
    # ui
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.video_label = QLabel(self)
        self.video_label.setObjectName("splash_video")
        self.video_label.setFixedSize(VIDEO_WIDTH, VIDEO_HEIGHT)
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setFont(QFont("Microsoft YaHei UI", 15, QFont.Weight.DemiBold))
        self.video_label.setStyleSheet("color: #e2e8f0; background: #0b1220;")
        self.video_label.setText(f"{APP_NAME}\n正在启动…")
        layout.addWidget(self.video_label)

        self.status_label = QLabel("正在启动…", self)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color: #94a3b8; background: #0b1220; font-size: 12px;")
        self.status_label.setFixedHeight(STATUS_HEIGHT)
        layout.addWidget(self.status_label)

        # Cross-fade support for the rotating status texts.
        self._status_effect = QGraphicsOpacityEffect(self.status_label)
        self._status_effect.setOpacity(1.0)
        self.status_label.setGraphicsEffect(self._status_effect)
        self._status_animation: Optional[QPropertyAnimation] = None
        self._status_texts: list = []
        self._status_index = 0
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(STATUS_ROTATE_MS)
        self._status_timer.timeout.connect(self._advance_status)

        self.setFixedSize(VIDEO_WIDTH, VIDEO_HEIGHT + STATUS_HEIGHT)

    def _start_media(self) -> None:
        """Try to play the animation; fall back to the static splash."""
        if not self._path.is_file():
            logger.warning("Splash animation not found at %s; using the static splash", self._path)
            return
        try:
            from PyQt6.QtMultimedia import QMediaPlayer, QVideoSink
        except Exception as exc:  # pragma: no cover - depends on the Qt build
            logger.warning("QtMultimedia unavailable (%s); using the static splash", exc)
            return

        try:
            player = QMediaPlayer(self)
            # No audio sink: the clip is silent and an audio device is not
            # always present on a classroom PC.
            sink = QVideoSink(self)
            player.setVideoSink(sink)
            sink.videoFrameChanged.connect(self._on_video_frame)
            player.errorOccurred.connect(self._on_media_error)
            player.mediaStatusChanged.connect(self._on_media_status)
            player.setSource(QUrl.fromLocalFile(str(self._path)))
            player.play()

            self._player = player
            self._sink = sink
            logger.info("Splash animation started: %s (%dx%d)", self._path.name, VIDEO_WIDTH, VIDEO_HEIGHT)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to start the splash animation (%s); using the static splash", exc)
            self._player = None

    # ------------------------------------------------------------------
    # callbacks
    # ------------------------------------------------------------------

    def _on_video_frame(self, frame) -> None:
        """Paint a decoded frame into the label (throttled)."""
        now = time.monotonic()
        if now - self._last_frame_at < 1.0 / MAX_FRAME_RATE:
            return
        self._last_frame_at = now
        image = frame.toImage()
        if image.isNull():
            return
        pixmap = QPixmap.fromImage(
            image.scaled(
                VIDEO_WIDTH,
                VIDEO_HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.video_label.setPixmap(pixmap)
        if self._frames_painted == 0:
            logger.info("Splash first frame painted")
        self._frames_painted += 1

    def _on_media_error(self, error, message: str = "") -> None:
        logger.warning("Splash media error (%s): %s", error, message)
        self._show_static_fallback()

    def _on_media_status(self, status) -> None:
        from PyQt6.QtMultimedia import QMediaPlayer

        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            logger.warning("Splash animation could not be decoded; using the static splash")
            self._show_static_fallback()
        elif status == QMediaPlayer.MediaStatus.EndOfMedia and not self._closed_emitted:
            # The clip finished (10s); keep the window until start-up is done,
            # only ensuring the minimum has been honoured.
            logger.info("Splash animation finished")

    def _show_static_fallback(self) -> None:
        """Show the text placeholder instead of video frames."""
        self.video_label.setPixmap(QPixmap())
        self.video_label.setText(f"{APP_NAME}\n正在启动…")

    def _on_min_elapsed(self) -> None:
        self._min_elapsed = True
        if self._pending_close:
            self.close_splash()

    def _on_max_elapsed(self) -> None:
        self._max_reached = True
        logger.warning("Splash reached its maximum on-screen time; closing anyway")
        self._min_elapsed = True
        self.close_splash()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def set_status(self, text: str) -> None:
        """Pin a status text (stops the rotation)."""
        self._status_timer.stop()
        self._status_texts = []
        self._set_status_text(text)

    # -- rotating status texts --------------------------------------------

    def start_status_sequence(self, texts=None) -> None:
        """Rotate through status texts, cross-fading every ``STATUS_ROTATE_MS``."""
        self._status_texts = list(texts or DEFAULT_STATUS_TEXTS)
        if not self._status_texts:
            return
        self._status_index = 0
        self._set_status_text(self._status_texts[0])
        self._status_timer.start(STATUS_ROTATE_MS)

    def _advance_status(self) -> None:
        if not self._status_texts:
            return
        self._status_index = (self._status_index + 1) % len(self._status_texts)
        self._fade_to_status(self._status_texts[self._status_index])

    def _fade_to_status(self, text: str) -> None:
        """Fade the current text out, swap it, fade the new one in."""
        effect = self._status_effect
        midpoint = max(80, STATUS_FADE_MS // 2)

        out = QPropertyAnimation(effect, b"opacity", self)
        out.setDuration(midpoint)
        out.setStartValue(effect.opacity())
        out.setEndValue(0.0)

        def _swap() -> None:
            self.status_label.setText(text)
            incoming = QPropertyAnimation(effect, b"opacity", self)
            incoming.setDuration(midpoint)
            incoming.setStartValue(0.0)
            incoming.setEndValue(1.0)
            incoming.setEasingCurve(QEasingCurve.Type.InOutQuad)
            incoming.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
            self._status_animation = incoming

        out.finished.connect(_swap)
        out.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        self._status_animation = out

    def _set_status_text(self, text: str) -> None:
        self.status_label.setText(text)
        self._status_effect.setOpacity(1.0)

    # -- playback control --------------------------------------------------

    def prime(self, timeout_ms: int = 2000) -> bool:
        """Decode and show the **first** frame, then hold playback at 0.

        Starting playback immediately used to run the clip while the window was
        still being built, so the first frame the user saw was ~0.5 s in.
        Returns whether a frame was painted.
        """
        if self._player is None:
            return False
        self._player.play()
        deadline = time.monotonic() + max(0.2, timeout_ms / 1000.0)
        while time.monotonic() < deadline and self._frames_painted == 0:
            QApplication.processEvents()
            time.sleep(0.01)
        if self._frames_painted:
            # Freeze on the first frame; start_playback() resumes from here.
            self._player.pause()
            self._player.setPosition(0)
            logger.info("Splash primed on its first frame")
        else:
            logger.warning("Splash could not decode its first frame in time")
        return self._frames_painted > 0

    def warm_up(self, milliseconds: int = 400) -> None:
        """Let the animation actually render for a moment.

        The heavy start-up work runs on the GUI thread; giving the event loop a
        few hundred milliseconds first is what makes the *beginning* of the
        animation look smooth instead of stuttering on its first frames.
        """
        deadline = time.monotonic() + max(0.0, milliseconds / 1000.0)
        while time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)

    def pause_playback(self) -> None:
        """Freeze playback (used while a long GUI-thread task runs)."""
        if self._player is not None:
            self._player.pause()

    def resume_playback(self) -> None:
        """Resume playback where it stopped (never skips clip time)."""
        if self._player is not None:
            self._player.play()

    def mark_ready(self) -> None:
        """Start-up work finished — close as soon as the minimum has elapsed."""
        self._pending_close = True
        if self._min_elapsed:
            self.close_splash()

    def close_splash(self) -> None:
        """Close the splash, honouring the minimum on-screen time."""
        if self._closed_emitted:
            return
        if not self._min_elapsed:
            # Too early: remember and let the timer close it (never truncate
            # the animation).
            self._pending_close = True
            logger.debug("Splash close deferred: minimum on-screen time not reached")
            return
        self._closed_emitted = True
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass
        self.hide()
        self.closed.emit()
        self.deleteLater()

    # ------------------------------------------------------------------
    # appearance
    # ------------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#0b1220"))
        painter.end()
        super().paintEvent(event)


def splash_available() -> bool:
    """Whether the animation file exists (used by tests/diagnostics)."""
    return _ANIMATION_PATH.is_file()


def center_on_screen(widget: QWidget) -> None:
    """Move *widget* to the centre of the primary screen."""
    screen = QApplication.primaryScreen()
    if screen is None:
        return
    geometry = screen.availableGeometry()
    widget.move(
        geometry.center().x() - widget.width() // 2,
        geometry.center().y() - widget.height() // 2,
    )


__all__ = ["SplashScreen", "splash_available", "center_on_screen", "MIN_VISIBLE_MS", "MAX_VISIBLE_MS"]
