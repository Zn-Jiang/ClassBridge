from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).resolve().parents[5]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

_PLUGIN_DIR = Path(__file__).resolve().parents[3]  # kgGao29Robot/

from shared.config import load_plugin_toml_config


class Config(BaseModel):
    """Shared plugin settings for the house-school communication project."""

    internal_token: str = "dev-internal-token"
    server_ws_url: str = "ws://127.0.0.1:8765/ws/plugin"
    bot_name: str = "kgGao29MessageBot"
    class_group_ids: List[int] = Field(default_factory=list)
    admin_users: List[int] = Field(default_factory=list)
    short_id_ttl_seconds: int = 300

    # AI intent classification (DeepSeek official API)
    ai_api_key: str = ""
    ai_api_url: str = "https://api.deepseek.com/beta"
    ai_model: str = "deepseek-v4-flash"


def merge_with_plugin_config(runtime_config: Config) -> Config:
    """Use NoneBot runtime config first, then fall back to configs/plugin.toml."""

    plugin = load_plugin_toml_config(_PLUGIN_DIR / "plugin.toml")

    merged = runtime_config.model_copy(
        update={
            "internal_token": _prefer(runtime_config.internal_token, plugin.internal_token),
            "server_ws_url": _prefer(runtime_config.server_ws_url, plugin.server_ws_url),
            "bot_name": _prefer(runtime_config.bot_name, plugin.bot_name),
            "class_group_ids": _prefer_list(runtime_config.class_group_ids, plugin.class_group_ids),
            "admin_users": _prefer_list(runtime_config.admin_users, plugin.admin_users),
            "short_id_ttl_seconds": _prefer_int(
                runtime_config.short_id_ttl_seconds, plugin.short_id_ttl_seconds
            ),
            "ai_api_key": _prefer_str(runtime_config.ai_api_key, plugin.ai_api_key, sentinel=""),
            "ai_api_url": _prefer_str(runtime_config.ai_api_url, plugin.ai_api_url, sentinel=""),
            "ai_model": _prefer_str(runtime_config.ai_model, plugin.ai_model, sentinel=""),
        }
    )
    return merged


def _prefer(current: str, fallback: str) -> str:
    if current and current not in {"dev-internal-token", "ws://127.0.0.1:8765/ws/plugin", "kgGao29MessageBot"}:
        return current
    return fallback


def _prefer_list(current: List[int], fallback: List[int]) -> List[int]:
    return list(current) if current else list(fallback)


def _prefer_int(current: int, fallback: int) -> int:
    return current if current != 300 else fallback


def _prefer_str(current: str, fallback: str, sentinel: str = "") -> str:
    """Return *current* if non-empty and different from *sentinel*, else *fallback*."""
    if current and current != sentinel:
        return current
    return fallback
