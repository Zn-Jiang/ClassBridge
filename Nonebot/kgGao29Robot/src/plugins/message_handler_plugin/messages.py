from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from shared.protocol import ClientMode, MessagePriority


HELP_TEXT = """可用指令：
/帮助
/查询 或 /cx
/撤回 短ID
/重发 短ID

发送消息（在群内）：
@机器人 记得带雨伞

提示：执行 /查询 后，在短 ID 有效期内可直接发送"撤回 短ID"或"重发 短ID"，无需 @机器人。"""

#: 智能反转：家长只发 @机器人（正文为空）时使用的提示
REVERSE_WINDOW_SECONDS = 120
REVERSE_NO_RECENT_TEXT = "最近 2 分钟内没有再收到您的消息，不知道要撤回还是转发。\n\n" + HELP_TEXT
REVERSE_RECALLED_TEXT = "↩️ 已为您撤回上一条转告消息。"
REVERSE_FORWARDED_TEXT = "已为您转发到客户端。"
REVERSE_FAILED_TEXT = "操作失败：{reason}"


def reverse_action_for_status(status: Optional[str]) -> str:
    """根据「最近一条消息」的状态决定反转动作（纯函数，便于单测）。

    * ``ignored``：这条消息被 AI 过滤、从未转发 → 补发（``forward``）；
    * 其余状态（``unread`` 已转发未读、``read`` 已读、``recalled`` 已撤回、
      以及未知值）：都交给服务端撤回接口，由它给出精确的拒绝理由（``recall``）。
    """
    if str(status or "").strip().lower() == "ignored":
        return "forward"
    return "recall"


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
    is_in_break = bool(client_status.get("is_in_break", False))
    label = "紧急" if msg_type == MessagePriority.URGENT else "普通"
    if label == "普通":
        label = ""
    if not is_online:
        return f"提示：学生端当前离线，您的{label}消息已存入消息服务器，上线后将立即提醒。"
    if mode == ClientMode.EXAM.value:
        return f"提示：学生端正处于考试静默模式，您的{label}消息已转发，但查看可能延迟。"
    if not is_in_break:
        return f"提示：当前正在上课，您的{label}消息已转发到客户端，学生将在课间查看。"
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
    lines.append("可回复 撤回 短ID 或 重发 短ID（无需 @机器人），也可使用 /撤回 或 /重发")
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
