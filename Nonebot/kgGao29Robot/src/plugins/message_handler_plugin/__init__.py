from __future__ import annotations

import asyncio
import contextlib
import re
import sys
import time
from pathlib import Path
from typing import Dict, Optional

from nonebot import get_bots, get_driver, get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment, PrivateMessageEvent
from nonebot.plugin import PluginMetadata

ROOT_DIR = Path(__file__).resolve().parents[5]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared.protocol import MessagePriority, MessageType

from .ai_classifier import (
    classify_message,
    configure as configure_ai_classifier,
    warmup_connection,
)
from .config import Config, merge_with_plugin_config
from .messages import (
    HELP_TEXT,
    build_operation_feedback,
    build_query_feedback,
    build_server_error_feedback,
    build_store_feedback,
    current_timestamp_text,
    parse_user_input,
)
from .server_api import ServerApiError, send_request

__plugin_meta__ = PluginMetadata(
    name="message_handler_plugin",
    description="家校沟通消息转发插件",
    usage="群内 @机器人 发送消息，或使用 /帮助 查看指令。",
    config=Config,
)

config = merge_with_plugin_config(get_plugin_config(Config))
message_handler = on_message(priority=5, block=False)
# AI classifier runs before the main handler so it can intercept natural-language
# messages that aren't @-mentions.
ai_handler = on_message(priority=4, block=False)
receipt_task: Optional[asyncio.Task] = None

# ---------------------------------------------------------------------------
# 免@命令（free-form commands）：
# 用户执行过 /查询 后，在短 ID 有效期内可直接发送自然语言执行命令，
# 无需 @机器人。注册表为可扩展列表：每项 (正则, 处理器)。
# 处理器签名：async (bot, event, arg) -> None，其中 arg 为正则第 1 个捕获组。
# 新增免@命令时只需在 FREE_COMMAND_SPECS 里追加一项即可。
# ---------------------------------------------------------------------------
# user_id -> 查询会话过期时间（time.monotonic() 时间戳）
_query_sessions: Dict[int, float] = {}


def _record_query_session(user_id: int, ttl_seconds: int) -> None:
    """Record that *user_id* has an active query session for *ttl_seconds*."""
    _query_sessions[user_id] = time.monotonic() + ttl_seconds


def _has_active_query_session(user_id: int) -> bool:
    """Return True while the user's short IDs are still valid."""
    expires_at = _query_sessions.get(user_id)
    if expires_at is None:
        return False
    if time.monotonic() > expires_at:
        _query_sessions.pop(user_id, None)
        return False
    return True


async def _try_free_command(bot: Bot, event: GroupMessageEvent, content: str) -> bool:
    """Try to run a free-form (no-@) command; return True if one matched."""
    for pattern, handler in FREE_COMMAND_SPECS:
        match = pattern.match(content)
        if match:
            logger.info("Free command: user=%s content=%r", event.user_id, content[:60])
            await handler(bot, event, match.group(1))
            return True
    return False


@get_driver().on_startup
async def _on_startup() -> None:
    global receipt_task
    logger.info(
        "message_handler_plugin loaded. groups={} admins={} server={} ai_model={}",
        config.class_group_ids,
        config.admin_users,
        config.server_ws_url,
        config.ai_model or "(disabled)",
    )
    # Inject the AI configuration once, then warm the HTTP connection so the
    # first parent message doesn't pay for DNS/TLS.
    configure_ai_classifier(
        api_key=config.ai_api_key,
        base_url=config.ai_api_url,
        model=config.ai_model,
    )
    asyncio.create_task(warmup_connection())
    receipt_task = asyncio.create_task(_receipt_loop())


@get_driver().on_shutdown
async def _on_shutdown() -> None:
    global receipt_task
    if receipt_task is not None:
        receipt_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await receipt_task
        receipt_task = None


# =========================================================================
# AI intent classification handler (priority 4 — runs before the main handler)
# =========================================================================


@ai_handler.handle()
async def handle_ai_classify(bot: Bot, event: MessageEvent) -> None:
    """Auto-detect notification intent in non-admin group messages.

    Parents can type naturally (e.g. "下午接孩子时帮带一本书") without
    @-mentioning the bot.  The AI classifier decides whether the message
    should be forwarded to the classroom client.

    Rule-matching runs BEFORE the AI call to save tokens: users who recently
    ran /查询 may issue free-form (no-@) commands such as "撤回 1" / "重发 2";
    anything that matches is handled locally and never reaches the AI.
    """
    # Only applies to group messages (not private chat).
    if not isinstance(event, GroupMessageEvent):
        return

    # Only for configured class groups.
    if event.group_id not in set(config.class_group_ids):
        return

    # Skip messages from admins — they use explicit @bot commands, never AI.
    if event.user_id in set(config.admin_users):
        return

    # Skip @bot messages — the main handler already processes them.
    if event.is_tome():
        return

    content = event.get_plaintext().strip()
    if not content:
        return

    # 1) Rule matching first (no token cost): free-form commands for users
    #    whose short IDs are still valid after a /查询.
    if _has_active_query_session(event.user_id):
        if await _try_free_command(bot, event, content):
            return

    # Skip explicit slash commands (no point sending them to AI).
    if content.startswith("/"):
        return

    logger.info(
        "AI classify: user=%s group=%s content=%r",
        event.user_id,
        event.group_id,
        content[:80],
    )

    # The classifier uses the module-level client configured at start-up.
    is_notification = await classify_message(content)

    if not is_notification:
        logger.info("AI classify: NOT a notification, skipping")
        return

    logger.info("AI classify: IS a notification, auto-forwarding")
    await _auto_forward(bot, event, content)


async def _auto_forward(bot: Bot, event: GroupMessageEvent, content: str) -> None:
    """Forward a message that the AI classified as a notification."""
    group_id = str(event.group_id)
    try:
        response = await send_request(
            config,
            MessageType.NEW_MESSAGE,
            data={
                "sender_id": str(event.user_id),
                "sender_name": _sender_name(event),
                "content": content,
                "msg_type": MessagePriority.NORMAL.value,
                "timestamp": current_timestamp_text(),
                "group_id": group_id,
                "source_message_id": getattr(event, "message_id", None),
            },
        )
    except ServerApiError as exc:
        await bot.send(
            event,
            MessageSegment.reply(event.message_id)
            + MessageSegment.text(f" AI 自动转发失败：{exc}"),
        )
        return

    # Use the same feedback logic as the manual dispatch path.
    await bot.send(
        event,
        MessageSegment.reply(event.message_id)
        + MessageSegment.text(
            " " + build_store_feedback(response, MessagePriority.NORMAL)
        ),
    )


# =========================================================================
# Main message handler (priority 5 — @bot commands & explicit dispatch)
# =========================================================================


@message_handler.handle()
async def handle_message(bot: Bot, event: MessageEvent) -> None:
    if _is_self_message(event):
        return

    if isinstance(event, GroupMessageEvent):
        if event.group_id not in set(config.class_group_ids):
            return
        if not event.is_tome():
            return
    elif isinstance(event, PrivateMessageEvent):
        if event.user_id not in set(config.admin_users):
            await bot.send(event, "为保证安全，私聊指令仅限管理员使用。请在家长群里 @机器人 发送消息。")
            return
    else:
        return

    parsed = parse_user_input(event.get_plaintext())
    kind = parsed["kind"]

    if kind in {"empty", "help"}:
        await _reply(bot, event, HELP_TEXT)
        return
    if kind == "query":
        await _handle_query(bot, event)
        return
    if kind == "recall":
        await _handle_recall(bot, event, parsed["content"] or "")
        return
    if kind == "resend":
        await _handle_resend(bot, event, parsed["content"] or "")
        return
    if kind == "dispatch":
        if parsed["command"] == "/紧急消息" and isinstance(event, GroupMessageEvent):
            await _reply(bot, event, "紧急消息仅限管理员私聊发送。")
            return
        await _handle_dispatch(bot, event, parsed["content"] or "", parsed["command"])


async def _handle_dispatch(bot: Bot, event: MessageEvent, content: str, command: Optional[str]) -> None:
    content = content.strip()
    if not content:
        await _reply(bot, event, "消息内容不能为空，请重新输入。")
        return

    msg_type = MessagePriority.URGENT if command == "/紧急消息" else MessagePriority.NORMAL
    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else None
    try:
        response = await send_request(
            config,
            MessageType.NEW_MESSAGE,
            data={
                "sender_id": str(event.user_id),
                "sender_name": _sender_name(event),
                "content": content,
                "msg_type": msg_type.value,
                "timestamp": current_timestamp_text(),
                "group_id": group_id,
                "source_message_id": getattr(event, "message_id", None),
            },
        )
    except ServerApiError as exc:
        await _reply(bot, event, build_server_error_feedback(exc))
        return

    await _reply(bot, event, build_store_feedback(response, msg_type))


async def _handle_query(bot: Bot, event: MessageEvent) -> None:
    try:
        response = await send_request(
            config,
            MessageType.QUERY_UNREAD,
            data={"sender_id": str(event.user_id)},
        )
    except ServerApiError as exc:
        await _reply(bot, event, build_server_error_feedback(exc))
        return
    await _reply(bot, event, build_query_feedback(response))

    # Remember the query session so this user can run free-form commands
    # (e.g. "撤回 1") without @-mentioning the bot while the short IDs live.
    try:
        ttl_seconds = int(response.get("expires_in_seconds", 300))
    except (TypeError, ValueError):
        ttl_seconds = 300
    _record_query_session(event.user_id, ttl_seconds)
    logger.info("Query session recorded for user=%s ttl=%ss", event.user_id, ttl_seconds)


async def _handle_recall(bot: Bot, event: MessageEvent, short_id: str) -> None:
    if not short_id:
        await _reply(bot, event, "请提供要撤回的短 ID，例如：/撤回 1")
        return
    try:
        response = await send_request(
            config,
            MessageType.RECALL_MESSAGE,
            data={"sender_id": str(event.user_id), "short_id": short_id},
        )
    except ServerApiError as exc:
        await _reply(bot, event, build_server_error_feedback(exc))
        return
    await _reply(bot, event, build_operation_feedback(response))


async def _handle_resend(bot: Bot, event: MessageEvent, short_id: str) -> None:
    if not short_id:
        await _reply(bot, event, "请提供要重发的短 ID，例如：/重发 1")
        return
    try:
        response = await send_request(
            config,
            MessageType.RESEND_MESSAGE,
            data={"sender_id": str(event.user_id), "short_id": short_id},
        )
    except ServerApiError as exc:
        await _reply(bot, event, build_server_error_feedback(exc))
        return
    await _reply(bot, event, build_operation_feedback(response))


async def _receipt_loop() -> None:
    while True:
        try:
            payload = await send_request(config, MessageType.FETCH_RECEIPTS, data={})
            for item in payload.get("items", []):
                await _deliver_receipt(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Failed to fetch receipts: {}", exc)
        await asyncio.sleep(3)


async def _deliver_receipt(item: dict) -> None:
    bots = list(get_bots().values())
    if not bots:
        return
    bot = bots[0]
    target_user_id = int(item["target_user_id"])
    source_message_id = int(item["source_message_id"])
    group_id_text = str(item.get("group_id", "")).strip()
    if group_id_text:
        message = (
            MessageSegment.reply(source_message_id)
            + MessageSegment.at(target_user_id)
            + MessageSegment.text(f" {item['text']}")
        )
        await bot.send_group_msg(group_id=int(group_id_text), message=message)
        return

    message = MessageSegment.reply(source_message_id) + MessageSegment.text(item["text"])
    await bot.send_private_msg(user_id=target_user_id, message=message)


async def _reply(bot: Bot, event: MessageEvent, text: str) -> None:
    if isinstance(event, GroupMessageEvent):
        message = MessageSegment.reply(event.message_id) + MessageSegment.text(text)
        await bot.send(event, message)
        return
    await bot.send(event, text)


def _sender_name(event: MessageEvent) -> str:
    return str(event.sender.card or event.sender.nickname or event.user_id)


def _is_self_message(event: MessageEvent) -> bool:
    try:
        return int(event.user_id) == int(event.self_id)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 免@命令注册表（定义在末尾，避免模块加载时的前向引用）：
# 每项 (正则, 处理器)，处理器签名 async (bot, event, arg) -> None，
# arg 为正则第 1 个捕获组。\s+ 允许空格；^\/? 兼容用户手滑带上斜杠。
# 新增免@命令时只需在此追加一项即可。
# ---------------------------------------------------------------------------
FREE_COMMAND_SPECS = [
    (re.compile(r"^\/?\s*撤回\s*(\d+)\s*$"), _handle_recall),
    (re.compile(r"^\/?\s*重发\s*(\d+)\s*$"), _handle_resend),
]
