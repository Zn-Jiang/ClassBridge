import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .paths import (
    CLIENT_CONFIG_PATH,
    CLIENT_EXAMPLE_CONFIG_PATH,
    PLUGIN_CONFIG_PATH,
    SERVER_CONFIG_PATH,
)

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


def load_server_config(config_path: Optional[Path] = None) -> ServerConfig:
    path = _resolve_path(config_path, SERVER_CONFIG_ENV_VAR, SERVER_CONFIG_PATH)
    if not path.exists():
        return ServerConfig()

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

    def resolved_client_ws_url(self) -> str:
        return self.server_ws_url

    def break_time_ranges(self) -> List[Tuple[time, time]]:
        return [item.as_time_range() for item in self.schedule_breaks]


def load_client_config(config_path: Optional[Path] = None) -> ClientConfig:
    path = _resolve_path(config_path, CLIENT_CONFIG_ENV_VAR, CLIENT_CONFIG_PATH)
    if not path.exists():
        return ClientConfig()

    raw = _load_toml(path)
    section = raw.get("client", {})
    sched = raw.get("schedule", {})

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
    lines.append("")
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


def load_plugin_toml_config(config_path: Optional[Path] = None) -> PluginTomlConfig:
    path = _resolve_path(config_path, PLUGIN_CONFIG_ENV_VAR, PLUGIN_CONFIG_PATH)
    if not path.exists():
        return PluginTomlConfig()

    raw = _load_toml(path)
    section = raw.get("plugin", {})

    return PluginTomlConfig(
        internal_token=_opt_str(raw.get("internal_token"), PluginTomlConfig().internal_token),
        server_ws_url=_opt_str(raw.get("server_ws_url"), PluginTomlConfig().server_ws_url),
        bot_name=_opt_str(section.get("bot_name"), PluginTomlConfig().bot_name),
        class_group_ids=_int_list(section.get("class_group_ids", [])),
        admin_users=_int_list(section.get("admin_users", [])),
        short_id_ttl_seconds=int(_opt_str(
            section.get("short_id_ttl_seconds"), PluginTomlConfig().short_id_ttl_seconds,
        )),
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
