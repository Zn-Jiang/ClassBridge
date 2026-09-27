"""Unified configuration manager for the whole ClassBridge project.

A single ``config.toml`` now feeds both the message server and the NoneBot
plugin.  Its top level carries environment-independent settings plus one
``[environments.<name>]`` table per deployment target::

    active_env = "dev"
    internal_token = "…"
    admin_secret_token = "…"
    short_id_ttl_seconds = 300

    [environments.dev]
    env_name = "开发测试环境"
    server_host = "127.0.0.1"
    server_port = 8765
    class_group_ids = [825942382]
    admin_users = [123456789]
    debug_mode = true

    [server]   …   # fixed parameters shared by every environment
    [plugin]   …
    [ai]       …

:meth:`ConfigManager.get_current_config` flattens the active environment into
the returned mapping so callers get one ready-to-use dictionary::

    {
      "active_env": "dev",
      "internal_token": "…",
      "server_host": "127.0.0.1",     # from environments.dev
      "server_port": 8765,            # from environments.dev
      "class_group_ids": [...],       # from environments.dev
      "admin_users": [...],           # from environments.dev
      "debug_mode": True,             # from environments.dev
      "environments": {...},          # kept verbatim so the admin UI can edit both
      "server": {...},
      "plugin": {...},
      "ai": {...},
    }

The manager is thread-safe (the Flask admin API runs in its own thread while
the asyncio WebSocket server keeps running) and writes atomically so a crash
never truncates the live configuration.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import CONFIG_PATH, CONFIG_EXAMPLE_PATH

logger = logging.getLogger("kg.config")

#: Environment names the schema understands.
ENV_DEV = "dev"
ENV_PROD = "prod"
KNOWN_ENVS = (ENV_DEV, ENV_PROD)

#: Keys lifted out of ``[environments.<active_env>]`` into the flat view.
ENV_KEYS = (
    "env_name",
    "server_host",
    "server_port",
    "class_group_ids",
    "admin_users",
    "debug_mode",
)

#: Sections that are copied as-is into the flat view.
SECTION_KEYS = ("server", "plugin", "ai", "client")

_DEFAULT_ENV: Dict[str, Any] = {
    "env_name": "",
    "server_host": "127.0.0.1",
    "server_port": 8765,
    "class_group_ids": [],
    "admin_users": [],
    "debug_mode": False,
}


# ---------------------------------------------------------------------------
# TOML backend (stdlib tomllib on 3.11+, tomli on 3.9/3.10)
# ---------------------------------------------------------------------------
try:  # pragma: no cover - depends on interpreter version
    import tomllib as _toml_reader
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as _toml_reader  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover
        _toml_reader = None  # type: ignore


class ConfigError(RuntimeError):
    """Raised when ``config.toml`` cannot be read or validated."""


# ---------------------------------------------------------------------------
# TOML writer
# ---------------------------------------------------------------------------


def _toml_value(value: Any) -> str:
    """Render a Python value as a TOML literal."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if value is None:
        return '""'
    return _toml_value(str(value))


def dump_config_toml(config: Dict[str, Any]) -> str:
    """Serialize a config mapping back into readable TOML text.

    Scalars come first, then the ``[environments.*]`` tables, then the fixed
    ``[server]`` / ``[plugin]`` / ``[ai]`` sections — matching the documented
    layout of ``config.example.toml``.
    """
    lines: List[str] = []
    sections: List[tuple] = []
    env_tables: Dict[str, Dict[str, Any]] = {}

    for key, value in config.items():
        if key == "environments" and isinstance(value, dict):
            env_tables = {str(name): (table if isinstance(table, dict) else {}) for name, table in value.items()}
            continue
        if isinstance(value, dict):
            sections.append((key, value))
            continue
        lines.append(f"{key} = {_toml_value(value)}")

    # Environment tables, dev first then prod, then anything else.
    ordered_envs = [name for name in KNOWN_ENVS if name in env_tables]
    ordered_envs += [name for name in env_tables if name not in ordered_envs]
    for name in ordered_envs:
        lines.append("")
        lines.append(f"[environments.{name}]")
        for key, value in env_tables[name].items():
            lines.append(f"{key} = {_toml_value(value)}")

    for name, table in sections:
        lines.append("")
        lines.append(f"[{name}]")
        for key, value in table.items():
            lines.append(f"{key} = {_toml_value(value)}")

    lines.append("")
    return "\n".join(lines)


def _atomic_write(path: Path, text: str) -> None:
    """Write *text* to *path* atomically (temp file in the same directory)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp_name, str(path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


# ---------------------------------------------------------------------------
# manager
# ---------------------------------------------------------------------------


class ConfigManager:
    """Read, merge and persist the unified ``config.toml``.

    A single process-wide instance is shared through :func:`get_config_manager`.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else CONFIG_PATH
        self._lock = threading.RLock()
        self._raw: Dict[str, Any] = {}
        self._flat: Dict[str, Any] = {}
        self._mtime: Optional[float] = None
        self.revision = 0

    # ------------------------------------------------------------------
    # paths / metadata
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def exists(self) -> bool:
        return self._path.is_file()

    def file_mtime(self) -> Optional[float]:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return None

    # ------------------------------------------------------------------
    # loading
    # ------------------------------------------------------------------

    def load(self, *, force: bool = False) -> Dict[str, Any]:
        """Read ``config.toml`` and refresh the cached flat view."""
        with self._lock:
            mtime = self.file_mtime()
            if not force and self._raw and mtime is not None and mtime == self._mtime:
                return self._flat

            raw = self._read_file()
            self._raw = raw
            self._flat = flatten_config(raw)
            self._mtime = mtime
            self.revision += 1
            logger.info(
                "Loaded config from %s (active_env=%s, revision=%s)",
                self._path,
                self._flat.get("active_env"),
                self.revision,
            )
            return self._flat

    def _read_file(self) -> Dict[str, Any]:
        if _toml_reader is None:  # pragma: no cover - environment dependent
            raise ConfigError(
                "读取 config.toml 需要 tomli（Python 3.9/3.10）或 tomllib（3.11+），"
                "请先安装 requirements.txt 中的依赖。"
            )
        if not self._path.is_file():
            if CONFIG_EXAMPLE_PATH.is_file():
                raise ConfigError(
                    f"未找到配置文件 {self._path}，请先复制模板："
                    f"copy {CONFIG_EXAMPLE_PATH.name} config.toml"
                )
            raise ConfigError(f"未找到配置文件 {self._path}")
        try:
            with self._path.open("rb") as handle:
                data = _toml_reader.load(handle)
        except Exception as exc:
            raise ConfigError(f"解析 {self._path} 失败：{exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{self._path} 的内容不是 TOML 表")
        return data

    # ------------------------------------------------------------------
    # public accessors
    # ------------------------------------------------------------------

    def get_current_config(self, *, force: bool = False) -> Dict[str, Any]:
        """Return the merged configuration (active environment already applied)."""
        if not self._raw or force:
            return self.load(force=force)
        # Cheap change detection so long-running processes pick up edits.
        if self.file_mtime() != self._mtime:
            return self.load(force=True)
        with self._lock:
            return copy.deepcopy(self._flat)

    def get_raw_config(self) -> Dict[str, Any]:
        """Return the on-disk structure (with ``[environments.*]`` intact)."""
        self.load()
        with self._lock:
            return copy.deepcopy(self._raw)

    def get_active_env(self) -> str:
        return str(self.get_current_config().get("active_env") or ENV_DEV)

    def get_admin_token(self) -> str:
        return str(self.get_current_config().get("admin_secret_token") or "")

    def get_env_config(self, env: Optional[str] = None) -> Dict[str, Any]:
        """Return one environment table (defaults to the active one)."""
        name = env or self.get_active_env()
        environments = self.get_current_config().get("environments") or {}
        table = environments.get(name)
        if not isinstance(table, dict):
            logger.warning("Environment %r missing from %s; using defaults", name, self._path)
            return dict(_DEFAULT_ENV)
        merged = dict(_DEFAULT_ENV)
        merged.update(table)
        return merged

    # ------------------------------------------------------------------
    # saving
    # ------------------------------------------------------------------

    def save_and_reload_config(
        self,
        new_config: Dict[str, Any],
        *,
        apply_flat_env_keys: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Persist *new_config*, then refresh and return the merged view.

        The incoming mapping may be either the flat view produced by
        :meth:`get_current_config` (what the admin UI sends) or the raw
        on-disk structure.

        ``environments`` is authoritative: when the payload carries it, the
        flattened top-level keys (``class_group_ids``, ``admin_users``,
        ``server_host``, …) are **ignored**, because those describe whichever
        environment was active when the payload was read — writing them back
        after an environment switch would corrupt the new table.  Pass
        ``apply_flat_env_keys=True`` to force them onto the active environment,
        or call :meth:`update_env_config` to edit one environment explicitly.
        """
        if not isinstance(new_config, dict):
            raise ConfigError("配置必须是 JSON 对象")

        with self._lock:
            raw = copy.deepcopy(self._raw) if self._raw else self._read_file()

            incoming_envs = new_config.get("environments")
            env_tables_provided = isinstance(incoming_envs, dict) and bool(incoming_envs)
            if apply_flat_env_keys is None:
                apply_flat_env_keys = not env_tables_provided

            if env_tables_provided:
                raw["environments"] = _normalize_environments(incoming_envs, raw.get("environments"))
            else:
                raw.setdefault("environments", {})
                for name in KNOWN_ENVS:
                    raw["environments"].setdefault(name, dict(_DEFAULT_ENV))

            # --- top-level scalars ----------------------------------------
            for key, value in new_config.items():
                if key in ("environments",):
                    continue
                if key in SECTION_KEYS and isinstance(value, dict):
                    section = raw.get(key)
                    if not isinstance(section, dict):
                        section = {}
                    section.update(value)
                    raw[key] = section
                    continue
                if key in ENV_KEYS:
                    if not apply_flat_env_keys:
                        continue
                    target_env = str(new_config.get("active_env") or raw.get("active_env") or ENV_DEV)
                    table = raw["environments"].setdefault(target_env, dict(_DEFAULT_ENV))
                    table[key] = value
                    continue
                raw[key] = value

            self._validate(raw)
            text = dump_config_toml(raw)
            _atomic_write(self._path, text)

            self._raw = raw
            self._flat = flatten_config(raw)
            self._mtime = self.file_mtime()
            self.revision += 1
            logger.info(
                "Saved config to %s (active_env=%s, revision=%s)",
                self._path,
                self._flat.get("active_env"),
                self.revision,
            )
            return copy.deepcopy(self._flat)

    def update_env_config(self, env: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update fields of one environment table (``dev`` / ``prod``).

        The unambiguous way to change environment-scoped settings.
        """
        name = str(env or "").strip()
        if name not in KNOWN_ENVS:
            raise ConfigError(f"未知环境 {env!r}，仅支持 {', '.join(KNOWN_ENVS)}")
        if not isinstance(updates, dict):
            raise ConfigError("环境更新内容必须是 JSON 对象")

        with self._lock:
            raw = copy.deepcopy(self._raw) if self._raw else self._read_file()
            environments = raw.setdefault("environments", {})
            table = environments.setdefault(name, dict(_DEFAULT_ENV))
            if not isinstance(table, dict):
                table = dict(_DEFAULT_ENV)
                environments[name] = table
            table.update(updates)

            self._validate(raw)
            _atomic_write(self._path, dump_config_toml(raw))
            self._raw = raw
            self._flat = flatten_config(raw)
            self._mtime = self.file_mtime()
            self.revision += 1
            logger.info("Updated environments.%s (revision=%s)", name, self.revision)
            return copy.deepcopy(self._flat)

    def set_active_env(self, env: str) -> Dict[str, Any]:
        """Switch the active environment (all other tables stay untouched)."""
        name = str(env or "").strip()
        if name not in KNOWN_ENVS:
            raise ConfigError(f"未知环境 {env!r}，仅支持 {', '.join(KNOWN_ENVS)}")

        with self._lock:
            raw = copy.deepcopy(self._raw) if self._raw else self._read_file()
            if not isinstance(raw.get("environments"), dict) or name not in raw["environments"]:
                raise ConfigError(f"config.toml 中不存在 [environments.{name}]")
            raw["active_env"] = name

            self._validate(raw)
            _atomic_write(self._path, dump_config_toml(raw))
            self._raw = raw
            self._flat = flatten_config(raw)
            self._mtime = self.file_mtime()
            self.revision += 1
            logger.info("Active environment switched to %s (revision=%s)", name, self.revision)
            return copy.deepcopy(self._flat)

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate(raw: Dict[str, Any]) -> None:
        active = str(raw.get("active_env") or "")
        if active and active not in KNOWN_ENVS:
            raise ConfigError(f"active_env 必须是 {' / '.join(KNOWN_ENVS)} 之一，收到 {active!r}")
        environments = raw.get("environments")
        if environments is not None and not isinstance(environments, dict):
            raise ConfigError("environments 必须是 TOML 表")
        for name, table in (environments or {}).items():
            if not isinstance(table, dict):
                raise ConfigError(f"environments.{name} 必须是 TOML 表")
            for key in ("server_port",):
                if key in table and not _is_int_like(table[key]):
                    raise ConfigError(f"environments.{name}.{key} 必须是整数")
            for key in ("class_group_ids", "admin_users"):
                if key in table and not _is_int_list(table[key]):
                    raise ConfigError(f"environments.{name}.{key} 必须是整数数组")
        ttl = raw.get("short_id_ttl_seconds")
        if ttl is not None and not _is_int_like(ttl):
            raise ConfigError("short_id_ttl_seconds 必须是整数")


# ---------------------------------------------------------------------------
# flattening helpers
# ---------------------------------------------------------------------------


def flatten_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Merge ``[environments.<active_env>]`` into a single flat mapping."""
    flat: Dict[str, Any] = {}
    environments = raw.get("environments") if isinstance(raw.get("environments"), dict) else {}

    for key, value in raw.items():
        if key == "environments":
            continue
        if isinstance(value, dict):
            flat[key] = copy.deepcopy(value)
        else:
            flat[key] = value

    active = str(raw.get("active_env") or ENV_DEV)
    env_table = environments.get(active)
    if not isinstance(env_table, dict):
        logger.warning("active_env=%r not found in [environments]; falling back to defaults", active)
        env_table = {}
    merged_env = dict(_DEFAULT_ENV)
    merged_env.update({key: value for key, value in env_table.items() if not isinstance(value, dict)})
    for key in ENV_KEYS:
        flat[key] = merged_env.get(key)

    derived: Dict[str, Any] = {}
    for name, table in environments.items():
        if isinstance(table, dict):
            merged = dict(_DEFAULT_ENV)
            merged.update(table)
            derived[str(name)] = merged
    flat["environments"] = derived or {name: dict(_DEFAULT_ENV) for name in KNOWN_ENVS}
    return flat


def _normalize_environments(
    incoming: Dict[str, Any],
    existing: Any,
) -> Dict[str, Dict[str, Any]]:
    """Merge incoming environment tables with the existing ones."""
    base: Dict[str, Dict[str, Any]] = {}
    if isinstance(existing, dict):
        for name, table in existing.items():
            if isinstance(table, dict):
                base[str(name)] = copy.deepcopy(table)

    for name, table in incoming.items():
        if not isinstance(table, dict):
            continue
        target = base.setdefault(str(name), dict(_DEFAULT_ENV))
        target.update(table)

    if not base:
        base = {name: dict(_DEFAULT_ENV) for name in KNOWN_ENVS}
    return base


def _is_int_like(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value.is_integer()
    if isinstance(value, str):
        return value.strip().lstrip("-").isdigit()
    return False


def _is_int_list(value: Any) -> bool:
    if not isinstance(value, (list, tuple)):
        return False
    return all(_is_int_like(item) for item in value)


# ---------------------------------------------------------------------------
# process-wide singleton
# ---------------------------------------------------------------------------

_manager: Optional[ConfigManager] = None
_manager_lock = threading.Lock()


def get_config_manager(path: Optional[Path] = None) -> ConfigManager:
    """Return the shared :class:`ConfigManager` (created on first use)."""
    global _manager
    with _manager_lock:
        if _manager is None or (path is not None and Path(path) != _manager.path):
            _manager = ConfigManager(path)
        return _manager


def reset_config_manager() -> None:
    """Drop the cached manager (used by tests)."""
    global _manager
    with _manager_lock:
        _manager = None


def get_current_config() -> Dict[str, Any]:
    """Module-level shortcut for :meth:`ConfigManager.get_current_config`."""
    return get_config_manager().get_current_config()


def save_and_reload_config(new_config: Dict[str, Any]) -> Dict[str, Any]:
    """Module-level shortcut for :meth:`ConfigManager.save_and_reload_config`."""
    return get_config_manager().save_and_reload_config(new_config)
