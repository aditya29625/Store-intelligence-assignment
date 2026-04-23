"""
staff_detector.py — Heuristic staff detection.

Identifies store staff using two complementary signals:
1. Uniform colour detection: staff uniforms tend to be a specific solid colour
   (e.g., red apron, dark blue shirt). We detect dominant clothing colour in the
   lower-body crop and check against a configurable list of staff colour ranges.
2. Zone-permanence heuristic: tracks that spend >80% of their time in ALL zones
   (including back areas) across an entire session are flagged as staff.

The `is_staff` flag is set on TrackState objects and propagated to emitted events.
"""

import cv2
import numpy as np
from typing import List, Tuple, Optional


# ---------------------------------------------------------------------------
# Configurable HSV colour ranges for typical retail staff uniforms
# Tweak these per-store using store_layout.json in future versions.
# ---------------------------------------------------------------------------

STAFF_COLOUR_RANGES_HSV: List[Tuple[np.ndarray, np.ndarray]] = [
    # Dark navy blue
    (np.array([100, 80, 30]), np.array([130, 255, 120])),
    # Red / burgundy apron
    (np.array([0, 120, 70]), np.array([10, 255, 200])),
    (np.array([170, 120, 70]), np.array([180, 255, 200])),
    # Black uniforms
    (np.array([0, 0, 0]), np.array([180, 40, 60])),
    # Olive / khaki
    (np.array([20, 40, 60]), np.array([40, 120, 160])),
]

# Fraction of lower-body pixels that must match a staff colour
STAFF_COLOUR_THRESHOLD = 0.35

# Number of consecutive frames a track must be "visible everywhere" to be staff
STAFF_ZONE_PERSISTENCE_FRAMES = 60  # ~4s at 15fps


def is_staff_by_colour(
    frame: np.ndarray, x1: int, y1: int, x2: int, y2: int
) -> Tuple[bool, float]:
    """
    Return (is_staff, confidence) based on uniform colour analysis.
    Analyses only the lower half of the bounding box (torso / clothing area).
    """
    try:
        h = y2 - y1
        # Focus on mid-lower body (chest to waist height)
        crop_y1 = y1 + h // 3
        crop_y2 = y2 - h // 6
        crop = frame[crop_y1:crop_y2, x1:x2]
        if crop.size == 0:
            return False, 0.0

        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        total_pixels = crop.shape[0] * crop.shape[1]
        if total_pixels == 0:
            return False, 0.0

        max_match_frac = 0.0
        for lo, hi in STAFF_COLOUR_RANGES_HSV:
            mask = cv2.inRange(hsv, lo, hi)
            match_frac = float(np.count_nonzero(mask)) / total_pixels
            if match_frac > max_match_frac:
                max_match_frac = match_frac

        is_staff = max_match_frac >= STAFF_COLOUR_THRESHOLD
        confidence = min(1.0, max_match_frac / STAFF_COLOUR_THRESHOLD)
        return is_staff, round(confidence, 3)

    except Exception:
        return False, 0.0


class ZonePersistenceTracker:
    """
    Tracks which zones a given track_id visits.
    If a track visits N distinct zones and has been seen for many frames,
    it is flagged as staff (customers typically stay in 1-2 zones).
    """

    def __init__(self, min_zones_for_staff: int = 4, min_frames: int = 90):
        self._track_zones: dict = {}   # track_id -> set of zone_ids
        self._track_frames: dict = {}  # track_id -> frame_count
        self.min_zones_for_staff = min_zones_for_staff
        self.min_frames = min_frames

    def update(self, track_id: int, zone_id: Optional[str], frame_idx: int) -> None:
        if track_id not in self._track_zones:
            self._track_zones[track_id] = set()
            self._track_frames[track_id] = 0
        if zone_id:
            self._track_zones[track_id].add(zone_id)
        self._track_frames[track_id] += 1

    def is_staff(self, track_id: int) -> bool:
        zones = self._track_zones.get(track_id, set())
        frames = self._track_frames.get(track_id, 0)
        return (
            frames >= self.min_frames
            and len(zones) >= self.min_zones_for_staff
        )

    def cleanup(self, active_ids: set) -> None:
        stale = set(self._track_zones.keys()) - active_ids
        for tid in stale:
            self._track_zones.pop(tid, None)
            self._track_frames.pop(tid, None)
