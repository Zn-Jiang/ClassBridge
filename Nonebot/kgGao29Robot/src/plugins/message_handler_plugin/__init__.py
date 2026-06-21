from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Optional

from nonebot import get_bots, get_driver, get_plugin_config, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment, PrivateMessageEvent
from nonebot.plugin import PluginMetadata

ROOT_DIR = Path(__file__).resolve().parents[5]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared.protocol import MessagePriority, MessageType

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
receipt_task: Optional[asyncio.Task] = None


@get_driver().on_startup
async def _on_startup() -> None:
    global receipt_task
    logger.info(
        "message_handler_plugin loaded. groups={} admins={} server={}",
        config.class_group_ids,
        config.admin_users,
        config.server_ws_url,
    )
    receipt_task = asyncio.create_task(_receipt_loop())


@get_driver().on_shutdown
async def _on_shutdown() -> None:
    global receipt_task
    if receipt_task is not None:
        receipt_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await receipt_task
        receipt_task = None


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
