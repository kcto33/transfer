from __future__ import annotations

import json
import mimetypes
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles


def get_runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_runtime_base_dir()
STATIC_DIR = BASE_DIR / "static"
MOBILE_HINTS = ("android", "iphone", "ipad", "ipod", "mobile", "windows phone")

# Some Windows machines register `.js` as `text/plain`, which breaks
# `<script type="module">` loading in browsers. Force stable MIME types.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


@dataclass
class ClientConnection:
    client_id: str
    display_name: str
    websocket: WebSocket
    room_code: str


class RoomHub:
    def __init__(self) -> None:
        self.rooms: dict[str, dict[str, ClientConnection]] = defaultdict(dict)

    async def join(
        self, room_code: str, client_id: str, display_name: str, websocket: WebSocket
    ) -> list[dict[str, str]]:
        room = self.rooms[room_code]
        peers = [
            {"client_id": peer.client_id, "display_name": peer.display_name}
            for peer in room.values()
        ]
        room[client_id] = ClientConnection(
            client_id=client_id,
            display_name=display_name,
            websocket=websocket,
            room_code=room_code,
        )
        return peers

    async def leave(self, room_code: str, client_id: str) -> None:
        room = self.rooms.get(room_code)
        if not room:
            return
        room.pop(client_id, None)
        if not room:
            self.rooms.pop(room_code, None)

    async def broadcast(
        self, room_code: str, payload: dict[str, Any], exclude_client_id: str | None = None
    ) -> None:
        room = self.rooms.get(room_code, {})
        disconnected: list[str] = []
        for client_id, connection in room.items():
            if exclude_client_id and client_id == exclude_client_id:
                continue
            try:
                await connection.websocket.send_text(json.dumps(payload))
            except Exception:
                disconnected.append(client_id)
        for client_id in disconnected:
            await self.leave(room_code, client_id)

    async def send_to(
        self, room_code: str, target_client_id: str, payload: dict[str, Any]
    ) -> bool:
        room = self.rooms.get(room_code, {})
        target = room.get(target_client_id)
        if target is None:
            return False
        try:
            await target.websocket.send_text(json.dumps(payload))
        except Exception:
            await self.leave(room_code, target_client_id)
            return False
        return True

    def get_room_size(self, room_code: str) -> int:
        return len(self.rooms.get(room_code, {}))


hub = RoomHub()
app = FastAPI(title="Web Transfer")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def is_mobile_request(request: Request) -> bool:
    user_agent = request.headers.get("user-agent", "").lower()
    return any(hint in user_agent for hint in MOBILE_HINTS)


def get_query_override(request: Request) -> str | None:
    view = request.query_params.get("view", "").strip().lower()
    if view in {"desktop", "mobile"}:
        return view
    return None


def build_redirect_target(path: str, request: Request) -> str:
    params = [(key, value) for key, value in request.query_params.multi_items() if key != "view"]
    if not params:
        return path
    query = urlencode(params)
    return f"{path}?{query}"


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/")
async def index(request: Request) -> Response:
    override = get_query_override(request)
    if override == "mobile" or (override is None and is_mobile_request(request)):
        return RedirectResponse(build_redirect_target("/m", request), status_code=307)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/m")
async def mobile_index(request: Request) -> Response:
    override = get_query_override(request)
    if override == "desktop":
        return RedirectResponse(build_redirect_target("/", request), status_code=307)
    return FileResponse(STATIC_DIR / "mobile.html")


@app.websocket("/ws/signaling")
async def signaling_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    room_code: str | None = None
    client_id: str | None = None

    try:
        while True:
            raw_message = await websocket.receive_text()
            message = json.loads(raw_message)
            message_type = message.get("type")

            if message_type == "join_room":
                room_code = str(message["room_code"]).strip()
                client_id = str(message["client_id"]).strip()
                display_name = str(message.get("display_name") or "Anonymous").strip()
                peers = await hub.join(room_code, client_id, display_name, websocket)
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "joined_room",
                            "room_code": room_code,
                            "client_id": client_id,
                            "peers": peers,
                        }
                    )
                )
                await hub.broadcast(
                    room_code,
                    {
                        "type": "peer_joined",
                        "room_code": room_code,
                        "client_id": client_id,
                        "display_name": display_name,
                    },
                    exclude_client_id=client_id,
                )
                continue

            if room_code is None or client_id is None:
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "error",
                            "message": "join_room is required before other messages.",
                        }
                    )
                )
                continue

            if message_type in {
                "webrtc_offer",
                "webrtc_answer",
                "ice_candidate",
                "transfer_request",
                "transfer_accept",
                "transfer_reject",
                "resume_request",
                "resume_state",
                "relay_payload",
            }:
                target_client_id = message.get("target_client_id")
                if not target_client_id:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "message": f"{message_type} requires target_client_id.",
                            }
                        )
                    )
                    continue
                payload = {
                    **message,
                    "from_client_id": client_id,
                    "room_code": room_code,
                }
                sent = await hub.send_to(room_code, target_client_id, payload)
                if not sent:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "message": f"Target client {target_client_id} is offline.",
                            }
                        )
                    )
                continue

            await websocket.send_text(
                json.dumps({"type": "error", "message": f"Unknown message: {message_type}"})
            )
    except WebSocketDisconnect:
        pass
    finally:
        if room_code and client_id:
            await hub.leave(room_code, client_id)
            await hub.broadcast(
                room_code,
                {
                    "type": "peer_left",
                    "room_code": room_code,
                    "client_id": client_id,
                    "remaining_peers": hub.get_room_size(room_code),
                },
                exclude_client_id=client_id,
            )
