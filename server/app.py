from __future__ import annotations

import argparse
import asyncio
from json import dumps
from pathlib import Path
from typing import Optional

import websockets
from websockets.server import WebSocketServerProtocol

from shared.config import load_server_config
from shared.logging_utils import configure_logging
from shared.protocol import MessageType, parse_envelope_json

from .database import Database
from .service import ServerService
from .short_id import ShortIdStore

_SERVER_DIR = Path(__file__).resolve().parent


class ServerApplication:
    def __init__(self) -> None:
        self.config = load_server_config(_SERVER_DIR / "server.toml")
        self.logger = configure_logging("kg.server", "server.log", self.config.log_level)
        self.database = Database(self.config)
        self.short_ids = ShortIdStore()
        self.service = ServerService(self.config, self.database, self.short_ids)

    def initialize(self) -> None:
        self.database.initialize()
        self.logger.info("Server bootstrap complete")
        self.logger.info("Database path configured as %s", self.database.database_path)

    async def serve_forever(self) -> None:
        self.initialize()
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

        try:
            async for raw_message in websocket:
                envelope = parse_envelope_json(raw_message)
                if not self._is_authorized(path, envelope.auth_token):
                    await websocket.send(
                        _json_response(self.service._error_response("INTERNAL_TOKEN 校验失败。", envelope.request_id))
                    )
                    continue

                if path == self.config.plugin_ws_path:
                    response = self.service.handle_plugin_request(envelope.type, envelope.data, envelope.request_id)
                elif path == self.config.client_ws_path:
                    client_name = str(envelope.data.get("client_name") or self.config.client_name)
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
            if path == self.config.client_ws_path:
                self.service.mark_client_offline(client_name)

    def smoke_test(self) -> int:
        self.initialize()
        response = self.service.handle_plugin_request(MessageType.HEARTBEAT.value, {}, request_id="smoke-test")
        self.logger.info("Server smoke test passed with response type %s", response["type"])
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
