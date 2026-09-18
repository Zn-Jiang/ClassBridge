"""Production-grade AI message classifier for the home-school QQ group.

Decides whether a parent's message contains something that should be passed on
to the **student** (bring an item, a reminder, asking for leave, …).  Messages
classified as notifications are forwarded to the classroom client, so parents
don't need to @-mention the bot or remember any commands.

Design notes (validated against ``other/_test_ds.py``):

* DeepSeek official API, ``https://api.deepseek.com/beta``, model
  ``deepseek-flash``, ``temperature=0.0``, ``max_tokens=15`` and thinking
  disabled — the model must answer with a single digit.
* A module-level :class:`AsyncOpenAI` singleton keeps the HTTP connection
  alive between messages; :func:`warmup_connection` primes it at start-up.
* :func:`complete_and_parse_json` tolerates every output shape observed in
  practice: proper JSON, a bare ``1``/``0``, ``True``/``False``, and truncated
  JSON fragments that only need braces re-attached.
* Failures are safe: after exhausting retries the classifier returns ``False``
  so a transient outage never forwards a random message (the parent can still
  @-mention the bot as a fallback).
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

from openai import AsyncOpenAI

logger = logging.getLogger("kg.plugin.ai_classifier")

# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://api.deepseek.com/beta"
DEFAULT_MODEL = "deepseek-flash"
#: Environment variable consulted when no api_key is configured explicitly.
API_KEY_ENV_VAR = "DEEPSEEK_API_KEY"

#: Sent verbatim with every request (see ``other/_test_ds.py``).
_EXTRA_BODY: Dict[str, Any] = {"thinking": {"type": "disabled"}}

SYSTEM_PROMPT = """你是一个家校群消息分类器。分析用户消息是否包含需要告知或提醒【学生】的事项（如带物、转告、请假、嘱咐等）。

【绝对输出限制】
你必须且只能输出一个数字：
- 包含给学生的通知/提醒（即便混杂提问）：输出 1
- 纯提问、纯寒暄、或给【老师/他人】的通知：输出 0

严禁输出任何其他字符、标点符号、解释或换行！只输出数字 1 或 0。"""

#: Values that mean "yes" / "no" when the model answers with words.
_TRUE_WORDS = {"1", "true", "yes", "y", "真", "是"}
_FALSE_WORDS = {"0", "false", "no", "n", "假", "否"}

# ---------------------------------------------------------------------------
# module state (singleton client + effective configuration)
# ---------------------------------------------------------------------------

_config: Dict[str, str] = {
    "api_key": os.getenv(API_KEY_ENV_VAR, ""),
    "base_url": DEFAULT_BASE_URL,
    "model": DEFAULT_MODEL,
}
_client: Optional[AsyncOpenAI] = None
_client_key: Optional[Tuple[str, str]] = None


def configure(
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
) -> None:
    """Inject configuration (called by the plugin at start-up).

    Only values that are actually provided are changed, so a plugin can pass
    just the API key.  Changing the key/base URL invalidates the cached client
    so the next call builds a fresh one.
    """
    global _client, _client_key

    if api_key is not None:
        _config["api_key"] = str(api_key).strip()
    if base_url:
        _config["base_url"] = str(base_url).strip()
    if model:
        _config["model"] = str(model).strip()

    if _client_key is not None and _client_key != _current_client_key():
        logger.info("AI classifier configuration changed; rebuilding client")
        _client = None
        _client_key = None


def _current_client_key() -> Tuple[str, str]:
    return (_config["api_key"], _config["base_url"])


def get_client() -> Optional[AsyncOpenAI]:
    """Return the shared :class:`AsyncOpenAI` client (created on first use).

    ``None`` means "not configured" — callers must treat that as "cannot
    classify" instead of raising.
    """
    global _client, _client_key

    api_key = _config["api_key"]
    if not api_key:
        return None

    key = _current_client_key()
    if _client is None or _client_key != key:
        _client = AsyncOpenAI(api_key=api_key, base_url=_config["base_url"])
        _client_key = key
        logger.info("AI client initialised (base_url=%s, model=%s)", _config["base_url"], _config["model"])
    return _client


def is_configured() -> bool:
    """True when an API key is available."""
    return bool(_config["api_key"])


def resolved_model() -> str:
    return _config["model"]


# ---------------------------------------------------------------------------
# connection warm-up
# ---------------------------------------------------------------------------

async def warmup_connection() -> bool:
    """Open the HTTP connection ahead of the first real message.

    Sends a minimal 1-token request so TLS/DNS handshakes happen during
    start-up rather than on a parent's first message.
    """
    client = get_client()
    if client is None:
        logger.warning("AI warm-up skipped: api_key is empty")
        return False

    try:
        await client.chat.completions.create(
            model=_config["model"],
            messages=[{"role": "user", "content": "1"}],
            temperature=0.0,
            max_tokens=1,
            extra_body=_EXTRA_BODY,
        )
    except Exception as exc:
        logger.warning("AI warm-up request failed: %s", exc)
        return False

    logger.info("AI connection warmed up (model=%s)", _config["model"])
    return True


# ---------------------------------------------------------------------------
# tolerant parsing
# ---------------------------------------------------------------------------

def complete_and_parse_json(raw_text: str) -> bool:
    """Turn whatever the model returned into a boolean verdict.

    Mirrors the tolerant chain validated in ``other/_test_ds.py``:

    1. a proper ``{"is_notification": …}`` document;
    2. a bare ``1``/``0`` (or ``True``/``False``/``真``/``假``);
    3. a truncated fragment, repaired by re-attaching the missing braces.

    Raises:
        ValueError: when the text cannot be interpreted at all — callers retry.
    """
    text = _strip_code_fence((raw_text or "").strip())
    if not text:
        raise ValueError("模型返回内容为空")

    # 1) Proper JSON document.
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        data = None

    if isinstance(data, dict):
        if "is_notification" in data:
            return _coerce_bool(data["is_notification"])
        # Some replies use a different key; take the only boolean-ish value.
        for value in data.values():
            if isinstance(value, (bool, int)):
                return _coerce_bool(value)
    elif isinstance(data, bool):
        return data
    elif isinstance(data, (int, float)):
        return bool(data)

    # 2) Bare digit / word (extreme token-saving mode).
    lowered = text.lower()
    if lowered in _TRUE_WORDS:
        return True
    if lowered in _FALSE_WORDS:
        return False

    # 3) Repair a truncated JSON fragment.
    fixed = text
    if not fixed.startswith("{"):
        if "is_notification" in fixed:
            fixed = "{" + fixed
        else:
            fixed = '{"is_notification": ' + fixed
    if not fixed.endswith("}"):
        fixed = fixed + "}"

    try:
        repaired = json.loads(fixed)
    except Exception as exc:
        # 4) Last resort: the reply may have been cut off mid-literal (e.g.
        #    ``tru`` / ``fals``) by the small max_tokens budget.
        probed = _probe_truncated_boolean(text)
        if probed is not None:
            logger.info("Recovered truncated AI reply %r -> %s", raw_text[:24], probed)
            return probed
        raise ValueError(f"无法补全或解析输出内容: '{raw_text}'") from exc

    if isinstance(repaired, dict):
        return _coerce_bool(repaired.get("is_notification"))
    raise ValueError(f"无法补全或解析输出内容: '{raw_text}'")


def _probe_truncated_boolean(text: str) -> Optional[bool]:
    """Interpret a boolean literal that was cut off mid-word.

    Returns ``None`` when the trailing token is not recognisably ``true``,
    ``false``, ``1`` or ``0`` (so genuinely broken output still raises).
    """
    tail = text.rstrip().rstrip("}").strip()
    if not tail:
        return None

    # Take the token after the last ':' / '"' / whitespace.
    token = re.split(r'[:\s"]+', tail)[-1].strip().lower()
    if not token:
        return None
    if token.isdigit():
        return token != "0"
    if token in _TRUE_WORDS:
        return True
    if token in _FALSE_WORDS:
        return False
    # Prefix of a word ("tru", "fals", "t", "f", …).
    if len(token) < 4 and "true".startswith(token):
        return True
    if len(token) < 5 and "false".startswith(token):
        return False
    return None


def _strip_code_fence(text: str) -> str:
    """Remove a ```json … ``` wrapper if the model added one."""
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _coerce_bool(value: Any) -> bool:
    """Interpret a JSON scalar as a boolean.

    Unlike a plain ``bool(value)`` this maps ``"false"`` to ``False`` instead
    of ``True`` (a classic bug when models answer with words).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    if text.isdigit():
        return bool(int(text))
    return False


# ---------------------------------------------------------------------------
# public classification API
# ---------------------------------------------------------------------------

async def classify_message(text: str, max_retries: int = 3) -> bool:
    """Return ``True`` when *text* should be forwarded to the classroom client.

    Args:
        text: the parent's plain-text message.
        max_retries: maximum number of attempts (at least 1).  A response that
            cannot be parsed counts as a failed attempt, matching the retry
            behaviour of ``other/_test_ds.py``.

    Returns:
        ``True``/``False`` on success, and ``False`` as a safe default once
        every attempt has failed.
    """
    content = (text or "").strip()
    if not content:
        return False

    client = get_client()
    if client is None:
        logger.warning("AI classification skipped: api_key is empty")
        return False

    attempts = max(1, int(max_retries))
    for attempt in range(1, attempts + 1):
        try:
            response = await client.chat.completions.create(
                model=_config["model"],
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                temperature=0.0,
                max_tokens=15,
                extra_body=_EXTRA_BODY,
            )
            raw = _extract_content(response)
            result = complete_and_parse_json(raw)
        except Exception as exc:
            logger.warning(
                "AI classification attempt %s/%s failed: %s", attempt, attempts, exc
            )
            continue

        logger.info(
            "AI classify: attempt=%s/%s raw=%r -> %s",
            attempt,
            attempts,
            raw.strip()[:24],
            result,
        )
        return result

    logger.error("AI classification gave up after %s attempt(s); defaulting to False", attempts)
    return False


def _extract_content(response: Any) -> str:
    """Pull the text out of a chat-completion response."""
    try:
        return response.choices[0].message.content or ""
    except (IndexError, AttributeError, TypeError) as exc:
        raise ValueError(f"AI 响应缺少内容: {exc}") from exc
