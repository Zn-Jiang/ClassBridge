from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from shared.protocol import ClientMode, MessagePriority


HELP_TEXT = """可用指令：
/帮助
/查询 或 /cx
/撤回 短ID
/重发 短ID

发送普通消息：
@机器人 记得带雨伞

发送紧急消息：
@机器人 /紧急消息 记得尽快去医务室
"""


def parse_user_input(text: str) -> Dict[str, Optional[str]]:
    cleaned = (text or "").strip()
    if not cleaned:
        return {"kind": "empty", "command": None, "content": None}
    if cleaned in {"/帮助", "帮助"}:
        return {"kind": "help", "command": cleaned, "content": None}
    if cleaned in {"/查询", "/cx"}:
        return {"kind": "query", "command": cleaned, "content": None}
    if cleaned.startswith("/撤回"):
        return {"kind": "recall", "command": "/撤回", "content": _tail_arg(cleaned)}
    if cleaned.startswith("/重发"):
        return {"kind": "resend", "command": "/重发", "content": _tail_arg(cleaned)}
    if cleaned.startswith("/紧急消息"):
        return {
            "kind": "dispatch",
            "command": "/紧急消息",
            "content": cleaned[len("/紧急消息") :].strip(),
        }
    return {"kind": "dispatch", "command": None, "content": cleaned}


def build_store_feedback(payload: Dict[str, Any], msg_type: MessagePriority) -> str:
    client_status = payload.get("client_status", {})
    mode = client_status.get("mode", ClientMode.NORMAL.value)
    is_online = bool(client_status.get("is_online", False))
    label = "紧急" if msg_type == MessagePriority.URGENT else "普通"
    if not is_online:
        return f"提示：学生端当前离线，您的{label}消息已存入消息服务器，上线后将立即提醒。"
    if mode == ClientMode.EXAM.value:
        return f"提示：学生端正处于考试静默模式，您的{label}消息已转发，但查看可能延迟。"
    return f"您的{label}消息已转发到客户端。"


def build_query_feedback(payload: Dict[str, Any]) -> str:
    items = payload.get("items", [])
    ttl_seconds = int(payload.get("expires_in_seconds", 300))
    if not items:
        return "当前没有未读消息。"

    lines = [f"您当前有 {len(items)} 条未读消息，短 ID 在 {ttl_seconds} 秒内有效："]
    for item in items:
        label = "紧急" if item.get("msg_type") == MessagePriority.URGENT.value else "普通"
        lines.append(
            f"[{item.get('short_id')}] {label} | {item.get('timestamp')} | {item.get('content_preview')}"
        )
    lines.append("可使用：/撤回 短ID 或 /重发 短ID")
    return "\n".join(lines)


def build_operation_feedback(payload: Dict[str, Any]) -> str:
    return str(payload.get("message", "操作已完成。"))


def build_server_error_feedback(exc: Exception) -> str:
    return f"消息服务器暂时不可用：{exc}"


def current_timestamp_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _tail_arg(text: str) -> str:
    parts = text.split(maxsplit=1)
    return "" if len(parts) < 2 else parts[1].strip()
