"""Plugin configuration: unified ``config.toml`` + optional runtime overrides.

The plugin no longer reads a ``plugin.toml`` of its own.  Everything comes from
the project-wide ``config.toml`` (see :mod:`shared.config_manager`), which means
switching ``active_env`` between ``dev`` and ``prod`` immediately changes the
monitored groups and admin list.

NoneBot's own runtime config (``.env`` / driver config) still wins when a value
is set explicitly there; the helper functions below only fall back to the
unified file when the runtime value is still at its built-in default.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

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
    ai_enabled: bool = True
    ai_api_key: str = ""
    ai_api_url: str = "https://api.deepseek.com/beta"
    ai_model: str = "deepseek-flash"

    # --- read-only context from the unified config -----------------------
    active_env: str = "dev"
    debug_mode: bool = False


def merge_with_plugin_config(
    runtime_config: Config,
    *,
    plugin_config_path: Optional[Path] = None,
) -> Config:
    """Merge NoneBot runtime config with the unified ``config.toml``.

    Args:
        runtime_config: values supplied by NoneBot's own configuration.
        plugin_config_path: force reading a specific file (tests / legacy).
            When omitted the unified ``config.toml`` is used, with a fallback
            to a legacy ``plugin.toml`` if the unified file is absent.
    """
    plugin = load_plugin_toml_config(plugin_config_path)

    defaults = Config()
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
            "ai_enabled": bool(plugin.ai_enabled),
            "ai_api_key": _prefer_str(runtime_config.ai_api_key, plugin.ai_api_key, sentinel=""),
            "ai_api_url": _prefer_str(
                runtime_config.ai_api_url, plugin.ai_api_url, sentinel=defaults.ai_api_url
            ),
            "ai_model": _prefer_str(
                runtime_config.ai_model, plugin.ai_model, sentinel=defaults.ai_model
            ),
            "active_env": plugin.active_env,
            "debug_mode": plugin.debug_mode,
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
    """Return *current* if non-empty and different from *sentinel*, else *fallback*.

    The sentinel is the field's built-in default: a runtime value equal to the
    default means "not configured here", so the unified config wins.
    """
    if current and current != sentinel:
        return current
    return fallback
