"""Flask admin API for ClassBridge (system configuration management).

Serves the local single-file console (``admin/admin.html``):

* ``GET  /api/sys_mgmt/config`` — returns the whole ``config.toml`` structure
  (including ``admin_secret_token``, which the console needs to authenticate);
* ``POST /api/sys_mgmt/config`` — writes the submitted configuration back to
  ``config.toml``, hot-reloads it in memory and notifies connected plugins.

Security model
--------------
Every ``/api/sys_mgmt/*`` route requires the ``X-Admin-Token`` header to match
``admin_secret_token`` from ``config.toml``; anything else gets ``403``.  The
comparison uses :func:`hmac.compare_digest` to avoid timing leaks.

CORS is enabled so the console works when opened straight from disk
(``file://``, whose origin is ``null``).  ``flask_cors`` is used when
installed; otherwise an equivalent built-in implementation takes over, so the
service still runs on machines where the extra package is unavailable.

The API runs in its own thread next to the asyncio WebSocket server; the
``on_config_saved`` hook is how a successful save reaches the asyncio side for
hot-reloading and plugin broadcasts.
"""

from __future__ import annotations

import hmac
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request

from shared.config import ServerConfig, load_server_config
from shared.config_manager import ConfigManager, get_config_manager
from shared.paths import LOG_DIR

logger = logging.getLogger("kg.server.admin_api")

#: Routes protected by the admin token.
_PROTECTED_PREFIX = "/api/sys_mgmt/"

#: Settings that only take effect after restarting the server.
_RESTART_ONLY_KEYS = ("server_host", "server_port", "admin_host", "admin_port")

#: Log levels the console may filter on.
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
#: Upper bound on bytes read from the end of the log file (keeps the endpoint
#: cheap even when the file has grown for months).
_LOG_TAIL_BYTES = 512 * 1024

ConfigSavedHook = Callable[[Dict[str, Any]], None]


# ---------------------------------------------------------------------------
# werkzeug / flask compatibility
# ---------------------------------------------------------------------------


def patch_werkzeug_version() -> None:
    """Restore ``werkzeug.__version__`` when running flask < 2.3.

    Werkzeug 3.x removed that module attribute, but Flask 2.2 still reads it in
    ``flask.testing`` (``app.test_client()``) and in some CLI paths, raising
    ``AttributeError``.  ``app.run()`` is unaffected, so this is purely about
    keeping the test client usable on machines that cannot upgrade Flask.
    """
    try:
        import werkzeug  # type: ignore
    except ImportError:  # pragma: no cover - flask depends on it
        return
    if hasattr(werkzeug, "__version__"):
        return

    version = "3.0.0"
    try:
        from importlib.metadata import version as _dist_version

        version = _dist_version("werkzeug")
    except Exception:  # pragma: no cover - defensive
        pass
    werkzeug.__version__ = version  # type: ignore[attr-defined]
    logger.debug("已为 flask<2.3 补回 werkzeug.__version__=%s", version)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


def install_cors(app: Flask) -> str:
    """Enable cross-origin access; returns which implementation was used.

    Tries ``flask_cors`` first (the documented approach) and falls back to a
    minimal built-in handler — including ``OPTIONS`` pre-flight support —
    when the package is missing.
    """
    try:
        from flask_cors import CORS  # type: ignore

        CORS(
            app,
            resources={r"/api/*": {"origins": "*"}},
            allow_headers=["Content-Type", "X-Admin-Token"],
            methods=["GET", "POST", "OPTIONS"],
            max_age=600,
        )
        logger.info("CORS 已启用（flask_cors）")
        return "flask_cors"
    except ImportError:
        logger.info("未安装 flask_cors，改用内置 CORS 实现（功能等价）")

    @app.after_request
    def _add_cors_headers(response):  # type: ignore[unused-ignore]
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Token"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "600"
        return response

    @app.before_request
    def _answer_preflight():  # type: ignore[unused-ignore]
        if request.method == "OPTIONS":
            return ("", 204)
        return None

    return "builtin"


# ---------------------------------------------------------------------------
# log tail parsing
# ---------------------------------------------------------------------------

#: ``shared.logging_utils`` writes ``%(asctime)s | %(levelname)s | %(name)s | %(message)s``.
_LOG_LINE_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| "
    r"(?P<level>[A-Z]+) \| "
    r"(?P<name>[^|]+?) \| "
    r"(?P<message>.*)$"
)


def log_file_path() -> Path:
    """The server log the admin console tails."""
    return Path(LOG_DIR) / "server.log"


def parse_log_text(text: str) -> List[Dict[str, str]]:
    """Parse log text into entries (newest **first**).

    Continuation lines (tracebacks, multi-line messages) are appended to the
    entry they belong to instead of being dropped.
    """
    entries: List[Dict[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            continue
        match = _LOG_LINE_RE.match(line)
        if match is None:
            if entries:
                entries[-1]["message"] += "\n" + line
            continue
        entries.append(
            {
                "time": match.group("time"),
                "level": match.group("level").upper(),
                "name": match.group("name").strip(),
                "message": match.group("message"),
            }
        )
    entries.reverse()
    return entries


def read_log_entries(
    *,
    limit: int = 300,
    levels: Optional[List[str]] = None,
    search: Optional[str] = None,
    path: Optional[Path] = None,
) -> tuple:
    """Read the log tail, filter it and return ``(entries, path, truncated)``."""
    target = Path(path) if path is not None else log_file_path()
    if not target.is_file():
        return [], target, False

    size = target.stat().st_size
    truncated = size > _LOG_TAIL_BYTES
    with target.open("r", encoding="utf-8", errors="replace") as handle:
        if truncated:
            handle.seek(size - _LOG_TAIL_BYTES)
            handle.readline()  # drop the partial first line
        text = handle.read()

    entries = parse_log_text(text)

    wanted = {level.upper() for level in levels} if levels else None
    needle = search.lower() if search else None
    selected: List[Dict[str, str]] = []
    for entry in entries:
        if wanted is not None and entry["level"] not in wanted:
            continue
        if needle is not None:
            haystack = f"{entry['name']} {entry['message']}".lower()
            if needle not in haystack:
                continue
        selected.append(entry)
        if len(selected) >= limit:
            break
    return selected, target, truncated


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------


def create_admin_app(
    config_manager: Optional[ConfigManager] = None,
    *,
    startup_config: Optional[ServerConfig] = None,
    on_config_saved: Optional[ConfigSavedHook] = None,
    cors_mode: str = "auto",
) -> Flask:
    """Build the Flask application.

    Args:
        config_manager: configuration store; defaults to the shared singleton.
        startup_config: configuration the process started with — used to report
            which changes require a restart.
        on_config_saved: invoked after a successful save with the new config,
            so the asyncio side can hot-reload and broadcast to plugins.
        cors_mode: ``"auto"`` (default), ``"flask_cors"`` or ``"builtin"``.
    """
    manager = config_manager or get_config_manager()
    app = Flask("classbridge_admin")
    app.url_map.strict_slashes = False
    patch_werkzeug_version()

    if cors_mode in ("auto", "flask_cors"):
        used = install_cors(app)
        if cors_mode == "flask_cors" and used != "flask_cors":
            raise RuntimeError("flask_cors 不可用，无法满足 cors_mode='flask_cors'")

    # ------------------------------------------------------------------
    # auth middleware
    # ------------------------------------------------------------------

    @app.before_request
    def _require_admin_token():  # type: ignore[unused-ignore]
        if request.method == "OPTIONS" or not request.path.startswith(_PROTECTED_PREFIX):
            return None

        expected = manager.get_admin_token()
        if not expected:
            logger.error("config.toml 未设置 admin_secret_token，拒绝所有管理请求")
            return jsonify({"ok": False, "error": "服务端未配置 admin_secret_token"}), 403

        provided = request.headers.get("X-Admin-Token", "")
        if not provided or not hmac.compare_digest(provided, expected):
            logger.warning("管理接口鉴权失败：来自 %s 的请求令牌无效", request.remote_addr)
            return jsonify({"ok": False, "error": "管理员令牌无效（X-Admin-Token）"}), 403
        return None

    # ------------------------------------------------------------------
    # GET /api/sys_mgmt/config
    # ------------------------------------------------------------------

    @app.get("/api/sys_mgmt/config")
    def get_config():  # type: ignore[unused-ignore]
        try:
            config = manager.get_current_config(force=True)
        except Exception as exc:
            logger.exception("读取配置失败")
            return jsonify({"ok": False, "error": f"读取配置失败：{exc}"}), 500

        return jsonify(
            {
                "ok": True,
                "config": config,
                "revision": manager.revision,
                "path": str(manager.path),
                "environments": sorted((config.get("environments") or {}).keys()),
            }
        )

    # ------------------------------------------------------------------
    # POST /api/sys_mgmt/config
    # ------------------------------------------------------------------

    @app.post("/api/sys_mgmt/config")
    def post_config():  # type: ignore[unused-ignore]
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400

        # Accept both {"config": {...}} and a bare config object.
        incoming = payload.get("config") if isinstance(payload.get("config"), dict) else payload

        try:
            saved = manager.save_and_reload_config(incoming)
        except Exception as exc:
            logger.warning("保存配置失败：%s", exc)
            return jsonify({"ok": False, "error": str(exc)}), 400

        restart_required = _restart_required(startup_config, saved)

        if on_config_saved is not None:
            try:
                on_config_saved(saved)
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("配置已保存，但热重载/广播钩子失败：%s", exc)

        logger.info(
            "配置已更新并热重载（revision=%s, active_env=%s, 需重启=%s）",
            manager.revision,
            saved.get("active_env"),
            restart_required or "无",
        )
        return jsonify(
            {
                "ok": True,
                "message": "配置已保存并热重载。",
                "config": saved,
                "revision": manager.revision,
                "restart_required": restart_required,
            }
        )

    # ------------------------------------------------------------------
    # GET /api/sys_mgmt/logs
    # ------------------------------------------------------------------

    @app.get("/api/sys_mgmt/logs")
    def get_logs():  # type: ignore[unused-ignore]
        """Tail of the server log, newest entry first.

        Query parameters:
            limit:  maximum number of entries (default 300, max 2000)
            levels: comma-separated level names to keep (default: all)
            search: case-insensitive substring filter on the message
        """
        limit_raw = request.args.get("limit", "300")
        try:
            limit = max(1, min(2000, int(limit_raw)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "limit 必须是整数"}), 400

        levels = [
            item.strip().upper()
            for item in (request.args.get("levels") or "").split(",")
            if item.strip()
        ]
        invalid = [level for level in levels if level not in LOG_LEVELS]
        if invalid:
            return jsonify({"ok": False, "error": f"未知日志等级：{', '.join(invalid)}"}), 400

        search = (request.args.get("search") or "").strip()

        try:
            entries, path, truncated = read_log_entries(
                limit=limit, levels=levels or None, search=search or None
            )
        except Exception as exc:
            logger.exception("读取日志失败")
            return jsonify({"ok": False, "error": f"读取日志失败：{exc}"}), 500

        return jsonify(
            {
                "ok": True,
                "entries": entries,
                "count": len(entries),
                "path": str(path),
                "truncated": truncated,
                "available_levels": LOG_LEVELS,
            }
        )

    # ------------------------------------------------------------------
    # error handling
    # ------------------------------------------------------------------

    @app.errorhandler(404)
    def _not_found(_error):  # type: ignore[unused-ignore]
        return jsonify({"ok": False, "error": f"未知接口：{request.path}"}), 404

    @app.errorhandler(405)
    def _method_not_allowed(_error):  # type: ignore[unused-ignore]
        return jsonify({"ok": False, "error": f"方法不被允许：{request.method} {request.path}"}), 405

    @app.errorhandler(500)
    def _server_error(error):  # type: ignore[unused-ignore]
        logger.exception("管理接口内部错误：%s", error)
        return jsonify({"ok": False, "error": "服务端内部错误"}), 500

    return app


def _restart_required(
    startup_config: Optional[ServerConfig],
    new_config: Dict[str, Any],
) -> List[str]:
    """List settings whose change only takes effect after a restart."""
    if startup_config is None:
        return []
    pending: List[str] = []
    if str(startup_config.host) != str(new_config.get("server_host", startup_config.host)):
        pending.append("server_host")
    if int(startup_config.port) != int(new_config.get("server_port") or startup_config.port):
        pending.append("server_port")
    if str(startup_config.admin_host) != str(new_config.get("admin_host", startup_config.admin_host)):
        pending.append("admin_host")
    section = new_config.get("server") if isinstance(new_config.get("server"), dict) else {}
    new_admin_port = section.get("admin_port")
    if new_admin_port is not None and int(new_admin_port) != int(startup_config.admin_port):
        pending.append("admin_port")
    return pending


# ---------------------------------------------------------------------------
# server thread
# ---------------------------------------------------------------------------


class AdminApiServer:
    """Runs the Flask admin API in a daemon thread beside the WS server."""

    def __init__(
        self,
        app: Flask,
        host: str,
        port: int,
    ) -> None:
        self._app = app
        self._host = host
        self._port = port
        self._thread: Optional[threading.Thread] = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        display_host = "127.0.0.1" if self._host in {"0.0.0.0", "::", ""} else self._host
        return f"http://{display_host}:{self._port}"

    def start(self) -> None:
        """Start serving (no-op when already running)."""
        if self._thread is not None and self._thread.is_alive():
            return
        # Werkzeug logs every request; keep it quiet.
        logging.getLogger("werkzeug").setLevel(logging.WARNING)
        self._thread = threading.Thread(
            target=self._serve,
            name="classbridge-admin-api",
            daemon=True,
        )
        self._thread.start()
        logger.info("管理后台 API 已启动：%s/api/sys_mgmt/config", self.url)

    def _serve(self) -> None:
        try:
            self._app.run(
                host=self._host,
                port=self._port,
                threaded=True,
                debug=False,
                use_reloader=False,
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("管理后台 API 异常退出（%s:%s）", self._host, self._port)

    def stop(self, timeout: float = 2.0) -> None:
        """Best-effort shutdown (the thread is a daemon; Flask cannot be closed)."""
        thread = self._thread
        if thread is not None and thread.is_alive():
            logger.info("管理后台 API 线程仍在运行（daemon），随进程退出")


def build_admin_api(
    startup_config: Optional[ServerConfig] = None,
    *,
    config_manager: Optional[ConfigManager] = None,
    on_config_saved: Optional[ConfigSavedHook] = None,
) -> AdminApiServer:
    """Create the admin API server using the unified configuration."""
    manager = config_manager or get_config_manager()
    config = startup_config or load_server_config()
    app = create_admin_app(
        manager,
        startup_config=config,
        on_config_saved=on_config_saved,
    )
    return AdminApiServer(app, config.admin_host, config.admin_port)
