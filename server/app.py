from __future__ import annotations

import argparse
import asyncio
import contextlib
from json import dumps
from pathlib import Path
from typing import Any, Dict, Optional, Set

import websockets
from websockets.server import WebSocketServerProtocol

from shared.config import load_server_config
from shared.config_manager import get_config_manager
from shared.logging_utils import configure_logging
from shared.protocol import MessageType, parse_envelope_json

from .database import Database
from .message_server import AdminApiServer, build_admin_api
from .service import ServerService
from .short_id import ShortIdStore

_SERVER_DIR = Path(__file__).resolve().parent


class ServerApplication:
    def __init__(self) -> None:
        # Unified config.toml (active environment applied); legacy server.toml
        # is only used when config.toml is missing.
        self.config = load_server_config()
        self.logger = configure_logging("kg.server", "server.log", self.config.log_level)
        self.database = Database(self.config)
        self.short_ids = ShortIdStore()
        self.service = ServerService(self.config, self.database, self.short_ids)
        # The currently-active client WebSocket connection.  Only the connection
        # that is still active when it closes is allowed to mark the client
        # offline — a stale connection whose ``finally`` runs after a reconnect
        # must NOT overwrite the new connection's online status (the classic
        # "client looks offline but still receives messages" race).
        self._active_client_ws: Optional[WebSocketServerProtocol] = None
        # Live plugin connections, used to broadcast configuration updates.
        self._plugin_sockets: Set[WebSocketServerProtocol] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._admin_api: Optional[AdminApiServer] = None  # set in serve_forever

    def initialize(self) -> None:
        self.database.initialize()
        self.logger.info("Server bootstrap complete")
        self.logger.info(
            "Active environment: %s (%s)",
            self.config.active_env,
            "debug" if self.config.debug_mode else "normal",
        )
        self.logger.info("Database path configured as %s", self.database.database_path)

    # ------------------------------------------------------------------
    # configuration hot-reload (driven by the admin API)
    # ------------------------------------------------------------------

    def apply_config(self, new_config) -> None:
        """Apply a freshly saved configuration to the running services."""
        old = self.config
        self.config = new_config
        self.service.update_config(new_config)

        self.logger.info(
            "Config hot-reloaded: active_env=%s, token=%s…, short_id_ttl=%ss",
            new_config.active_env,
            str(new_config.internal_token)[:4],
            new_config.short_id_ttl_seconds,
        )
        if (old.host, old.port) != (new_config.host, new_config.port):
            self.logger.warning(
                "server_host/server_port 已变更（%s:%s → %s:%s），需重启服务端才能生效",
                old.host,
                old.port,
                new_config.host,
                new_config.port,
            )
        if old.database_path != new_config.database_path:
            self.logger.warning("database_path 已变更，需重启服务端才能生效")

    def on_config_saved(self, saved: Dict[str, Any]) -> None:
        """Called from the Flask thread after a successful save.

        Reloads the configuration and asks the asyncio loop to broadcast a
        ``config_updated`` notification to every connected plugin.
        """
        try:
            reloaded = load_server_config()
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.exception("配置重载失败：%s", exc)
            return

        self.apply_config(reloaded)

        revision = get_config_manager().revision
        loop = self._loop
        if loop is None or loop.is_closed():
            self.logger.info("事件循环未就绪，跳过 Plugin 配置广播")
            return

        payload = {
            "type": "config_updated",
            "revision": revision,
            "active_env": reloaded.active_env,
            "message": "服务端配置已更新，插件可重新读取 config.toml",
        }
        asyncio.run_coroutine_threadsafe(self._broadcast_to_plugins(payload), loop)

    async def _broadcast_to_plugins(self, message: Dict[str, Any]) -> None:
        """Push *message* to every connected plugin (best effort)."""
        if not self._plugin_sockets:
            self.logger.info("没有在线的 Plugin 连接，跳过广播：%s", message.get("type"))
            return

        text = dumps(message, ensure_ascii=False, separators=(",", ":"))
        delivered = 0
        for websocket in list(self._plugin_sockets):
            try:
                await websocket.send(text)
                delivered += 1
            except Exception as exc:
                self.logger.warning("向 Plugin 广播失败，移除该连接：%s", exc)
                self._plugin_sockets.discard(websocket)
        self.logger.info("已向 %s 个 Plugin 连接广播配置更新", delivered)

    async def serve_forever(self) -> None:
        self.initialize()
        self._loop = asyncio.get_event_loop()

        # Admin console API runs beside the WebSocket server, in its own thread.
        self._admin_api = build_admin_api(self.config, on_config_saved=self.on_config_saved)
        self._admin_api.start()

        async with websockets.serve(
            self._handle_connection,
            self.config.host,
            self.config.port,
            ping_interval=20,
            ping_timeout=20,
            max_size=2**20,
        ):
            self.logger.info(
                "WebSocket server listening on ws://%s:%s",
                self.config.host,
                self.config.port,
            )
            await asyncio.Future()

    async def _handle_connection(self, websocket: WebSocketServerProtocol) -> None:
        path = getattr(websocket, "path", "")
        self.logger.info("Incoming WebSocket connection: %s", path)
        client_name: Optional[str] = None
        is_plugin = path == self.config.plugin_ws_path
        if is_plugin:
            self._plugin_sockets.add(websocket)

        try:
            async for raw_message in websocket:
                envelope = parse_envelope_json(raw_message)
                if not self._is_authorized(path, envelope.auth_token):
                    await websocket.send(
                        _json_response(self.service._error_response("INTERNAL_TOKEN 校验失败。", envelope.request_id))
                    )
                    continue

                if is_plugin:
                    response = self.service.handle_plugin_request(envelope.type, envelope.data, envelope.request_id)
                elif path == self.config.client_ws_path:
                    client_name = str(envelope.data.get("client_name") or self.config.client_name)
                    # Mark this connection as the active client connection as
                    # soon as we see a valid authenticated message on it, so
                    # that a stale connection can never mark us offline later.
                    self._active_client_ws = websocket
                    response = self.service.handle_client_request(envelope.type, envelope.data, envelope.request_id)
                else:
                    response = self.service._error_response(
                        f"Unsupported WebSocket path: {path}",
                        envelope.request_id,
                    )

                await websocket.send(_json_response(response))
        except websockets.ConnectionClosed:
            self.logger.info("WebSocket disconnected: %s", path)
        except Exception:
            self.logger.exception("Unhandled error while processing WebSocket connection: %s", path)
            with contextlib.suppress(Exception):
                await websocket.send(_json_response(self.service._error_response("Server internal error.", None)))
        finally:
            if is_plugin:
                self._plugin_sockets.discard(websocket)
            if path == self.config.client_ws_path and self._active_client_ws is websocket:
                # Only the connection that is *currently* the active client may
                # mark the client offline.  If a newer connection already took
                # over (client reconnected), this stale connection must not
                # clobber its online status.
                self._active_client_ws = None
                self.service.mark_client_offline(client_name)

    def smoke_test(self) -> int:
        self.initialize()
        response = self.service.handle_plugin_request(MessageType.HEARTBEAT.value, {}, request_id="smoke-test")
        self.logger.info("Server smoke test passed with response type %s", response["type"])
        self.logger.info(
            "Admin console API would listen on http://%s:%s（控制台：admin/admin.html）",
            self.config.admin_host,
            self.config.admin_port,
        )
        return 0

    def _is_authorized(self, path: str, auth_token: Optional[str]) -> bool:
        if path not in {self.config.plugin_ws_path, self.config.client_ws_path}:
            return False
        return auth_token == self.config.internal_token


def main() -> int:
    parser = argparse.ArgumentParser(description="Class message relay server")
    parser.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()

    app = ServerApplication()
    if args.smoke_test:
        return app.smoke_test()

    try:
        asyncio.run(app.serve_forever())
    except KeyboardInterrupt:
        app.logger.info("Server shutdown requested by user")
    return 0


def _json_response(payload: dict) -> str:
    return dumps(payload, ensure_ascii=False, separators=(",", ":"))


import contextlib
