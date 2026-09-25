"""ClassIsland schedule bridge monitor (CIB 2.0 protocol).

Connects to the ClassIsland.WSBridge executable (``ws://localhost:6614/``) and
turns its messages into Qt signals for the main window.

**CIB 2.0** (see ``classisland-ws-bridge/Release/v2.0/README.md``) pushes events
as JSON::

    {"type": "event", "eventName": "OnBreakingTimeNotifyId"}
    {"type": "event", "eventName": "OnClassNotifyId"}

and answers commands such as ``{"action": "get_properties", "keys": [...]}``
with ``{"type": "properties", "data": {...}}``.  The legacy v1 plain-text
messages (``BreakingTime|数学`` / ``OnClass|None``) are still understood so an
older bridge installation keeps working.

This module runs inside a QThread so it never blocks the Qt event loop.

After *MAX_CONSECUTIVE_FAILURES* consecutive reconnect failures the monitor
gives up and emits ``fallback_needed`` so the main window can switch back to a
locally stored timetable.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

import websockets
from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger("kg.client.classisland")

#: Default bridge endpoint — CIB 2.0 serves the root path.
DEFAULT_BRIDGE_URL = "ws://localhost:6614/"

# How long to wait before the first reconnect attempt.
_INITIAL_RECONNECT_DELAY = 2.0
# Maximum back-off delay between reconnect attempts.
_MAX_RECONNECT_DELAY = 60.0
# Number of consecutive failures before giving up.
_MAX_CONSECUTIVE_FAILURES = 5
# Timeout for the auxiliary "what is the next subject?" query.
_PROPERTY_QUERY_TIMEOUT = 2.0

_EVENT_BREAK = "OnBreakingTimeNotifyId"
_EVENT_CLASS = "OnClassNotifyId"
_UNKNOWN_SUBJECT = "未知科目"


class ClassIslandMonitor(QThread):
    """Persistent WebSocket connection to the ClassIsland bridge.

    Signals
    -------
    break_started : str
        Emitted when a break begins.  Carries the name of the next subject
        (or ``未知科目`` when it cannot be resolved).
    class_started :
        Emitted when a class period begins.
    connection_changed : bool, str
        Emitted when the connection state changes (connected, status text).
    error_occurred : str
        Emitted on non-fatal errors (connection lost, parse errors, …).
    fallback_needed :
        Emitted after *MAX_CONSECUTIVE_FAILURES* consecutive reconnect
        failures.  The main window should switch to a local timetable.
        The monitor stops itself before emitting this signal.
    """

    break_started = pyqtSignal(str)
    class_started = pyqtSignal()
    connection_changed = pyqtSignal(bool, str)
    error_occurred = pyqtSignal(str)
    fallback_needed = pyqtSignal()

    def __init__(
        self,
        ws_url: str = DEFAULT_BRIDGE_URL,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._ws_url = ws_url
        self._running = False
        self._in_break = False
        self._consecutive_failures = 0
        #: Last properties snapshot received from the bridge (may be empty).
        self._last_properties: Dict[str, Any] = {}

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

    @property
    def last_properties(self) -> Dict[str, Any]:
        return dict(self._last_properties)

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
                    max_size=2**20,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    open_timeout=5,
                ) as websocket:
                    self.connection_changed.emit(True, "ClassIsland 已连接")
                    self._consecutive_failures = 0
                    delay = _INITIAL_RECONNECT_DELAY
                    await self._read_loop(websocket)
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
                        f"ClassIsland 桥接器连续 {self._consecutive_failures} 次连接失败，已自动回退至本地时间表。"
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

    async def _read_loop(self, websocket) -> None:
        """Read messages until the connection closes.

        A plain ``while``/``recv`` loop (instead of ``async for``) is used so
        the handler may issue a follow-up request and await its reply without
        two coroutines competing for the same socket.
        """
        while self._running:
            raw = await websocket.recv()
            if not self._running:
                break
            text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
            text = text.strip()
            if not text:
                continue
            await self._dispatch(websocket, text)

    async def _dispatch(self, websocket, text: str) -> None:
        """Handle one message from the bridge (CIB 2.0 JSON or legacy text)."""
        if text.startswith("{"):
            await self._dispatch_json(websocket, text)
            return
        # ---- legacy v1 plain-text protocol -------------------------------
        if "|" not in text:
            logger.debug("ClassIsland bridge sent unknown message: %r", text)
            return
        kind, payload = text.split("|", 1)
        kind = kind.strip()
        if kind == "BreakingTime":
            self._in_break = True
            subject = payload.strip() or _UNKNOWN_SUBJECT
            logger.info("ClassIsland: break started (v1), next subject=%s", subject)
            self.break_started.emit(subject)
        elif kind == "OnClass":
            self._in_break = False
            logger.info("ClassIsland: class started (v1)")
            self.class_started.emit()
        else:
            logger.debug("ClassIsland bridge sent unknown event: %r", text)

    async def _dispatch_json(self, websocket, text: str) -> None:
        """Handle a CIB 2.0 JSON message."""
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("ClassIsland bridge sent malformed JSON: %r", text[:160])
            return
        if not isinstance(message, dict):
            return

        message_type = str(message.get("type") or "")

        if message_type == "event":
            event_name = str(message.get("eventName") or "").strip()
            await self._handle_event(websocket, event_name)
            return

        if message_type == "properties":
            data = message.get("data")
            if isinstance(data, dict):
                self._last_properties = data
            return

        if message_type == "error":
            logger.warning("ClassIsland bridge error: %s", message.get("message"))
            return

        logger.debug("ClassIsland bridge sent unhandled message type=%r", message_type)

    async def _handle_event(self, websocket, event_name: str) -> None:
        if event_name == _EVENT_BREAK:
            self._in_break = True
            subject = await self._query_next_subject(websocket)
            logger.info("ClassIsland: break started, next subject=%s", subject)
            self.break_started.emit(subject)
            return

        if event_name == _EVENT_CLASS:
            self._in_break = False
            logger.info("ClassIsland: class started")
            self.class_started.emit()
            return

        logger.debug("ClassIsland bridge sent unknown event %r", event_name)

    async def _query_next_subject(self, websocket) -> str:
        """Ask the bridge for ``NextClassSubject`` (best effort)."""
        try:
            await websocket.send(
                json.dumps({"action": "get_properties", "keys": ["NextClassSubject"]})
            )
            raw = await asyncio.wait_for(websocket.recv(), timeout=_PROPERTY_QUERY_TIMEOUT)
            payload = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
            if isinstance(payload, dict) and payload.get("type") == "properties":
                data = payload.get("data")
                if isinstance(data, dict):
                    self._last_properties = data
                    subject = data.get("NextClassSubject")
                    if isinstance(subject, str) and subject.strip():
                        return subject.strip()
        except Exception as exc:
            logger.debug("Could not query NextClassSubject: %s", exc)
        return _UNKNOWN_SUBJECT
