import json
import uuid
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class WsEndpoint(str, Enum):
    CLIENT = "/ws/client"
    PLUGIN = "/ws/plugin"


class PeerRole(str, Enum):
    CLIENT = "client"
    PLUGIN = "plugin"
    SERVER = "server"


class MessageType(str, Enum):
    AUTH = "auth"
    AUTH_OK = "auth_ok"
    AUTH_ERROR = "auth_error"
    HEARTBEAT = "heartbeat"
    ERROR = "error"
    NEW_MESSAGE = "new_message"
    NEW_MESSAGE_STORED = "new_message_stored"
    PENDING_MESSAGES = "pending_messages"
    MARK_READ = "mark_read"
    READ_RECEIPT = "read_receipt"
    STATUS_UPDATE = "status_update"
    STATUS_SNAPSHOT = "status_snapshot"
    QUERY_UNREAD = "query_unread"
    QUERY_UNREAD_RESULT = "query_unread_result"
    RECALL_MESSAGE = "recall_message"
    RECALL_RESULT = "recall_result"
    RESEND_MESSAGE = "resend_message"
    RESEND_RESULT = "resend_result"
    FETCH_RECEIPTS = "fetch_receipts"
    RECEIPT_BATCH = "receipt_batch"


class MessagePriority(str, Enum):
    NORMAL = "normal"
    URGENT = "urgent"


class MessageStatus(str, Enum):
    UNREAD = "unread"
    READ = "read"
    RECALLED = "recalled"


class ClientMode(str, Enum):
    NORMAL = "normal"
    EXAM = "exam"


@dataclass
class Envelope:
    type: str
    data: Dict[str, Any] = field(default_factory=dict)
    request_id: Optional[str] = None
    auth_token: Optional[str] = None


@dataclass
class MessageRecord:
    db_id: Optional[int]
    sender_id: str
    sender_name: str
    content: str
    msg_type: MessagePriority
    status: MessageStatus = MessageStatus.UNREAD
    timestamp: str = ""
    resend_count: int = 0
    resend_time: Optional[str] = None
    group_id: Optional[str] = None
    short_id: Optional[str] = None
    source_message_id: Optional[int] = None


@dataclass
class ClientStatusPayload:
    client_name: str
    is_online: bool
    mode: ClientMode
    updated_at: str


@dataclass
class ShortIdMapping:
    db_id: int
    short_id: str
    sender_id: str
    msg_type: MessagePriority
    content_preview: str
    timestamp: str


@dataclass
class OperationResult:
    ok: bool
    message: str
    db_id: Optional[int] = None
    short_id: Optional[str] = None


@dataclass
class ReceiptRecord:
    receipt_id: int
    message_db_id: int
    group_id: str
    target_user_id: str
    source_message_id: int
    text: str


def new_request_id() -> str:
    return uuid.uuid4().hex


def make_envelope(
    message_type: MessageType,
    data: Optional[Dict[str, Any]] = None,
    *,
    request_id: Optional[str] = None,
    auth_token: Optional[str] = None,
) -> Envelope:
    return Envelope(
        type=message_type.value,
        data=data or {},
        request_id=request_id or new_request_id(),
        auth_token=auth_token,
    )


def envelope_to_dict(envelope: Envelope) -> Dict[str, Any]:
    return _to_plain_data(envelope)


def envelope_to_json(envelope: Envelope) -> str:
    return json.dumps(envelope_to_dict(envelope), ensure_ascii=True, separators=(",", ":"))


def parse_envelope_json(raw_text: str) -> Envelope:
    payload = json.loads(raw_text)
    if not isinstance(payload, dict):
        raise ValueError("WebSocket payload must decode to a JSON object.")

    message_type = payload.get("type")
    if not message_type:
        raise ValueError("WebSocket payload is missing required field: type")

    data = payload.get("data", {})
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("WebSocket payload field 'data' must be a JSON object.")

    return Envelope(
        type=str(message_type),
        data=data,
        request_id=_optional_string(payload.get("request_id")),
        auth_token=_optional_string(payload.get("auth_token")),
    )


def message_records_to_payload(records: List[MessageRecord]) -> List[Dict[str, Any]]:
    return [_to_plain_data(item) for item in records]


def _to_plain_data(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _to_plain_data(item) for key, item in asdict(value).items() if item is not None}
    if isinstance(value, dict):
        return {key: _to_plain_data(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_to_plain_data(item) for item in value]
    return value


def _optional_string(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    return str(value)
