import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import logging

from .paths import (
    CLIENT_CONFIG_PATH,
    CLIENT_EXAMPLE_CONFIG_PATH,
    CONFIG_PATH,
    PLUGIN_CONFIG_PATH,
    SERVER_CONFIG_PATH,
)

logger = logging.getLogger("kg.config")

# ---------------------------------------------------------------------------
# TOML loader (stdlib tomllib on 3.11+, tomli on older)
# ---------------------------------------------------------------------------
try:
    import tomllib as toml_loader
except ModuleNotFoundError:
    try:
        import tomli as toml_loader
    except ModuleNotFoundError:
        toml_loader = None


# ---------------------------------------------------------------------------
# Shared helper types
# ---------------------------------------------------------------------------

@dataclass
class ScheduleBreak:
    name: str
    start: str
    end: str

    def as_time_range(self) -> Tuple[time, time]:
        return (_parse_clock_time(self.start), _parse_clock_time(self.end))


def load_unified_config() -> Optional[Dict[str, Any]]:
    """Return the merged unified configuration, or ``None`` when unavailable.

    Thin wrapper around :mod:`shared.config_manager` so other modules stay
    agnostic about *where* the configuration comes from.
    """
    try:
        from .config_manager import get_config_manager

        manager = get_config_manager()
        if not manager.exists:
            return None
        return manager.get_current_config()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("读取统一配置 %s 失败：%s", CONFIG_PATH, exc)
        return None


def derive_plugin_ws_url(unified: Dict[str, Any]) -> str:
    """Derive the plugin's WebSocket URL from the unified config.

    ``[plugin].server_ws_url`` wins when present; otherwise the URL is built
    from the active environment's host/port (a wildcard listen address such as
    ``0.0.0.0`` is replaced by ``127.0.0.1`` because the plugin runs on the same
    machine as the server).
    """
    plugin_section = unified.get("plugin") if isinstance(unified.get("plugin"), dict) else {}
    configured = _none_if_empty(plugin_section.get("server_ws_url"))
    if configured:
        return configured

    server_section = unified.get("server") if isinstance(unified.get("server"), dict) else {}
    host = str(unified.get("server_host") or "127.0.0.1").strip()
    if host in {"", "0.0.0.0", "::", "*"}:
        host = "127.0.0.1"
    try:
        port = int(unified.get("server_port") or 8765)
    except (TypeError, ValueError):
        port = 8765
    path = _norm_ws(server_section.get("plugin_ws_path", "") or "/ws/plugin")
    return f"ws://{host}:{port}{path}"


# ===========================================================================
# Server config
# ===========================================================================

SERVER_CONFIG_ENV_VAR = "KG_SERVER_CONFIG"


@dataclass
class ServerConfig:
    internal_token: str = "dev-internal-token"
    host: str = "127.0.0.1"
    port: int = 8765
    database_path: str = "data/app.db"
    log_level: str = "INFO"
    client_ws_path: str = "/ws/client"
    plugin_ws_path: str = "/ws/plugin"
    client_name: str = "classroom-desktop"
    short_id_ttl_seconds: int = 300
    # --- provided by the unified config.toml -----------------------------
    active_env: str = "dev"
    debug_mode: bool = False
    # Admin API listens on all interfaces by default so the console works from
    # another machine; protect it with admin_secret_token (and firewall rules).
    admin_host: str = "0.0.0.0"
    admin_port: int = 8766
    admin_secret_token: str = ""


def load_server_config(config_path: Optional[Path] = None) -> ServerConfig:
    """Load the message-server settings.

    Prefers the unified ``config.toml`` (with the active environment already
    applied).  A legacy ``server.toml`` is only consulted when *config_path* is
    passed explicitly or no unified file exists, so old deployments keep
    working while everything migrates.
    """
    if config_path is None:
        unified = load_unified_config()
        if unified is not None:
            return _server_config_from_unified(unified)

    # --- legacy path (explicit argument or missing config.toml) ----------
    path = _resolve_path(config_path, SERVER_CONFIG_ENV_VAR, SERVER_CONFIG_PATH)
    if not path.exists():
        if config_path is None:
            logger.warning(
                "未找到 %s 且不存在旧版 server.toml，使用默认服务端配置。", CONFIG_PATH
            )
        return ServerConfig()

    logger.info("使用旧版配置文件 %s（建议迁移到统一的 config.toml）", path)
    raw = _load_toml(path)
    section = raw.get("server", {})

    return ServerConfig(
        internal_token=_opt_str(raw.get("internal_token"), ServerConfig().internal_token),
        host=_opt_str(section.get("host"), ServerConfig().host),
        port=int(_opt_str(section.get("port"), ServerConfig().port)),
        database_path=_opt_str(section.get("database_path"), ServerConfig().database_path),
        log_level=_opt_str(section.get("log_level"), ServerConfig().log_level),
        client_ws_path=_norm_ws(section.get("client_ws_path", "")),
        plugin_ws_path=_norm_ws(section.get("plugin_ws_path", "")),
        client_name=_opt_str(section.get("client_name"), ServerConfig().client_name),
        short_id_ttl_seconds=int(_opt_str(section.get("short_id_ttl_seconds"), ServerConfig().short_id_ttl_seconds)),
    )


def _server_config_from_unified(unified: Dict[str, Any]) -> ServerConfig:
    """Build :class:`ServerConfig` from the flattened unified config."""
    defaults = ServerConfig()
    section = unified.get("server") if isinstance(unified.get("server"), dict) else {}
    return ServerConfig(
        internal_token=_opt_str(unified.get("internal_token"), defaults.internal_token),
        host=_opt_str(unified.get("server_host"), defaults.host),
        port=int(unified.get("server_port") or defaults.port),
        database_path=_opt_str(section.get("database_path"), defaults.database_path),
        log_level=_opt_str(section.get("log_level"), defaults.log_level),
        client_ws_path=_norm_ws(section.get("client_ws_path", "")),
        plugin_ws_path=_norm_ws(section.get("plugin_ws_path", "")),
        client_name=_opt_str(section.get("client_name"), defaults.client_name),
        short_id_ttl_seconds=int(unified.get("short_id_ttl_seconds") or defaults.short_id_ttl_seconds),
        active_env=_opt_str(unified.get("active_env"), defaults.active_env),
        debug_mode=bool(unified.get("debug_mode", defaults.debug_mode)),
        admin_host=_opt_str(section.get("admin_host"), defaults.admin_host),
        admin_port=int(section.get("admin_port") or defaults.admin_port),
        admin_secret_token=_opt_str(unified.get("admin_secret_token"), defaults.admin_secret_token),
    )


def save_server_config(config: ServerConfig, config_path: Optional[Path] = None) -> Path:
    path = _resolve_path(config_path, SERVER_CONFIG_ENV_VAR, SERVER_CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump_server_toml(config), encoding="utf-8")
    return path


def _dump_server_toml(config: ServerConfig) -> str:
    return "\n".join([
        f'internal_token = "{_esc(config.internal_token)}"',
        "",
        "[server]",
        f'host = "{_esc(config.host)}"',
        f"port = {config.port}",
        f'database_path = "{_esc(config.database_path)}"',
        f'log_level = "{_esc(config.log_level)}"',
        f'client_ws_path = "{_esc(config.client_ws_path)}"',
        f'plugin_ws_path = "{_esc(config.plugin_ws_path)}"',
        f'client_name = "{_esc(config.client_name)}"',
        f"short_id_ttl_seconds = {config.short_id_ttl_seconds}",
        "",
    ])


# ===========================================================================
# Client config
# ===========================================================================

CLIENT_CONFIG_ENV_VAR = "KG_CLIENT_CONFIG"


@dataclass
class ClientConfig:
    internal_token: str = "dev-internal-token"
    server_ws_url: str = "ws://127.0.0.1:8765/ws/client"
    log_level: str = "INFO"
    client_name: str = "classroom-desktop"
    schedule_source: Optional[str] = None
    last_valid_schedule_source: Optional[str] = None
    # "auto"  = prefer live ClassIsland events, fall back to the local timetable
    #           when ClassIsland is not running;
    # "local" = only ever use the locally saved timetable.
    schedule_mode: str = "auto"
    # Seconds to wait after a break starts before showing the popup.
    break_popup_delay_seconds: int = 0
    # When ClassIsland/CIB is unavailable, may the timetable imported from
    # ClassIsland ("CIB 时间表") be used as the fallback?  When disabled the
    # client degrades straight to the local JSON schedule.
    use_cib_schedule: bool = True
    # Optional explicit path to ClassIsland.WSBridge.exe (empty = auto-detect).
    cib_exe_path: str = ""
    classisland_ws_url: str = "ws://localhost:6614/"
    ntp_server: str = "ntp.aliyun.com"
    auto_popup_on_break: bool = True
    close_to_tray: bool = True
    enable_urgent_sound: bool = True
    history_retention_days: int = 180
    urgent_remind_default_minutes: int = 10
    reconnect_initial_delay_seconds: int = 1
    reconnect_max_delay_seconds: int = 60
    schedule_timezone: str = "Asia/Shanghai"
    schedule_breaks: List[ScheduleBreak] = field(default_factory=list)
    challenge_url: str = "http://127.0.0.1:1002/challenge"
    verify_url: str = "http://127.0.0.1:1002/verify"
    fallback_question: str = "密码"
    fallback_answer: str = "change-me"

    def resolved_client_ws_url(self) -> str:
        return self.server_ws_url

    def break_time_ranges(self) -> List[Tuple[time, time]]:
        return [item.as_time_range() for item in self.schedule_breaks]

    @property
    def prefers_classisland(self) -> bool:
        """True when live ClassIsland events should be preferred."""
        return self.schedule_mode != "local"


def load_client_config(config_path: Optional[Path] = None) -> ClientConfig:
    path = _resolve_path(config_path, CLIENT_CONFIG_ENV_VAR, CLIENT_CONFIG_PATH)
    if not path.exists():
        return ClientConfig()

    raw = _load_toml(path)
    section = raw.get("client", {})
    sched = raw.get("schedule", {})
    challenge = raw.get("challenge", {})

    breaks = [
        ScheduleBreak(
            name=item.get("name", f"break_{idx}"),
            start=item.get("start", ""),
            end=item.get("end", ""),
        )
        for idx, item in enumerate(sched.get("breaks", []), start=1)
    ]

    return ClientConfig(
        internal_token=_opt_str(raw.get("internal_token"), ClientConfig().internal_token),
        server_ws_url=_opt_str(raw.get("server_ws_url"), ClientConfig().server_ws_url),
        log_level=_opt_str(raw.get("log_level"), ClientConfig().log_level),
        client_name=_opt_str(section.get("client_name"), ClientConfig().client_name),
        schedule_source=_none_if_empty(section.get("schedule_source")),
        last_valid_schedule_source=_none_if_empty(section.get("last_valid_schedule_source")),
        schedule_mode=_opt_str(section.get("schedule_mode"), ClientConfig().schedule_mode),
        break_popup_delay_seconds=int(_opt_str(
            section.get("break_popup_delay_seconds"), ClientConfig().break_popup_delay_seconds,
        )),
        cib_exe_path=_opt_str(section.get("cib_exe_path"), ClientConfig().cib_exe_path),
        use_cib_schedule=bool(section.get("use_cib_schedule", ClientConfig().use_cib_schedule)),
        classisland_ws_url=_opt_str(section.get("classisland_ws_url"), ClientConfig().classisland_ws_url),
        ntp_server=_opt_str(section.get("ntp_server"), ClientConfig().ntp_server),
        auto_popup_on_break=bool(section.get("auto_popup_on_break", ClientConfig().auto_popup_on_break)),
        close_to_tray=bool(section.get("close_to_tray", ClientConfig().close_to_tray)),
        enable_urgent_sound=bool(section.get("enable_urgent_sound", ClientConfig().enable_urgent_sound)),
        history_retention_days=int(_opt_str(section.get("history_retention_days"), ClientConfig().history_retention_days)),
        urgent_remind_default_minutes=int(_opt_str(
            section.get("urgent_remind_default_minutes"), ClientConfig().urgent_remind_default_minutes,
        )),
        reconnect_initial_delay_seconds=int(_opt_str(
            section.get("reconnect_initial_delay_seconds"), ClientConfig().reconnect_initial_delay_seconds,
        )),
        reconnect_max_delay_seconds=int(_opt_str(
            section.get("reconnect_max_delay_seconds"), ClientConfig().reconnect_max_delay_seconds,
        )),
        schedule_timezone=_opt_str(sched.get("timezone"), ClientConfig().schedule_timezone),
        schedule_breaks=breaks,
        challenge_url=_opt_str(challenge.get("challenge_url"), ClientConfig().challenge_url),
        verify_url=_opt_str(challenge.get("verify_url"), ClientConfig().verify_url),
        fallback_question=_opt_str(challenge.get("fallback_question"), ClientConfig().fallback_question),
        fallback_answer=_opt_str(challenge.get("fallback_answer"), ClientConfig().fallback_answer),
    )


def save_client_config(config: ClientConfig, config_path: Optional[Path] = None) -> Path:
    path = _resolve_path(config_path, CLIENT_CONFIG_ENV_VAR, CLIENT_CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump_client_toml(config), encoding="utf-8")
    return path


def _dump_client_toml(config: ClientConfig) -> str:
    lines = [
        f'internal_token = "{_esc(config.internal_token)}"',
        f'server_ws_url = "{_esc(config.server_ws_url)}"',
        f'log_level = "{_esc(config.log_level)}"',
        "",
        "[client]",
        f'client_name = "{_esc(config.client_name)}"',
    ]
    if config.schedule_source:
        lines.append(f'schedule_source = "{_esc(config.schedule_source)}"')
    if config.last_valid_schedule_source:
        lines.append(f'last_valid_schedule_source = "{_esc(config.last_valid_schedule_source)}"')
    lines.extend([
        f'schedule_mode = "{_esc(config.schedule_mode)}"',
        f"break_popup_delay_seconds = {config.break_popup_delay_seconds}",
        f'cib_exe_path = "{_esc(config.cib_exe_path)}"',
        f"use_cib_schedule = {_bool_str(config.use_cib_schedule)}",
        f'classisland_ws_url = "{_esc(config.classisland_ws_url)}"',
        f'ntp_server = "{_esc(config.ntp_server)}"',
        f"auto_popup_on_break = {_bool_str(config.auto_popup_on_break)}",
        f"close_to_tray = {_bool_str(config.close_to_tray)}",
        f"enable_urgent_sound = {_bool_str(config.enable_urgent_sound)}",
        f"history_retention_days = {config.history_retention_days}",
        f"urgent_remind_default_minutes = {config.urgent_remind_default_minutes}",
        f"reconnect_initial_delay_seconds = {config.reconnect_initial_delay_seconds}",
        f"reconnect_max_delay_seconds = {config.reconnect_max_delay_seconds}",
        "",
        "[schedule]",
        f'timezone = "{_esc(config.schedule_timezone)}"',
    ])
    for item in config.schedule_breaks:
        lines.extend([
            "",
            "[[schedule.breaks]]",
            f'name = "{_esc(item.name)}"',
            f'start = "{_esc(item.start)}"',
            f'end = "{_esc(item.end)}"',
        ])
    lines.extend([
        "",
        "[challenge]",
        f'challenge_url = "{_esc(config.challenge_url)}"',
        f'verify_url = "{_esc(config.verify_url)}"',
        f'fallback_question = "{_esc(config.fallback_question)}"',
        f'fallback_answer = "{_esc(config.fallback_answer)}"',
        "",
    ])
    return "\n".join(lines)


# ===========================================================================
# Plugin TOML config (used by NoneBot plugin to load plugin.toml)
# ===========================================================================

PLUGIN_CONFIG_ENV_VAR = "KG_PLUGIN_CONFIG"


@dataclass
class PluginTomlConfig:
    internal_token: str = "dev-internal-token"
    server_ws_url: str = "ws://127.0.0.1:8765/ws/plugin"
    bot_name: str = "kgGao29MessageBot"
    class_group_ids: List[int] = field(default_factory=list)
    admin_users: List[int] = field(default_factory=list)
    short_id_ttl_seconds: int = 300
    ai_enabled: bool = True
    ai_api_key: str = ""
    ai_api_url: str = "https://api.deepseek.com/beta"
    ai_model: str = "deepseek-flash"
    # --- provided by the unified config.toml -----------------------------
    active_env: str = "dev"
    debug_mode: bool = False


def _plugin_config_from_unified(unified: Dict[str, Any]) -> PluginTomlConfig:
    """Build :class:`PluginTomlConfig` from the flattened unified config.

    ``class_group_ids`` / ``admin_users`` come from the currently active
    environment, which is what makes the dev/prod switch work for the plugin.
    """
    defaults = PluginTomlConfig()
    section = unified.get("plugin") if isinstance(unified.get("plugin"), dict) else {}
    ai_section = unified.get("ai") if isinstance(unified.get("ai"), dict) else {}

    return PluginTomlConfig(
        internal_token=_opt_str(unified.get("internal_token"), defaults.internal_token),
        server_ws_url=derive_plugin_ws_url(unified),
        bot_name=_opt_str(section.get("bot_name"), defaults.bot_name),
        class_group_ids=_int_list(unified.get("class_group_ids", [])),
        admin_users=_int_list(unified.get("admin_users", [])),
        short_id_ttl_seconds=int(unified.get("short_id_ttl_seconds") or defaults.short_id_ttl_seconds),
        ai_enabled=bool(ai_section.get("enabled", defaults.ai_enabled)),
        ai_api_key=_opt_str(ai_section.get("api_key"), defaults.ai_api_key),
        ai_api_url=_opt_str(ai_section.get("api_url"), defaults.ai_api_url),
        ai_model=_opt_str(ai_section.get("model"), defaults.ai_model),
        active_env=_opt_str(unified.get("active_env"), defaults.active_env),
        debug_mode=bool(unified.get("debug_mode", defaults.debug_mode)),
    )


def load_plugin_toml_config(config_path: Optional[Path] = None) -> PluginTomlConfig:
    """Load the NoneBot plugin settings.

    Reads the unified ``config.toml`` (active environment applied) when
    available, otherwise falls back to a legacy ``plugin.toml``.
    """
    if config_path is None:
        unified = load_unified_config()
        if unified is not None:
            return _plugin_config_from_unified(unified)

    path = _resolve_path(config_path, PLUGIN_CONFIG_ENV_VAR, PLUGIN_CONFIG_PATH)
    if not path.exists():
        if config_path is None:
            logger.warning(
                "未找到 %s 且不存在旧版 plugin.toml，使用默认插件配置。", CONFIG_PATH
            )
        return PluginTomlConfig()

    logger.info("使用旧版配置文件 %s（建议迁移到统一的 config.toml）", path)
    raw = _load_toml(path)
    section = raw.get("plugin", {})
    ai_section = raw.get("ai", {})

    return PluginTomlConfig(
        internal_token=_opt_str(raw.get("internal_token"), PluginTomlConfig().internal_token),
        server_ws_url=_opt_str(raw.get("server_ws_url"), PluginTomlConfig().server_ws_url),
        bot_name=_opt_str(section.get("bot_name"), PluginTomlConfig().bot_name),
        class_group_ids=_int_list(section.get("class_group_ids", [])),
        admin_users=_int_list(section.get("admin_users", [])),
        short_id_ttl_seconds=int(_opt_str(
            section.get("short_id_ttl_seconds"), PluginTomlConfig().short_id_ttl_seconds,
        )),
        ai_enabled=bool(ai_section.get("enabled", PluginTomlConfig().ai_enabled)),
        ai_api_key=_opt_str(ai_section.get("api_key"), PluginTomlConfig().ai_api_key),
        ai_api_url=_opt_str(ai_section.get("api_url"), PluginTomlConfig().ai_api_url),
        ai_model=_opt_str(ai_section.get("model"), PluginTomlConfig().ai_model),
    )


# ===========================================================================
# Internal helpers
# ===========================================================================

def _resolve_path(explicit: Optional[Path], env_var: str, default_path: Path) -> Path:
    if explicit is not None:
        return Path(explicit)
    env_val = os.environ.get(env_var)
    if env_val:
        return Path(env_val)
    return default_path


def _load_toml(path: Path) -> Dict[str, Any]:
    if toml_loader is None:
        raise RuntimeError(
            "Reading TOML config requires tomli on Python 3.9. "
            "Install dependencies from requirements.txt first."
        )
    with path.open("rb") as fh:
        return toml_loader.load(fh)


def _opt_str(value: Any, default: Any) -> str:
    if value is None or value == "":
        return str(default)
    return str(value)


def _none_if_empty(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    return str(value)


def _int_list(value: Any) -> List[int]:
    if not value:
        return []
    return [int(item) for item in value]


def _norm_ws(value: str) -> str:
    if not value:
        return "/"
    return value if value.startswith("/") else f"/{value}"


def _parse_clock_time(value: str) -> time:
    hour_text, minute_text = value.split(":", 1)
    return time(hour=int(hour_text), minute=int(minute_text))


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _bool_str(value: bool) -> str:
    return "true" if value else "false"
