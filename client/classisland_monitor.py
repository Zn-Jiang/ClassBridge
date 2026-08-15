"""ClassIsland schedule bridge monitor.

Connects to the ClassIsland WS bridge (``ws://localhost:6614/status``) which
translates ClassIsland IPC events into WebSocket messages.  The bridge is a
standalone .NET executable whose source lives in ``other/program.cs``.

Message format (text, pipe-delimited)::

    BreakingTime|<nextSubject>   – a break / dismissal just started
    OnClass|None                 – class just started

This module runs inside a QThread so it never blocks the Qt event loop.

After *MAX_CONSECUTIVE_FAILURES* consecutive reconnect failures the monitor
gives up and emits ``fallback_needed`` so the main window can switch back to a
JSON schedule source.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import websockets
from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger("kg.client.classisland")

# How long to wait before the first reconnect attempt.
_INITIAL_RECONNECT_DELAY = 2.0
# Maximum back-off delay between reconnect attempts.
_MAX_RECONNECT_DELAY = 60.0
# Number of consecutive failures before giving up.
_MAX_CONSECUTIVE_FAILURES = 5


class ClassIslandMonitor(QThread):
    """Persistent WebSocket connection to the ClassIsland IPC bridge.

    Signals
    -------
    break_started : str
        Emitted when a break begins.  Carries the name of the next subject.
    class_started :
        Emitted when a class period begins.
    connection_changed : bool, str
        Emitted when the connection state changes (connected, status text).
    error_occurred : str
        Emitted on non-fatal errors (connection lost, parse errors, …).
    fallback_needed :
        Emitted after *MAX_CONSECUTIVE_FAILURES* consecutive reconnect
        failures.  The main window should switch to a JSON schedule source.
        The monitor stops itself before emitting this signal.
    """

    break_started = pyqtSignal(str)
    class_started = pyqtSignal()
    connection_changed = pyqtSignal(bool, str)
    error_occurred = pyqtSignal(str)
    fallback_needed = pyqtSignal()

    def __init__(
        self,
        ws_url: str = "ws://localhost:6614/status",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._ws_url = ws_url
        self._running = False
        self._in_break = False
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def set_ws_url(self, url: str) -> None:
        """Update the bridge URL.  Takes effect on the next reconnect."""
        self._ws_url = url

    @property
    def is_in_break(self) -> bool:
        return self._in_break

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    # ------------------------------------------------------------------
    # QThread lifecycle
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Request a graceful shutdown."""
        self._running = False

    def run(self) -> None:
        self._running = True
        self._consecutive_failures = 0
        asyncio.run(self._main())

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    async def _main(self) -> None:
        delay = _INITIAL_RECONNECT_DELAY

        while self._running:
            try:
                self.connection_changed.emit(False, "正在连接 ClassIsland 桥接器...")
                async with websockets.connect(
                    self._ws_url,
                    max_size=2**16,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    self.connection_changed.emit(True, "ClassIsland 已连接")
                    self._consecutive_failures = 0
                    delay = _INITIAL_RECONNECT_DELAY
                    await self._read_loop(ws)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._running:
                    break

                self._consecutive_failures += 1
                remaining = _MAX_CONSECUTIVE_FAILURES - self._consecutive_failures

                if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        "ClassIsland: %d consecutive failures, giving up.",
                        self._consecutive_failures,
                    )
                    self.connection_changed.emit(False, "ClassIsland 已放弃重连")
                    self.error_occurred.emit(
                        f"ClassIsland 桥接器连续 {self._consecutive_failures} 次连接失败，已自动回退至 JSON 时间表。"
                    )
                    self._running = False
                    self.fallback_needed.emit()
                    return

                self.connection_changed.emit(False, "ClassIsland 离线")
                self.error_occurred.emit(
                    f"ClassIsland 桥接器连接失败（{self._consecutive_failures}/{_MAX_CONSECUTIVE_FAILURES}），"
                    f"剩余 {remaining} 次尝试，{delay:.0f} 秒后重试：{exc}"
                )
                await asyncio.sleep(delay)
                delay = min(_MAX_RECONNECT_DELAY, delay * 2)

    async def _read_loop(self, ws) -> None:
        """Read text frames from the bridge until the connection closes."""
        async for raw in ws:
            if not self._running:
                break
            text = raw.strip() if isinstance(raw, str) else ""
            if not text:
                continue
            self._dispatch(text)

    def _dispatch(self, text: str) -> None:
        """Parse a pipe-delimited message and emit the matching signal."""
        if "|" not in text:
            logger.warning("ClassIsland bridge sent malformed message: %r", text)
            return

        kind, payload = text.split("|", 1)
        kind = kind.strip()

        if kind == "BreakingTime":
            self._in_break = True
            subject = payload.strip() if payload.strip() else "未知科目"
            logger.info("ClassIsland: break started, next subject=%s", subject)
            self.break_started.emit(subject)

        elif kind == "OnClass":
            self._in_break = False
            logger.info("ClassIsland: class started")
            self.class_started.emit()

        else:
            logger.debug("ClassIsland bridge sent unknown event: %r", text)
