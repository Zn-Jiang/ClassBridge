from __future__ import annotations

from typing import Any, Dict

import websockets

from shared.protocol import MessageType, envelope_to_json, make_envelope, parse_envelope_json

from .config import Config


class ServerApiError(RuntimeError):
    pass


async def send_request(config: Config, message_type: MessageType, data: Dict[str, Any]) -> Dict[str, Any]:
    envelope = make_envelope(message_type, data=data, auth_token=config.internal_token)
    try:
        async with websockets.connect(config.server_ws_url, max_size=2**20) as websocket:
            await websocket.send(envelope_to_json(envelope))
            raw_response = await websocket.recv()
    except Exception as exc:
        raise ServerApiError(f"无法连接消息服务器：{exc}") from exc

    response = parse_envelope_json(raw_response)
    payload = dict(response.data)
    payload["type"] = response.type
    payload["request_id"] = response.request_id
    if response.type == MessageType.ERROR.value:
        raise ServerApiError(payload.get("message", "消息服务器返回了未知错误。"))
    return payload
