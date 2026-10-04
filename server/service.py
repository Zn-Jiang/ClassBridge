from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, Optional

from shared.config import ServerConfig
from shared.protocol import (
    ClientMode,
    MessagePriority,
    MessageType,
    MessageStatus,
    OperationResult,
    envelope_to_dict,
    make_envelope,
    message_records_to_payload,
)

from .database import Database, local_now
from .short_id import ShortIdStore

logger = logging.getLogger("kg.server.service")

#: Fallback history window when the server config does not set one.
#: ``0`` disables the window entirely.
DEFAULT_HISTORY_WINDOW_DAYS = 30
#: 智能反转的时间窗口：家长只发 @机器人 时，只回看最近这么久内的消息
DEFAULT_REVERSE_WINDOW_SECONDS = 120


class ServerService:
    def __init__(self, config: ServerConfig, database: Database, short_ids: ShortIdStore):
        self._config = config
        self._database = database
        self._short_ids = short_ids

    def update_config(self, config: ServerConfig) -> None:
        """Hot-reload the settings this service reads on every request.

        ``internal_token``, ``short_id_ttl_seconds`` and the client name apply
        immediately; listen addresses/database path need a restart (the caller
        logs a warning for those).
        """
        self._config = config

    def handle_plugin_request(self, message_type: str, data: Dict[str, Any], request_id: Optional[str]):
        if message_type == MessageType.NEW_MESSAGE.value:
            return self._handle_new_message(data, request_id)
        if message_type == MessageType.QUERY_UNREAD.value:
            return self._handle_query_unread(data, request_id)
        if message_type == MessageType.RECALL_MESSAGE.value:
            return self._handle_recall_message(data, request_id)
        if message_type == MessageType.RESEND_MESSAGE.value:
            return self._handle_resend_message(data, request_id)
        if message_type == MessageType.FETCH_RECEIPTS.value:
            return self._handle_fetch_receipts(request_id)
        if message_type == MessageType.RECORD_IGNORED.value:
            return self._handle_record_ignored(data, request_id)
        if message_type == MessageType.RECENT_SENDER_MESSAGE.value:
            return self._handle_recent_sender_message(data, request_id)
        if message_type == MessageType.FORWARD_MESSAGE.value:
            return self._handle_forward_message(data, request_id)
        if message_type == MessageType.HEARTBEAT.value:
            return envelope_to_dict(
                make_envelope(
                    MessageType.STATUS_SNAPSHOT,
                    data={"client_status": envelope_to_dict(self._database.get_client_status())},
                    request_id=request_id,
                )
            )
        return self._error_response(f"Unsupported plugin message type: {message_type}", request_id)

    def handle_client_request(self, message_type: str, data: Dict[str, Any], request_id: Optional[str]):
        if message_type == MessageType.PENDING_MESSAGES.value:
            return self._handle_pending_messages(data, request_id)
        if message_type == MessageType.MARK_READ.value:
            return self._handle_mark_read(data, request_id)
        if message_type == MessageType.STATUS_UPDATE.value:
            return self._handle_status_update(data, request_id)
        if message_type == MessageType.HEARTBEAT.value:
            return envelope_to_dict(
                make_envelope(
                    MessageType.STATUS_SNAPSHOT,
                    data={"client_status": envelope_to_dict(self._database.get_client_status())},
                    request_id=request_id,
                )
            )
        return self._error_response(f"Unsupported client message type: {message_type}", request_id)

    def mark_client_offline(self, client_name: Optional[str]) -> None:
        status = self._database.get_client_status()
        self._database.update_client_status(
            client_name=client_name or self._config.client_name,
            is_online=False,
            mode=status.mode,
        )

    def _handle_new_message(self, data: Dict[str, Any], request_id: Optional[str]):
        try:
            stored = self._database.store_message(
                sender_id=str(data["sender_id"]),
                sender_name=str(data["sender_name"]),
                content=str(data["content"]),
                msg_type=MessagePriority(str(data["msg_type"])),
                timestamp=_optional_str(data.get("timestamp")),
                group_id=_optional_str(data.get("group_id")),
                source_message_id=_optional_int(data.get("source_message_id")),
            )
        except KeyError as exc:
            return self._error_response(f"new_message missing field: {exc.args[0]}", request_id)
        except ValueError as exc:
            return self._error_response(str(exc), request_id)

        return envelope_to_dict(
            make_envelope(
                MessageType.NEW_MESSAGE_STORED,
                data={
                    "ok": True,
                    "message": "消息已存入消息服务器。",
                    "record": envelope_to_dict(stored.message),
                    "client_status": envelope_to_dict(stored.client_status),
                },
                request_id=request_id,
            )
        )

    def _handle_query_unread(self, data: Dict[str, Any], request_id: Optional[str]):
        sender_id = _optional_str(data.get("sender_id"))
        if not sender_id:
            return self._error_response("query_unread missing sender_id", request_id)

        unread_records = self._database.list_unread_messages_for_sender(sender_id)
        mappings = self._short_ids.create_scope(
            sender_id=sender_id,
            records=unread_records,
            ttl_seconds=self._config.short_id_ttl_seconds,
        )
        return envelope_to_dict(
            make_envelope(
                MessageType.QUERY_UNREAD_RESULT,
                data={
                    "ok": True,
                    "message": "查询完成。",
                    "expires_in_seconds": self._config.short_id_ttl_seconds,
                    "items": [envelope_to_dict(item) for item in mappings],
                },
                request_id=request_id,
            )
        )

    def _handle_recall_message(self, data: Dict[str, Any], request_id: Optional[str]):
        """撤回一条消息。

        两种调用方式：
        * ``sender_id`` + ``short_id``：家长在 /查询 结果里按编号撤回（原有方式）；
        * ``db_id``：智能反转流程（家长只发 @机器人）已经查到具体记录，直接用 id。
        """
        sender_id = _optional_str(data.get("sender_id"))
        short_id = _optional_str(data.get("short_id"))
        raw_db_id = data.get("db_id")

        if raw_db_id is not None:
            try:
                db_id = int(raw_db_id)
            except (TypeError, ValueError):
                return self._error_response("recall_message 的 db_id 必须是整数", request_id)
        elif sender_id and short_id:
            db_id = self._short_ids.resolve(sender_id=sender_id, short_id=short_id)
            if db_id is None:
                return self._operation_response(
                    MessageType.RECALL_RESULT, False, "短 ID 无效或已过期。", request_id
                )
        else:
            return self._error_response(
                "recall_message requires (sender_id + short_id) or db_id", request_id
            )

        current = self._database.get_message(db_id)
        if current is None:
            return self._operation_response(MessageType.RECALL_RESULT, False, "消息不存在。", request_id)
        if current.status == MessageStatus.READ:
            return self._operation_response(MessageType.RECALL_RESULT, False, "消息已读，无法撤回。", request_id, db_id)
        if current.status == MessageStatus.RECALLED:
            return self._operation_response(MessageType.RECALL_RESULT, False, "消息已经撤回。", request_id, db_id)
        if current.status == MessageStatus.IGNORED:
            return self._operation_response(
                MessageType.RECALL_RESULT,
                False,
                "这条消息没有转发到客户端，无需撤回（可以改用 @机器人 让它转发）。",
                request_id,
                db_id,
            )

        self._database.recall_message(db_id)
        return self._operation_response(
            MessageType.RECALL_RESULT,
            True,
            "消息已撤回。",
            request_id,
            db_id=db_id,
            short_id=short_id,
        )

    # ------------------------------------------------------------------
    # 智能反转：只发 @机器人 时，按最近一条消息的状态撤回或补发
    # ------------------------------------------------------------------

    def _handle_record_ignored(self, data: Dict[str, Any], request_id: Optional[str]):
        """记录一条被 AI 判定为「不是转告」的消息（不推送给客户端）。"""
        try:
            stored = self._database.store_message(
                sender_id=str(data["sender_id"]),
                sender_name=str(data.get("sender_name") or data["sender_id"]),
                content=str(data.get("content") or ""),
                msg_type=MessagePriority(
                    str(data.get("msg_type") or MessagePriority.NORMAL.value)
                ),
                timestamp=_optional_str(data.get("timestamp")),
                group_id=_optional_str(data.get("group_id")),
                source_message_id=_optional_int(data.get("source_message_id")),
                status=MessageStatus.IGNORED,
            )
        except KeyError as exc:
            return self._error_response(f"record_ignored missing field: {exc.args[0]}", request_id)
        except ValueError as exc:
            return self._error_response(str(exc), request_id)

        logger.debug(
            "已记录一条被 AI 过滤的消息 db_id=%s sender=%s",
            stored.message.db_id,
            stored.message.sender_id,
        )
        return envelope_to_dict(
            make_envelope(
                MessageType.NEW_MESSAGE_STORED,
                data={
                    "ok": True,
                    "message": "已记录（未转发）。",
                    "record": envelope_to_dict(stored.message),
                    "client_status": envelope_to_dict(stored.client_status),
                },
                request_id=request_id,
            )
        )

    def _handle_recent_sender_message(self, data: Dict[str, Any], request_id: Optional[str]):
        """查询某位家长在最近 N 秒内最新的一条消息（默认 120 秒）。"""
        sender_id = _optional_str(data.get("sender_id"))
        if not sender_id:
            return self._error_response("recent_sender_message missing sender_id", request_id)

        try:
            window_seconds = int(data.get("window_seconds") or DEFAULT_REVERSE_WINDOW_SECONDS)
        except (TypeError, ValueError):
            window_seconds = DEFAULT_REVERSE_WINDOW_SECONDS
        window_seconds = max(1, window_seconds)

        since = local_now() - timedelta(seconds=window_seconds)
        record = self._database.latest_message_for_sender(sender_id, since=since)
        return envelope_to_dict(
            make_envelope(
                MessageType.RECENT_SENDER_MESSAGE_RESULT,
                data={
                    "ok": True,
                    "window_seconds": window_seconds,
                    "record": envelope_to_dict(record) if record is not None else None,
                },
                request_id=request_id,
            )
        )

    def _handle_forward_message(self, data: Dict[str, Any], request_id: Optional[str]):
        """把一条被 AI 过滤（ignored）的消息补发到客户端。"""
        raw_db_id = data.get("db_id")
        try:
            db_id = int(raw_db_id)
        except (TypeError, ValueError):
            return self._error_response("forward_message 的 db_id 必须是整数", request_id)

        current = self._database.get_message(db_id)
        if current is None:
            return self._operation_response(MessageType.FORWARD_RESULT, False, "消息不存在。", request_id)
        if current.status == MessageStatus.IGNORED:
            updated = self._database.set_message_status(db_id, MessageStatus.UNREAD)
            logger.info("智能反转：消息 %s 已补发到客户端", db_id)
            return self._operation_response(
                MessageType.FORWARD_RESULT,
                True,
                "消息已转发到客户端。",
                request_id,
                db_id=db_id,
                record=envelope_to_dict(updated) if updated is not None else None,
            )
        if current.status == MessageStatus.UNREAD:
            return self._operation_response(
                MessageType.FORWARD_RESULT, True, "消息本来就已经转发过。", request_id, db_id=db_id
            )
        if current.status == MessageStatus.READ:
            return self._operation_response(
                MessageType.FORWARD_RESULT, True, "消息此前已转发且已被查看。", request_id, db_id=db_id
            )
        return self._operation_response(
            MessageType.FORWARD_RESULT, False, "消息已撤回，无法再转发。", request_id, db_id=db_id
        )

    def _handle_resend_message(self, data: Dict[str, Any], request_id: Optional[str]):
        sender_id = _optional_str(data.get("sender_id"))
        short_id = _optional_str(data.get("short_id"))
        if not sender_id or not short_id:
            return self._error_response("resend_message requires sender_id and short_id", request_id)

        db_id = self._short_ids.resolve(sender_id=sender_id, short_id=short_id)
        if db_id is None:
            return self._operation_response(MessageType.RESEND_RESULT, False, "短 ID 无效或已过期。", request_id)

        current = self._database.get_message(db_id)
        if current is None:
            return self._operation_response(MessageType.RESEND_RESULT, False, "消息不存在。", request_id)
        if current.status == MessageStatus.READ:
            return self._operation_response(MessageType.RESEND_RESULT, False, "消息已读，无法重发。", request_id, db_id)
        if current.status == MessageStatus.RECALLED:
            return self._operation_response(MessageType.RESEND_RESULT, False, "消息已撤回，无法重发。", request_id, db_id)

        message = self._database.resend_message(db_id)
        return envelope_to_dict(
            make_envelope(
                MessageType.RESEND_RESULT,
                data={
                    "ok": True,
                    "message": f"消息已重发，当前累计 {message.resend_count} 次。",
                    "db_id": db_id,
                    "short_id": short_id,
                    "record": envelope_to_dict(message),
                },
                request_id=request_id,
            )
        )

    def _handle_mark_read(self, data: Dict[str, Any], request_id: Optional[str]):
        db_id = _optional_int(data.get("db_id"))
        if db_id is None:
            return self._error_response("mark_read requires db_id", request_id)

        current = self._database.get_message(db_id)
        if current is None:
            return self._error_response("message not found", request_id)

        # Idempotency guard: the client may replay a read receipt after a
        # reconnect (offline queue).  Only enqueue the QQ-group receipt the
        # first time a message transitions into READ — otherwise parents get
        # duplicated receipts.
        was_already_read = current.status == MessageStatus.READ

        message = self._database.mark_message_read(db_id)
        if message is None:
            return self._error_response("message not found", request_id)

        if not was_already_read:
            self._database.enqueue_read_receipt(message, "[回执] 您的消息已被学生读取。")
        return envelope_to_dict(
            make_envelope(
                MessageType.READ_RECEIPT,
                data={
                    "ok": True,
                    "message": "消息已标记为已读。",
                    "record": envelope_to_dict(message),
                },
                request_id=request_id,
            )
        )

    def _handle_pending_messages(self, data: Dict[str, Any], request_id: Optional[str]):
        limit = int(data.get("history_limit", 200))
        # The history window is a *server* policy (``[server]
        # history_window_days``): a client asking for more is clamped, so
        # editing client.toml cannot unlock older records.
        window_days = int(getattr(self._config, "history_window_days", DEFAULT_HISTORY_WINDOW_DAYS) or 0)
        requested = data.get("history_days")
        # A client may only ever *narrow* the window: a larger value (or 0 /
        # "unlimited") is ignored, so editing client.toml cannot unlock older
        # records.
        if window_days > 0 and isinstance(requested, (int, float)) and int(requested) > 0:
            window_days = min(window_days, int(requested))
        since = None
        if window_days > 0:
            since = local_now() - timedelta(days=window_days)
        history = self._database.list_recent_messages(limit=limit, since=since)
        logger.debug(
            "pending_messages: history_limit=%s window=%sd (client asked %s) -> %s items",
            limit,
            window_days or "unlimited",
            requested if requested is not None else "-",
            len(history),
        )
        return envelope_to_dict(
            make_envelope(
                MessageType.PENDING_MESSAGES,
                data={
                    "ok": True,
                    "unread_items": message_records_to_payload(self._database.list_unread_messages()),
                    "history_items": message_records_to_payload(history),
                    "client_status": envelope_to_dict(self._database.get_client_status()),
                },
                request_id=request_id,
            )
        )

    def _handle_status_update(self, data: Dict[str, Any], request_id: Optional[str]):
        mode_text = _optional_str(data.get("mode")) or ClientMode.NORMAL.value
        client_name = _optional_str(data.get("client_name")) or self._config.client_name
        try:
            mode = ClientMode(mode_text)
        except ValueError:
            return self._error_response(f"Unsupported client mode: {mode_text}", request_id)

        status = self._database.update_client_status(
            client_name=client_name,
            is_online=bool(data.get("is_online", True)),
            mode=mode,
            is_in_break=bool(data.get("is_in_break", False)),
        )
        return envelope_to_dict(
            make_envelope(
                MessageType.STATUS_SNAPSHOT,
                data={"ok": True, "client_status": envelope_to_dict(status)},
                request_id=request_id,
            )
        )

    def _handle_fetch_receipts(self, request_id: Optional[str]):
        items = [envelope_to_dict(item) for item in self._database.fetch_pending_receipts()]
        return envelope_to_dict(
            make_envelope(
                MessageType.RECEIPT_BATCH,
                data={"ok": True, "items": items},
                request_id=request_id,
            )
        )

    def _operation_response(
        self,
        message_type: MessageType,
        ok: bool,
        message: str,
        request_id: Optional[str],
        db_id: Optional[int] = None,
        short_id: Optional[str] = None,
        record: Optional[Dict[str, Any]] = None,
    ):
        result = OperationResult(ok=ok, message=message, db_id=db_id, short_id=short_id)
        payload = envelope_to_dict(result)
        if record is not None:
            # 智能反转补发后把记录一并返回，便于插件写日志/排查
            payload["record"] = record
        return envelope_to_dict(
            make_envelope(message_type, data=payload, request_id=request_id)
        )

    def _error_response(self, message: str, request_id: Optional[str]):
        return envelope_to_dict(
            make_envelope(
                MessageType.ERROR,
                data={"ok": False, "message": message},
                request_id=request_id,
            )
        )


def _optional_str(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    return str(value)


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    return int(value)
