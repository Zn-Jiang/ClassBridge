from __future__ import annotations

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

from .database import Database
from .short_id import ShortIdStore


class ServerService:
    def __init__(self, config: ServerConfig, database: Database, short_ids: ShortIdStore):
        self._config = config
        self._database = database
        self._short_ids = short_ids

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
        sender_id = _optional_str(data.get("sender_id"))
        short_id = _optional_str(data.get("short_id"))
        if not sender_id or not short_id:
            return self._error_response("recall_message requires sender_id and short_id", request_id)

        db_id = self._short_ids.resolve(sender_id=sender_id, short_id=short_id)
        if db_id is None:
            return self._operation_response(MessageType.RECALL_RESULT, False, "短 ID 无效或已过期。", request_id)

        current = self._database.get_message(db_id)
        if current is None:
            return self._operation_response(MessageType.RECALL_RESULT, False, "消息不存在。", request_id)
        if current.status == MessageStatus.READ:
            return self._operation_response(MessageType.RECALL_RESULT, False, "消息已读，无法撤回。", request_id, db_id)
        if current.status == MessageStatus.RECALLED:
            return self._operation_response(MessageType.RECALL_RESULT, False, "消息已经撤回。", request_id, db_id)

        self._database.recall_message(db_id)
        return self._operation_response(
            MessageType.RECALL_RESULT,
            True,
            "消息已撤回。",
            request_id,
            db_id=db_id,
            short_id=short_id,
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

        message = self._database.mark_message_read(db_id)
        if message is None:
            return self._error_response("message not found", request_id)

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
        return envelope_to_dict(
            make_envelope(
                MessageType.PENDING_MESSAGES,
                data={
                    "ok": True,
                    "unread_items": message_records_to_payload(self._database.list_unread_messages()),
                    "history_items": message_records_to_payload(self._database.list_recent_messages(limit=limit)),
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
    ):
        result = OperationResult(ok=ok, message=message, db_id=db_id, short_id=short_id)
        return envelope_to_dict(
            make_envelope(message_type, data=envelope_to_dict(result), request_id=request_id)
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
