"""
emit.py — Event schema builder and JSONL emitter.
Converts raw detection state into structured events matching the challenge schema.
"""

import uuid
import json
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path


# ---------------------------------------------------------------------------
# Event type catalogue
# ---------------------------------------------------------------------------
EVENT_TYPES = {
    "ENTRY",
    "EXIT",
    "ZONE_ENTER",
    "ZONE_EXIT",
    "ZONE_DWELL",
    "BILLING_QUEUE_JOIN",
    "BILLING_QUEUE_ABANDON",
    "REENTRY",
}


def make_visitor_id() -> str:
    """Generate a short visitor ID token."""
    return "VIS_" + uuid.uuid4().hex[:6]


def build_event(
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    timestamp: str,
    zone_id: Optional[str] = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 0.9,
    queue_depth: Optional[int] = None,
    sku_zone: Optional[str] = None,
    session_seq: int = 1,
) -> dict:
    """
    Build a fully-compliant event dict matching the challenge schema.
    """
    assert event_type in EVENT_TYPES, f"Unknown event type: {event_type}"

    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": round(float(confidence), 4),
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone,
            "session_seq": session_seq,
        },
    }


def frame_to_iso(clip_start: datetime, frame_idx: int, fps: float) -> str:
    """Convert a frame index offset from clip start to ISO-8601 UTC string."""
    offset_sec = frame_idx / fps
    from datetime import timedelta
    ts = clip_start + timedelta(seconds=offset_sec)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


class EventEmitter:
    """
    Collects events and writes them to a JSONL file (one JSON per line).
    Also maintains an in-memory list for piping directly into the API.
    """

    def __init__(self, output_path: Optional[str] = None):
        self._events: list[dict] = []
        self._output_path = output_path
        self._fh = None
        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(output_path, "a", encoding="utf-8")

    def emit(self, event: dict) -> None:
        self._events.append(event)
        if self._fh:
            self._fh.write(json.dumps(event) + "\n")
            self._fh.flush()

    def all_events(self) -> list[dict]:
        return list(self._events)

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
