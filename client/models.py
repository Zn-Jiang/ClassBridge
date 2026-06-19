from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.protocol import ClientMode, MessagePriority, MessageStatus


DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass
class ClientMessage:
    db_id: int
    sender_id: str
    sender_name: str
    content: str
    msg_type: MessagePriority
    status: MessageStatus
    timestamp: str
    resend_count: int = 0
    resend_time: Optional[str] = None
    group_id: Optional[str] = None
    source_message_id: Optional[int] = None

    @property
    def is_urgent(self) -> bool:
        return self.msg_type == MessagePriority.URGENT

    @property
    def is_unread(self) -> bool:
        return self.status == MessageStatus.UNREAD

    @property
    def latest_time_text(self) -> str:
        if self.resend_time:
            return f"首发 {self.timestamp}\n重发 {self.resend_time}"
        return self.timestamp

    @property
    def sort_key(self) -> tuple[datetime, int]:
        latest = self.resend_time or self.timestamp
        return (_parse_datetime(latest), self.db_id)


@dataclass
class ClientSnapshot:
    unread_items: List[ClientMessage]
    history_items: List[ClientMessage]
    client_name: str
    is_online: bool
    mode: ClientMode
    updated_at: str


def message_from_dict(data: Dict[str, Any]) -> ClientMessage:
    return ClientMessage(
        db_id=int(data["db_id"]),
        sender_id=str(data["sender_id"]),
        sender_name=str(data["sender_name"]),
        content=str(data["content"]),
        msg_type=MessagePriority(str(data["msg_type"])),
        status=MessageStatus(str(data.get("status", MessageStatus.UNREAD.value))),
        timestamp=str(data.get("timestamp", "")),
        resend_count=int(data.get("resend_count", 0)),
        resend_time=_optional_str(data.get("resend_time")),
        group_id=_optional_str(data.get("group_id")),
        source_message_id=_optional_int(data.get("source_message_id")),
    )


def snapshot_from_payload(data: Dict[str, Any]) -> ClientSnapshot:
    status = data.get("client_status", {})
    unread_items = sorted(
        [message_from_dict(item) for item in data.get("unread_items", [])],
        key=lambda item: item.sort_key,
        reverse=True,
    )
    history_items = sorted(
        [message_from_dict(item) for item in data.get("history_items", [])],
        key=lambda item: item.sort_key,
        reverse=True,
    )
    return ClientSnapshot(
        unread_items=unread_items,
        history_items=history_items,
        client_name=str(status.get("client_name", "classroom-desktop")),
        is_online=bool(status.get("is_online", False)),
        mode=ClientMode(str(status.get("mode", ClientMode.NORMAL.value))),
        updated_at=str(status.get("updated_at", "")),
    )


def _optional_str(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    return str(value)


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    return int(value)


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.strptime(value, DATETIME_FORMAT)
    except ValueError:
        return datetime.min
