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
from typing import Any, Dict, Optional, Tuple

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
#: How often to re-read ``CurrentState`` from the bridge.
#:
#: Events alone are not enough: the client may start *during* a break (never
#: seeing the transition) and an event can be missed while reconnecting.
#: Polling the authoritative state keeps the server's ``is_in_break`` correct.
_STATE_POLL_INTERVAL = 15.0
#: How many consecutive unusable polls before the window is told to stop
#: trusting the bridge for class/break state.
#:
#: The bridge can be perfectly reachable and still be unable to answer: when
#: ClassIsland has no timetable loaded/enabled (``IsClassPlanLoaded=False``)
#: its ``CurrentState`` is ``None`` — the default enum value, *not* "class".
#: Without this the client never learned the state and silently stayed at its
#: initial "in class" value forever.
_STATE_UNAVAILABLE_LIMIT = 2

_EVENT_BREAK = "OnBreakingTimeNotifyId"
_EVENT_CLASS = "OnClassNotifyId"
_UNKNOWN_SUBJECT = "未知科目"

#: ``CurrentState`` 的全部取值（用户提供的权威清单），大小写不敏感精确匹配。
#: 上课 / 准备上课：两者都不是课间，不弹窗。
_STATE_CLASS_VALUES = frozenset({"onclass", "prepareonclass"})
#: 课间休息 / 放学：都算"可以弹窗的下课状态"。
_STATE_BREAK_VALUES = frozenset({"breaking", "afterschool", "breakingtime"})
#: 无：当天没有课表，或当天有课但还没到第一节课（ClassIsland 自己的语义）。
#:
#: 注意：**CIB 与 ClassIsland 断连时并不是返回 ``None``** —— 那种情况下向桥接器
#: 请求**任意属性**都会得到纯文本「ClassIsland 已离线」（见 :data:`OFFLINE_REPLY`
#: 与 :meth:`_query_properties`），是与这里完全不同的信号。
_STATE_NONE_VALUES = frozenset({"none", "null", ""})
#: 旧名，保留给 v1 文本协议与旧版桥接器。
_STATE_BREAK = "BreakingTime"


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
    #: Emitted whenever the authoritative state is (re)read — even when it did
    #: not change.  Lets the window decide its start-up behaviour without
    #: guessing: ``True`` means the client is currently in a break.
    state_synced = pyqtSignal(bool)
    #: Emitted once the bridge keeps being unable to report a real class/break
    #: state (no timetable loaded/enabled, IPC up but idle, …).  The window
    #: must then derive the state from a timetable instead of waiting for the
    #: bridge; a later usable read re-arms the signal.
    state_unavailable = pyqtSignal()
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
        self._url_index = 0
        self._candidates: List[str] = []
        self._running = False
        #: ``stop()`` 早于线程真正启动时置位：``run()`` 必须先看它，
        #: 否则会被 ``run()`` 里的 ``_running = True`` 覆盖，线程永不退出。
        self._stop_requested = False
        self._in_break = False
        self._consecutive_failures = 0
        #: Consecutive polls that could not yield a usable class/break state.
        self._unavailable_streak = 0
        #: Whether ``state_unavailable`` was already emitted (re-armed by a
        #: usable read), so the window is told exactly once per outage.
        self._unavailable_announced = False
        #: Raw probe values, logged whenever they change (diagnostics).
        self._last_probe: Dict[str, Any] = {}
        #: 是否已经就"桥接器回「ClassIsland 已离线」"记过一次 INFO（避免刷日志）。
        self._offline_reported = False
        #: Last properties snapshot received from the bridge (may be empty).
        self._last_properties: Dict[str, Any] = {}
        # Loop/socket handles so stop() can interrupt a blocked recv at once
        # (otherwise a caller waiting on the thread would stall for seconds).
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._websocket = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def set_ws_url(self, url: str) -> None:
        """Update the bridge URL.  Takes effect on the next reconnect."""
        self._ws_url = url
        self._url_index = 0
        self._candidates = []

    def _candidate_urls(self) -> List[str]:
        """连接候选地址：配置/默认地址（6614）优先，其次是 port.txt 里记录的端口。

        新版 CIB 启动成功后会把实际监听端口写进
        ``%APPDATA%/Zn-Jiang/ClassIsland Bridge/port.txt``；端口不一定是 6614，
        因此先试 6614，失败后再读文件里的端口。
        """
        if self._candidates:
            return self._candidates
        try:
            from . import cib_daemon

            candidates = cib_daemon.bridge_url_candidates(self._ws_url)
        except Exception as exc:  # pragma: no cover - 防御性
            logger.debug("解析桥接器候选地址失败：%s", exc)
            candidates = [self._ws_url]
        self._candidates = candidates or [self._ws_url]
        if len(self._candidates) > 1:
            logger.info("桥接器候选地址：%s", "、".join(self._candidates))
        return self._candidates

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
        """Request a graceful shutdown *and* interrupt any blocking wait.

        Without this, a caller doing ``monitor.stop(); monitor.wait(3000)``
        could block for seconds: the reader may be sitting in a bounded
        ``recv`` or in a reconnect back-off.

        Safe to call before the thread has actually started: the request is
        remembered (:attr:`_stop_requested`) so :meth:`run` returns at once
        instead of flipping ``_running`` back to ``True`` and running forever
        (that race used to leave a "retired" monitor alive, never reaped).
        """
        self._stop_requested = True
        self._running = False
        self._interrupt_recv()

    def _interrupt_recv(self) -> None:
        """Close the bridge socket from the GUI thread to unblock ``recv``."""
        loop = self._loop
        websocket = self._websocket
        if loop is None or websocket is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(websocket.close(), loop)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Could not interrupt the bridge socket: %s", exc)

    async def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep in small slices so :meth:`stop` takes effect within ~0.2s."""
        if seconds <= 0:
            return
        loop = asyncio.get_event_loop()
        deadline = loop.time() + seconds
        while self._running and loop.time() < deadline:
            await asyncio.sleep(min(0.2, max(0.0, deadline - loop.time())))

    def run(self) -> None:
        # 线程可能在 stop() 之后才真正启动（切换时间表来源时很常见），
        # 此时必须直接退出，不能把 _running 重新置为 True。
        if self._stop_requested:
            self._running = False
            logger.debug("ClassIsland monitor started after a stop request; exiting")
            return
        self._running = True
        self._consecutive_failures = 0
        asyncio.run(self._main())

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    async def _main(self) -> None:
        delay = _INITIAL_RECONNECT_DELAY
        self._loop = asyncio.get_event_loop()

        while self._running:
            # 候选地址：先试配置/默认（6614），失败后再试 port.txt 里记录的端口
            candidates = self._candidate_urls()
            target = candidates[self._url_index % len(candidates)]
            try:
                self.connection_changed.emit(False, "正在连接 ClassIsland 桥接器...")
                async with websockets.connect(
                    target,
                    max_size=2**20,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    open_timeout=5,
                ) as websocket:
                    self._websocket = websocket
                    if target != self._ws_url:
                        logger.info("桥接器连接成功（回退地址）：%s", target)
                    self.connection_changed.emit(True, "ClassIsland 已连接")
                    self._consecutive_failures = 0
                    delay = _INITIAL_RECONNECT_DELAY
                    await self._read_loop(websocket)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._running:
                    break

                # 连不上就换下一个候选地址（6614 → port.txt 记录的端口）
                if len(candidates) > 1:
                    self._url_index += 1
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
                await self._sleep_interruptible(delay)
                delay = min(_MAX_RECONNECT_DELAY, delay * 2)
            finally:
                self._websocket = None

    async def _read_loop(self, websocket) -> None:
        """Read messages until the connection closes.

        A plain ``while``/``recv`` loop (instead of ``async for``) is used so
        the handler may issue a follow-up request and await its reply without
        two coroutines competing for the same socket.

        ``recv`` is bounded by :data:`_STATE_POLL_INTERVAL`; when nothing
        arrives the loop re-reads the authoritative ``CurrentState`` instead of
        sitting idle.  That covers two gaps events cannot: the client starting
        *during* a break, and events lost while reconnecting.
        """
        await self._sync_state(websocket)          # calibrate right away

        while self._running:
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=_STATE_POLL_INTERVAL)
            except asyncio.TimeoutError:
                await self._sync_state(websocket)
                continue
            if not self._running:
                break
            text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
            text = text.strip()
            if not text:
                continue
            await self._dispatch(websocket, text)

    async def _sync_state(self, websocket) -> None:
        """Re-read the authoritative state; degrade when the bridge cannot tell.

        Emits ``state_synced`` on a usable read (even when the value did not
        change) and ``state_unavailable`` after ``_STATE_UNAVAILABLE_LIMIT``
        consecutive unusable reads — a reachable bridge with no timetable
        loaded reports ``CurrentState=None`` forever, and waiting for it would
        leave the client (and the QQ bot) stuck on "in class" indefinitely.
        """
        state, usable = await self._probe_state(websocket)
        if not usable:
            self._note_state_unavailable()
            return

        self._note_state_usable()
        changed = state != self._in_break
        self._in_break = state
        # Always announce the (re)read state so the window can act on it.
        self.state_synced.emit(state)

        if not changed:
            return
        if state:
            subject = await self._query_next_subject(websocket)
            logger.info("ClassIsland: state synced -> break (next=%s)", subject)
            self.break_started.emit(subject)
        else:
            logger.info("ClassIsland: state synced -> class")
            self.class_started.emit()

    async def _probe_state(self, websocket) -> Tuple[Optional[bool], bool]:
        """Read class/break state, reporting whether it is actually usable.

        ``(state, True)`` means the bridge gave a real answer; ``(None, False)``
        means "no opinion" — either the query failed, the timetable is not
        loaded/enabled, or ``CurrentState`` is unrecognised (``None`` is the
        ClassIsland enum default and must *not* be read as "in class").
        """
        data = await self._query_properties(
            websocket, ["CurrentState", "IsClassPlanEnabled", "IsClassPlanLoaded"]
        )
        if data is None:
            logger.info("State probe failed: the bridge returned no properties")
            return None, False

        probe = {
            key: data.get(key)
            for key in ("CurrentState", "IsClassPlanEnabled", "IsClassPlanLoaded")
        }
        if probe != self._last_probe:
            self._last_probe = dict(probe)
            logger.info("State probe changed: %s", probe)

        # A disabled/unloaded lesson plan makes every lesson property default,
        # so the bridge cannot distinguish class from break — same as "no CIB".
        if data.get("IsClassPlanLoaded") is False or data.get("IsClassPlanEnabled") is False:
            logger.debug(
                "ClassIsland has no active timetable (loaded=%s enabled=%s)",
                data.get("IsClassPlanLoaded"),
                data.get("IsClassPlanEnabled"),
            )
            return None, False

        state = self._map_state_value(
            data.get("CurrentState"), plan_loaded=data.get("IsClassPlanLoaded")
        )
        if state is None:
            logger.debug("Unusable CurrentState value: %r", data.get("CurrentState"))
            return None, False
        return state, True

    @staticmethod
    def _map_state_value(value: Any, *, plan_loaded: Optional[bool] = None) -> Optional[bool]:
        """把 ``CurrentState`` 映射成「是否下课」（``None`` = 无法判断）。

        ClassIsland 会给出的全部取值（用户提供的权威清单）：

        ===================  ==========================================
        ``None``             **无** —— 当天没有课表，或当天有课但还没到第一节课
        ``OnClass``          上课
        ``PrepareOnClass``   准备上课（上课前 n 秒，n 由用户自己设置）
        ``Breaking``         课间休息
        ``AfterSchool``      放学（当天有课表且已经上完）
        ===================  ==========================================

        因此 ``OnClass`` / ``PrepareOnClass`` → 上课；``Breaking`` / ``AfterSchool`` → 下课；
        ``None`` 表示"这一天此刻没有课"（**不是**课外时间，也**不是**断连）→ 返回
        ``None``，交给调用方走降级：导入课表的"非上课时段"本来就把首节课前/末节课后
        算作课间。

        **断连是另一回事**：CIB 与 ClassIsland 的 IPC 断了以后，向桥接器请求任何属性
        都只会拿到纯文本「ClassIsland 已离线」，根本走不到这里（见 :meth:`_query_properties`）。
        """
        if not isinstance(value, str):
            return None
        text = value.strip().lower()
        if not text:
            return None

        # 1) 精确匹配（大小写不敏感），覆盖上面全部取值
        if text in _STATE_CLASS_VALUES:
            return False
        if text in _STATE_BREAK_VALUES:
            return True
        if text in _STATE_NONE_VALUES:
            return None

        # 2) 兜底：兼容旧版桥接器与未来可能的变体写法（BreakingTime / AfterSchoolTime…）
        if "break" in text:
            return True
        if "after" in text:
            return True
        if "class" in text:
            return False
        logger.debug("Unrecognised CurrentState value: %r", value)
        return None

    async def _query_current_state(self, websocket) -> Optional[bool]:
        """Single-value convenience wrapper around :meth:`_probe_state`."""
        state, usable = await self._probe_state(websocket)
        return state if usable else None

    def _note_state_unavailable(self) -> None:
        """Track unusable polls and tell the window once they pile up."""
        self._unavailable_streak += 1
        if self._unavailable_streak < _STATE_UNAVAILABLE_LIMIT:
            return
        if self._unavailable_announced:
            return
        self._unavailable_announced = True
        logger.warning(
            "ClassIsland bridge gave no usable class/break state for %s consecutive "
            "polls — deriving the state from a timetable instead",
            self._unavailable_streak,
        )
        self.state_unavailable.emit()

    def _note_state_usable(self) -> None:
        """Reset the unusable-poll tracking after a real answer."""
        if self._unavailable_announced:
            logger.info("ClassIsland bridge reports a usable state again")
        self._unavailable_streak = 0
        self._unavailable_announced = False

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
            self._note_state_usable()
            self._in_break = True
            subject = payload.strip() or _UNKNOWN_SUBJECT
            logger.info("ClassIsland: break started (v1), next subject=%s", subject)
            self.break_started.emit(subject)
        elif kind == "OnClass":
            self._note_state_usable()
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
            # A real event proves the bridge is live, so it also clears any
            # "bridge cannot tell us" degradation.
            self._note_state_usable()
            self._in_break = True
            subject = await self._query_next_subject(websocket)
            logger.info("ClassIsland: break started, next subject=%s", subject)
            self.break_started.emit(subject)
            return

        if event_name == _EVENT_CLASS:
            self._note_state_usable()
            self._in_break = False
            logger.info("ClassIsland: class started")
            self.class_started.emit()
            return

        logger.debug("ClassIsland bridge sent unknown event %r", event_name)

    async def _query_properties(self, websocket, keys) -> Optional[Dict[str, Any]]:
        """Send a ``get_properties`` request and return the ``data`` mapping.

        Must only be called from :meth:`_read_loop` (there can be just one
        pending ``recv`` per socket); returns ``None`` on any failure.

        新版 CIB 在"与 ClassIsland 的 IPC 已断开"时，**任意属性请求**都返回纯文本
        「ClassIsland 已离线」（``capabilities`` 除外）。这是**链路级**的断连信号，
        与「``CurrentState=None``（当天无课/还没到第一节课）」是完全不同的两件事，
        所以这里单独识别、单独记一条信息级日志（否则会被当成"属性解析失败"，
        用户只能看到一堆 debug）。状态仍按"不可用"处理 → 降级用导入课表。
        """
        try:
            await websocket.send(
                json.dumps({"action": "get_properties", "keys": list(keys)})
            )
            raw = await asyncio.wait_for(websocket.recv(), timeout=_PROPERTY_QUERY_TIMEOUT)
        except Exception as exc:
            logger.debug("get_properties(%s) failed: %s", keys, exc)
            return None

        text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        if not text.lstrip().startswith("{"):
            # 桥接器用纯文本回复（例如「ClassIsland 已离线」）
            from . import cib_daemon

            if cib_daemon.is_offline_reply(text):
                # 每 15 秒轮询一次，断连期间不要刷满日志：只报第一次，恢复后再报
                if self._offline_reported:
                    logger.debug("桥接器仍提示：%s", text.strip()[:40])
                else:
                    self._offline_reported = True
                    logger.info(
                        "桥接器提示「%s」：桥接器进程活着，但它与 ClassIsland 的 IPC 已断开"
                        "（按状态不可用处理，改用导入课表）",
                        text.strip()[:40],
                    )
            else:
                logger.debug("get_properties(%s) 收到非 JSON 回复：%r", keys, text[:80])
            return None

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.debug("get_properties(%s) 收到无法解析的 JSON：%r", keys, text[:80])
            return None

        if not isinstance(payload, dict) or payload.get("type") != "properties":
            logger.debug("Unexpected response to get_properties: %r", str(payload)[:120])
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        # 拿到了正常属性 → IPC 已经恢复，下次再断连要重新报一次
        self._offline_reported = False
        self._last_properties.update(data)
        return data

    async def _query_property(self, websocket, key: str) -> Any:
        """Read a single property (``None`` when unavailable)."""
        data = await self._query_properties(websocket, [key])
        if data is None:
            return None
        return data.get(key)

    async def _query_next_subject(self, websocket) -> str:
        """Ask the bridge for ``NextClassSubject`` (best effort)."""
        subject = await self._query_property(websocket, "NextClassSubject")
        if isinstance(subject, str) and subject.strip():
            return subject.strip()
        return _UNKNOWN_SUBJECT
