"""
tracker.py — ByteTrack wrapper with Re-ID logic.

Wraps Ultralytics' built-in tracker (ByteTrack) and adds:
- Per-session visitor_id assignment
- Re-ID across exits (histogram + IoU similarity)
- Entry/exit direction detection based on centroid Y movement
- Zone containment checks from store_layout.json
"""

import json
import time
import uuid
import numpy as np
import cv2
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Tuple


# ---------------------------------------------------------------------------
# Zone geometry helpers
# ---------------------------------------------------------------------------

def load_store_layout(layout_path: str) -> dict:
    with open(layout_path, "r") as f:
        data = json.load(f)
    # Build fast lookup: store_id -> {zones, cameras}
    lookup = {}
    for store in data["stores"]:
        lookup[store["store_id"]] = store
    return lookup


def point_in_zone(cx_norm: float, cy_norm: float, zone_bbox: dict) -> bool:
    """Check if a normalised centre point (0-1) is inside a zone bbox."""
    return (
        zone_bbox["x1"] <= cx_norm <= zone_bbox["x2"]
        and zone_bbox["y1"] <= cy_norm <= zone_bbox["y2"]
    )


def zone_for_point(
    cx_norm: float, cy_norm: float, zones: list
) -> Optional[dict]:
    """Return the matching zone dict for a normalised point, or None."""
    for zone in zones:
        if point_in_zone(cx_norm, cy_norm, zone["bbox"]):
            return zone
    return None


# ---------------------------------------------------------------------------
# Per-track state
# ---------------------------------------------------------------------------

@dataclass
class TrackState:
    track_id: int
    visitor_id: str
    first_seen_frame: int
    last_seen_frame: int
    first_center: Tuple[float, float]
    last_center: Tuple[float, float]
    current_zone: Optional[str] = None
    zone_enter_frame: Optional[int] = None
    has_exited: bool = False
    is_staff: bool = False
    appearance_hist: Optional[np.ndarray] = None   # BGR histogram for Re-ID
    session_seq: int = 0
    billing_enter_time: Optional[float] = None     # epoch time
    # Dwell tracking
    dwell_zone: Optional[str] = None
    dwell_enter_frame: Optional[int] = None
    last_dwell_emit_frame: Optional[int] = None


# ---------------------------------------------------------------------------
# Re-ID fingerprint store (persists across exit events)
# ---------------------------------------------------------------------------

@dataclass
class ExitedTrack:
    visitor_id: str
    exit_frame: int
    last_center: Tuple[float, float]
    appearance_hist: Optional[np.ndarray]
    is_staff: bool


class ReIDStore:
    """
    Store of recently exited tracks for re-identification.
    Matches a new detection against exited tracks using:
     1. Spatial proximity (last centre vs first centre of new detection)
     2. Appearance similarity (BGR histogram correlation)
    """

    def __init__(self, max_age_frames: int = 450):  # ~30s at 15fps
        self._store: List[ExitedTrack] = []
        self.max_age_frames = max_age_frames

    def record_exit(self, track: TrackState, frame_idx: int) -> None:
        self._store.append(
            ExitedTrack(
                visitor_id=track.visitor_id,
                exit_frame=frame_idx,
                last_center=track.last_center,
                appearance_hist=track.appearance_hist,
                is_staff=track.is_staff,
            )
        )

    def find_match(
        self,
        first_center: Tuple[float, float],
        appearance_hist: Optional[np.ndarray],
        current_frame: int,
        spatial_thresh: float = 0.15,
        hist_thresh: float = 0.5,
    ) -> Optional[ExitedTrack]:
        """Return best matching exited track or None."""
        best: Optional[ExitedTrack] = None
        best_score = -1.0

        for et in self._store:
            age = current_frame - et.exit_frame
            if age > self.max_age_frames:
                continue

            # Spatial score (1 = same position, 0 = far)
            dist = np.hypot(
                first_center[0] - et.last_center[0],
                first_center[1] - et.last_center[1],
            )
            if dist > spatial_thresh:
                continue

            spatial_score = max(0.0, 1.0 - dist / spatial_thresh)

            # Appearance score
            app_score = 0.5  # neutral if no hist
            if appearance_hist is not None and et.appearance_hist is not None:
                corr = cv2.compareHist(
                    appearance_hist, et.appearance_hist, cv2.HISTCMP_CORREL
                )
                app_score = max(0.0, float(corr))

            score = 0.4 * spatial_score + 0.6 * app_score
            if score > best_score and score > hist_thresh:
                best_score = score
                best = et

        return best

    def prune(self, current_frame: int) -> None:
        self._store = [
            et for et in self._store
            if (current_frame - et.exit_frame) <= self.max_age_frames
        ]


# ---------------------------------------------------------------------------
# Appearance histogram extraction
# ---------------------------------------------------------------------------

def extract_appearance_hist(frame: np.ndarray, x1: int, y1: int, x2: int, y2: int) -> Optional[np.ndarray]:
    """Extract a BGR colour histogram from the person crop (lower body only)."""
    try:
        h = y2 - y1
        crop = frame[y1 + h // 2: y2, x1:x2]
        if crop.size == 0:
            return None
        hist = cv2.calcHist([crop], [0, 1, 2], None, [8, 8, 8], [0, 256, 0, 256, 0, 256])
        cv2.normalize(hist, hist)
        return hist
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Direction detection
# ---------------------------------------------------------------------------

def detect_direction(first_cy: float, last_cy: float) -> str:
    """
    For an entry camera where store interior is 'up' (lower Y in image = deeper into store):
    Moving from bottom → top (decreasing Y) means ENTRY.
    Moving from top → bottom (increasing Y) means EXIT.
    """
    delta = last_cy - first_cy
    if delta < -0.05:
        return "ENTRY"
    elif delta > 0.05:
        return "EXIT"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Main Tracker class
# ---------------------------------------------------------------------------

class StoreTracker:
    """
    Manages per-frame detection state and emits retail analytics events.
    Used by detect.py.
    """

    def __init__(
        self,
        store_id: str,
        camera_id: str,
        camera_type: str,  # "entry", "main_floor", "billing"
        zones: list,
        clip_start: datetime,
        fps: float,
        emitter,  # EventEmitter instance
        dwell_interval_frames: int = 450,  # 30s @ 15fps
    ):
        self.store_id = store_id
        self.camera_id = camera_id
        self.camera_type = camera_type
        self.zones = zones
        self.clip_start = clip_start
        self.fps = fps
        self.emitter = emitter
        self.dwell_interval_frames = dwell_interval_frames

        self._active: Dict[int, TrackState] = {}   # track_id -> TrackState
        self._reid_store = ReIDStore()

    # ------------------------------------------------------------------
    def _frame_to_iso(self, frame_idx: int) -> str:
        offset_sec = frame_idx / max(self.fps, 1.0)
        ts = self.clip_start + timedelta(seconds=offset_sec)
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _emit(self, event_type: str, state: TrackState, frame_idx: int, **kwargs) -> None:
        from pipeline.emit import build_event  # lazy import to avoid circular
        state.session_seq += 1
        evt = build_event(
            store_id=self.store_id,
            camera_id=self.camera_id,
            visitor_id=state.visitor_id,
            event_type=event_type,
            timestamp=self._frame_to_iso(frame_idx),
            is_staff=state.is_staff,
            session_seq=state.session_seq,
            **kwargs,
        )
        self.emitter.emit(evt)

    # ------------------------------------------------------------------
    def update(self, frame: np.ndarray, detections: list, frame_idx: int) -> None:
        """
        detections: list of dicts with keys:
            track_id, x1, y1, x2, y2 (pixel), confidence, is_staff
        frame shape: (H, W, 3)
        """
        frame_h, frame_w = frame.shape[:2]
        seen_ids = set()

        for det in detections:
            tid = det["track_id"]
            if tid is None:
                continue
            seen_ids.add(tid)

            x1, y1, x2, y2 = int(det["x1"]), int(det["y1"]), int(det["x2"]), int(det["y2"])
            cx_norm = ((x1 + x2) / 2.0) / frame_w
            cy_norm = ((y1 + y2) / 2.0) / frame_h
            conf = det.get("confidence", 0.9)
            is_staff = det.get("is_staff", False)

            hist = extract_appearance_hist(frame, x1, y1, x2, y2)

            if tid not in self._active:
                # --- New track: check for re-entry ---
                matched = self._reid_store.find_match(
                    first_center=(cx_norm, cy_norm),
                    appearance_hist=hist,
                    current_frame=frame_idx,
                )
                if matched and not is_staff:
                    visitor_id = matched.visitor_id
                    event_type = "REENTRY"
                else:
                    visitor_id = "VIS_" + uuid.uuid4().hex[:6]
                    event_type = None  # Assign on exit/entry detection

                state = TrackState(
                    track_id=tid,
                    visitor_id=visitor_id,
                    first_seen_frame=frame_idx,
                    last_seen_frame=frame_idx,
                    first_center=(cx_norm, cy_norm),
                    last_center=(cx_norm, cy_norm),
                    appearance_hist=hist,
                    is_staff=is_staff,
                )
                self._active[tid] = state

                if event_type == "REENTRY":
                    self._emit("REENTRY", state, frame_idx, confidence=conf)
                    # Also emit a new ENTRY for session tracking
                    self._emit("ENTRY", state, frame_idx, confidence=conf)

            else:
                state = self._active[tid]
                state.last_seen_frame = frame_idx
                state.last_center = (cx_norm, cy_norm)
                if hist is not None:
                    state.appearance_hist = hist
                if is_staff:
                    state.is_staff = True   # sticky

            # --- Zone logic (main floor / billing cameras) ---
            if self.camera_type in ("main_floor", "billing"):
                current_zone_obj = zone_for_point(cx_norm, cy_norm, self.zones)
                new_zone = current_zone_obj["zone_id"] if current_zone_obj else None

                if new_zone != state.current_zone:
                    # ZONE_EXIT from previous zone
                    if state.current_zone is not None:
                        dwell_frames = frame_idx - (state.zone_enter_frame or frame_idx)
                        dwell_ms = int((dwell_frames / max(self.fps, 1.0)) * 1000)
                        self._emit(
                            "ZONE_EXIT",
                            state,
                            frame_idx,
                            zone_id=state.current_zone,
                            dwell_ms=dwell_ms,
                            confidence=conf,
                            sku_zone=current_zone_obj["sku_zone"] if current_zone_obj else None,
                        )
                        # Billing abandon check
                        if state.current_zone in ("BILLING", "BILLING_QUEUE") and state.billing_enter_time:
                            elapsed = time.time() - state.billing_enter_time
                            if elapsed < 300:  # < 5 min → possible abandon
                                self._emit(
                                    "BILLING_QUEUE_ABANDON",
                                    state,
                                    frame_idx,
                                    zone_id=state.current_zone,
                                    confidence=conf,
                                )
                            state.billing_enter_time = None

                    # ZONE_ENTER for new zone
                    if new_zone is not None:
                        sku = current_zone_obj.get("sku_zone") if current_zone_obj else None
                        q_depth = None
                        evt_type = "ZONE_ENTER"
                        if new_zone in ("BILLING", "BILLING_QUEUE"):
                            q_depth = self._count_in_billing_zone()
                            if q_depth > 0:
                                evt_type = "BILLING_QUEUE_JOIN"
                            state.billing_enter_time = time.time()
                        self._emit(
                            evt_type,
                            state,
                            frame_idx,
                            zone_id=new_zone,
                            dwell_ms=0,
                            confidence=conf,
                            queue_depth=q_depth,
                            sku_zone=sku,
                        )

                    state.current_zone = new_zone
                    state.zone_enter_frame = frame_idx
                    state.dwell_enter_frame = frame_idx
                    state.last_dwell_emit_frame = frame_idx

                else:
                    # Same zone — check for 30s dwell emit
                    if new_zone and state.dwell_enter_frame is not None:
                        frames_since_dwell = frame_idx - (state.last_dwell_emit_frame or state.dwell_enter_frame)
                        if frames_since_dwell >= self.dwell_interval_frames:
                            dwell_ms = int((frames_since_dwell / max(self.fps, 1.0)) * 1000)
                            sku = current_zone_obj.get("sku_zone") if current_zone_obj else None
                            self._emit(
                                "ZONE_DWELL",
                                state,
                                frame_idx,
                                zone_id=new_zone,
                                dwell_ms=dwell_ms,
                                confidence=conf,
                                sku_zone=sku,
                            )
                            state.last_dwell_emit_frame = frame_idx

            # --- Entry/Exit for entry camera ---
            elif self.camera_type == "entry":
                pass  # Emitted on track termination (below)

        # --- Handle lost tracks (exited) ---
        lost_ids = set(self._active.keys()) - seen_ids
        for tid in lost_ids:
            state = self._active.pop(tid)
            direction = detect_direction(state.first_center[1], state.last_center[1])

            if self.camera_type == "entry":
                if direction == "EXIT" and not state.has_exited:
                    dwell_frames = frame_idx - state.first_seen_frame
                    dwell_ms = int((dwell_frames / max(self.fps, 1.0)) * 1000)
                    self._emit("EXIT", state, frame_idx, dwell_ms=dwell_ms, confidence=0.85)
                    state.has_exited = True
                elif direction == "ENTRY":
                    self._emit("ENTRY", state, state.first_seen_frame, confidence=0.88)

            # Record in Re-ID store
            self._reid_store.record_exit(state, frame_idx)

        # Prune old reid entries periodically
        if frame_idx % 300 == 0:
            self._reid_store.prune(frame_idx)

    def _count_in_billing_zone(self) -> int:
        """Count currently tracked people in BILLING or BILLING_QUEUE zones."""
        count = 0
        for state in self._active.values():
            if state.current_zone in ("BILLING", "BILLING_QUEUE") and not state.is_staff:
                count += 1
        return count
