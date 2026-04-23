"""
ws.py — WebSocket manager for real-time dashboard updates.

Clients connect to /ws and receive JSON-encoded events as they are ingested.
The broadcast is fire-and-forget; slow clients are disconnected gracefully.
"""

import json
import logging
from typing import Set

from fastapi import WebSocket

logger = logging.getLogger("ws")


class ConnectionManager:
    def __init__(self):
        self._active: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._active.add(websocket)
        logger.info(f"WS client connected. Total: {len(self._active)}")

    def disconnect(self, websocket: WebSocket) -> None:
        self._active.discard(websocket)
        logger.info(f"WS client disconnected. Total: {len(self._active)}")

    async def broadcast(self, data: dict) -> None:
        dead = set()
        for ws in list(self._active):
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._active.discard(ws)

    @property
    def connection_count(self) -> int:
        return len(self._active)


manager = ConnectionManager()


async def broadcast_event(event: dict) -> None:
    """Called from ingestion.py for each ingested event."""
    await manager.broadcast({"type": "event", "data": event})


async def broadcast_metrics(store_id: str, metrics: dict) -> None:
    """Optionally broadcast computed metrics."""
    await manager.broadcast({"type": "metrics", "store_id": store_id, "data": metrics})
