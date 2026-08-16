from __future__ import annotations

import asyncio
import sys
from queue import Empty, Queue
from typing import Any, Dict

import websockets
from PyQt6.QtCore import QThread, pyqtSignal

from shared.config import ClientConfig
from shared.protocol import ClientMode, MessageType, envelope_to_json, make_envelope, parse_envelope_json

from .models import snapshot_from_payload

# How long a request/response round-trip may take before we treat the
# connection as dead.  Guards against WinError 121 (semaphore timeout) hangs
# on Windows where an idle recv() can stall forever instead of erroring.
_RESPONSE_TIMEOUT_SECONDS = 15.0


class ClientWorker(QThread):
    connection_changed = pyqtSignal(bool, str)
    snapshot_received = pyqtSignal(object)
    read_completed = pyqtSignal(int)
    read_failed = pyqtSignal(int, str)
    error_message = pyqtSignal(str)

    def __init__(self, config: ClientConfig):
        super().__init__()
        self._config = config
        self._commands: Queue[Dict[str, Any]] = Queue()
        self._running = True
        self._exam_mode = False
        self._is_in_break = False

    def stop(self) -> None:
        self._running = False
        self._commands.put({"type": "stop"})

    def mark_read(self, db_id: int) -> None:
        self._commands.put({"type": "mark_read", "db_id": db_id})

    def set_exam_mode(self, enabled: bool) -> None:
        self._exam_mode = enabled
        self._commands.put({"type": "status_update", "is_online": True, "mode": self._current_mode().value})

    def set_is_in_break(self, in_break: bool) -> None:
        """Called from the main thread to update the break state sent to the server."""
        self._is_in_break = in_break

    def request_snapshot(self) -> None:
        self._commands.put({"type": "snapshot"})

    def run(self) -> None:
        # On Windows, the default ProactorEventLoop has known issues with
        # websockets where an idle connection raises WinError 121
        # ("semaphore timeout period has expired") and the client goes into a
        # fake-offline state.  SelectorEventLoop avoids those hangs.
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(self._main())

    async def _main(self) -> None:
        ws_url = self._config.resolved_client_ws_url()
        delay = self._config.reconnect_initial_delay_seconds
        max_delay = self._config.reconnect_max_delay_seconds

        while self._running:
            try:
                self.connection_changed.emit(False, "正在连接服务器...")
                async with websockets.connect(
                    ws_url,
                    max_size=2**20,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as websocket:
                    self.connection_changed.emit(True, "已连接")
                    delay = self._config.reconnect_initial_delay_seconds
                    await self._send_status_update(websocket, True)
                    await self._request_snapshot(websocket)
                    while self._running:
                        await self._flush_commands(websocket)
                        await self._request_snapshot(websocket)
                        await asyncio.sleep(2.0)
            except Exception as exc:
                if not self._running:
                    break
                self.connection_changed.emit(False, "离线")
                self.error_message.emit(
                    f"连接服务器失败，{delay} 秒后重试（最大 {max_delay} 秒）：{exc}"
                )
                await asyncio.sleep(delay)
                delay = min(max_delay, delay * 2)

    async def _flush_commands(self, websocket) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except Empty:
                break

            command_type = command["type"]
            if command_type == "stop":
                await self._send_status_update(websocket, False)
                return
            if command_type == "mark_read":
                response = await self._send_request(
                    websocket,
                    MessageType.MARK_READ,
                    {"db_id": int(command["db_id"])},
                )
                if response.type == MessageType.READ_RECEIPT.value:
                    record = response.data.get("record", {})
                    if "db_id" in record:
                        self.read_completed.emit(int(record["db_id"]))
                else:
                    self.read_failed.emit(
                        int(command["db_id"]),
                        response.data.get("message", "已读同步失败。"),
                    )
            elif command_type == "status_update":
                await self._send_status_update(websocket, bool(command.get("is_online", True)))
            elif command_type == "snapshot":
                await self._request_snapshot(websocket)

    async def _request_snapshot(self, websocket) -> None:
        response = await self._send_request(
            websocket,
            MessageType.PENDING_MESSAGES,
            {"history_limit": 200},
        )
        if response.type == MessageType.PENDING_MESSAGES.value:
            self.snapshot_received.emit(snapshot_from_payload(response.data))
            return

        if response.type == MessageType.ERROR.value:
            raise ConnectionError(response.data.get("message", "客户端同步失败。"))

        message = response.data.get("message", f"客户端同步失败：{response.type}")
        if "pending_messages" in message:
            message = "当前 server 版本过旧，请重启 server 后再连接客户端。"
        self.error_message.emit(message)

    async def _send_status_update(self, websocket, is_online: bool) -> None:
        response = await self._send_request(
            websocket,
            MessageType.STATUS_UPDATE,
            {
                "client_name": self._config.client_name,
                "is_online": is_online,
                "mode": self._current_mode().value,
                "is_in_break": self._is_in_break,
            },
        )
        if response.type == MessageType.ERROR.value:
            raise ConnectionError(response.data.get("message", "状态同步失败。"))
        if response.type != MessageType.STATUS_SNAPSHOT.value:
            self.error_message.emit(response.data.get("message", "状态同步失败。"))

    async def _send_request(self, websocket, message_type: MessageType, data: Dict[str, Any]):
        envelope = make_envelope(
            message_type,
            data=data,
            auth_token=self._config.internal_token,
        )
        await websocket.send(envelope_to_json(envelope))
        # Guard against a stalled recv() (WinError 121 / half-open TCP on
        # Windows).  If the server does not answer in time, treat the
        # connection as broken so the outer loop reconnects cleanly.
        raw = await asyncio.wait_for(websocket.recv(), timeout=_RESPONSE_TIMEOUT_SECONDS)
        return parse_envelope_json(raw)

    def _current_mode(self) -> ClientMode:
        return ClientMode.EXAM if self._exam_mode else ClientMode.NORMAL
