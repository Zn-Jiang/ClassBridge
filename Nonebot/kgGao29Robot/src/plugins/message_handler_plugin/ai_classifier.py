"""AI intent classifier for parent messages in the home-school group.

Calls the DeepSeek official API (deepseek-v4-flash) to decide whether a
message is a *notification* that should be forwarded to the classroom client.

Uses DeepSeek's prefix-completion trick: the assistant turn is pre-seeded
with ``{"is_notification":`` and generation stops at ``}``, so the model only
emits ``true`` / ``false`` — fast and cheap.

Messages classified as notifications are forwarded automatically —
parents don't need to @-mention the bot or remember any commands.
"""

from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI

logger = logging.getLogger("kg.plugin.ai_classifier")

SYSTEM_PROMPT = """\
你是一个专门分析家校群消息的自动化助手。
请分析用户消息是否包含通知信息（如请假、送药、带东西、通知孩子事情。注意，通知对象必须是学生，而不是老师或者其他人），如包含，则为 True。
如果是提问、闲聊或收到谢谢等则为 False。
如果出现通知类消息与其他消息混合的情况，判定为 True（如："我耳机是不是落你书包了？今晚打个电话给我"，前半句属于提问、后半句属于通知，因此判定为 True）。
只输出 {"is_notification": true} 或 {"is_notification": false}，不要输出其他任何文字。\
"""


async def classify_message(
    content: str,
    *,
    api_key: str,
    api_url: str,
    model: str,
) -> bool:
    """Return True when *content* should be forwarded to the classroom client.

    Returns False on any error (API failure, timeout, malformed response) so
    that the parent's message is never silently dropped due to a transient
    outage — they can still @-mention the bot as a fallback.
    """
    if not api_key:
        logger.warning("AI classification skipped: api_key is empty")
        return False

    if not content.strip():
        return False

    try:
        client = AsyncOpenAI(base_url=api_url, api_key=api_key)
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"分析以下家长的消息是否为通知类信息：{content}",
                },
                # Prefix completion: seed the assistant reply and let the model
                # finish just the boolean, then stop at the closing brace.
                {"role": "assistant", "content": '{"is_notification":', "prefix": True},
            ],
            stream=False,
            stop=["}"],
            extra_body={
                "thinking": {"type": "disabled"},
            },
        )
    except Exception as exc:
        logger.warning("AI classification API call failed: %s", exc)
        return False

    try:
        text = (response.choices[0].message.content or "").strip()
    except (IndexError, AttributeError) as exc:
        logger.warning("AI classification response missing content: %s", exc)
        return False

    # Re-assemble the JSON that the model was forced to complete.
    # The model emits e.g. " true" / "false", so the result is a valid
    # ``{"is_notification": true}`` document.
    full_json = '{"is_notification":' + text + "}"
    return _parse_is_notification(full_json)


def _parse_is_notification(text: str) -> bool:
    """Extract ``is_notification`` from a JSON response.

    Handles common formatting quirks like markdown code fences.
    """
    cleaned = text.strip()
    # Strip markdown code fences if present.
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove opening fence (```json or ```) and closing fence (```)
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        result = json.loads(cleaned)
        return bool(result.get("is_notification", False))
    except json.JSONDecodeError:
        logger.warning("AI classification returned non-JSON: %r", text[:200])
        return False
