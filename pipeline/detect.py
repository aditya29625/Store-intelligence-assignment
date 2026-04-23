"""
detect.py — Main detection + tracking script.

Usage:
    python pipeline/detect.py \
        --video path/to/clip.mp4 \
        --store-id STORE_BLR_002 \
        --camera-id CAM_ENTRY_01 \
        --camera-type entry \
        --layout data/store_layout.json \
        --output data/events.jsonl \
        --clip-start "2026-03-03T09:00:00Z" \
        --model yolov8m.pt \
        --every-n-frames 3

All events are written to --output (JSONL) and also POSTed to the API if
--api-url is provided.
"""

import argparse
import json
import os
import sys
import logging
import requests
import cv2
import numpy as np
from datetime import datetime, timezone
from pathlib import Path

# Make pipeline a package importable from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.emit import EventEmitter
from pipeline.tracker import StoreTracker, load_store_layout
from pipeline.staff_detector import is_staff_by_colour, ZonePersistenceTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("detect")


def load_model(model_path: str):
    try:
        from ultralytics import YOLO
        logger.info(f"Loading YOLO model from {model_path} ...")
        model = YOLO(model_path)
        logger.info("✅ Model loaded.")
        return model
    except Exception as e:
        logger.error(f"Failed to load model: {e}")
        return None


def process_clip(
    video_path: str,
    store_id: str,
    camera_id: str,
    camera_type: str,
    store_layout: dict,
    clip_start: datetime,
    output_path: str,
    model_path: str,
    every_n_frames: int = 3,
    api_url: str = None,
    batch_size: int = 50,
):
    model = load_model(model_path)
    if model is None:
        logger.error("Cannot proceed without model.")
        return

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        logger.error(f"Cannot open video: {video_path}")
        return

    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    logger.info(f"Video: {video_path}, FPS={fps:.1f}, Frames={total_frames}")

    store = store_layout.get(store_id)
    if not store:
        logger.error(f"Store ID {store_id} not in layout.")
        return

    zones = store["zones"]
    staff_zone_tracker = ZonePersistenceTracker()

    with EventEmitter(output_path=output_path) as emitter:
        tracker = StoreTracker(
            store_id=store_id,
            camera_id=camera_id,
            camera_type=camera_type,
            zones=zones,
            clip_start=clip_start,
            fps=fps,
            emitter=emitter,
        )

        frame_idx = 0
        batch_events = []

        while True:
            ok, frame = cap.read()
            if not ok:
                break

            frame_idx += 1
            if frame_idx % every_n_frames != 0:
                continue

            frame_h, frame_w = frame.shape[:2]

            # Run YOLO tracking
            results = model.track(frame, persist=True, conf=0.25, classes=[0], verbose=False)

            detections = []
            for result in results:
                if result.boxes is None:
                    continue
                for box in result.boxes:
                    x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
                    conf = float(box.conf[0])
                    track_id = int(box.id[0]) if box.id is not None else None

                    # Staff detection
                    staff_by_colour, _ = is_staff_by_colour(frame, x1, y1, x2, y2)

                    # Zone for persistence tracker
                    from pipeline.tracker import zone_for_point
                    cx_n = ((x1 + x2) / 2.0) / frame_w
                    cy_n = ((y1 + y2) / 2.0) / frame_h
                    zone_obj = zone_for_point(cx_n, cy_n, zones)
                    zone_name = zone_obj["zone_id"] if zone_obj else None

                    if track_id is not None:
                        staff_zone_tracker.update(track_id, zone_name, frame_idx)
                        staff_by_zone = staff_zone_tracker.is_staff(track_id)
                    else:
                        staff_by_zone = False

                    is_staff = staff_by_colour or staff_by_zone

                    detections.append({
                        "track_id": track_id,
                        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                        "confidence": conf,
                        "is_staff": is_staff,
                    })

            tracker.update(frame, detections, frame_idx)

            # Batch flush to API
            if api_url and len(emitter.all_events()) >= batch_size:
                _post_events(api_url, emitter.all_events()[-batch_size:])

            if frame_idx % 150 == 0:
                logger.info(f"Frame {frame_idx}/{total_frames} | Events: {len(emitter.all_events())}")

        cap.release()
        logger.info(f"Done. Total events: {len(emitter.all_events())} → {output_path}")

        # Final flush
        if api_url and emitter.all_events():
            _post_events(api_url, emitter.all_events())


def _post_events(api_url: str, events: list) -> None:
    try:
        resp = requests.post(
            f"{api_url}/events/ingest",
            json={"events": events},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            logger.warning(f"API returned {resp.status_code}: {resp.text[:200]}")
        else:
            logger.info(f"Ingested {len(events)} events into API.")
    except Exception as e:
        logger.error(f"Failed to POST events to API: {e}")


def main():
    parser = argparse.ArgumentParser(description="Apex Retail CCTV Detection Pipeline")
    parser.add_argument("--video", required=True, help="Path to input video clip")
    parser.add_argument("--store-id", required=True, help="Store ID (e.g. STORE_BLR_002)")
    parser.add_argument("--camera-id", required=True, help="Camera ID (e.g. CAM_ENTRY_01)")
    parser.add_argument("--camera-type", choices=["entry", "main_floor", "billing"], required=True)
    parser.add_argument("--layout", default="data/store_layout.json", help="Path to store_layout.json")
    parser.add_argument("--output", default="data/events.jsonl", help="Output JSONL path")
    parser.add_argument("--clip-start", default=None, help="ISO-8601 UTC clip start timestamp")
    parser.add_argument("--model", default="yolov8m.pt", help="YOLO model path")
    parser.add_argument("--every-n-frames", type=int, default=3, help="Process every Nth frame")
    parser.add_argument("--api-url", default=None, help="API base URL for live ingestion")
    args = parser.parse_args()

    if args.clip_start:
        clip_start = datetime.fromisoformat(args.clip_start.replace("Z", "+00:00"))
    else:
        clip_start = datetime.now(tz=timezone.utc)

    store_layout = load_store_layout(args.layout)

    process_clip(
        video_path=args.video,
        store_id=args.store_id,
        camera_id=args.camera_id,
        camera_type=args.camera_type,
        store_layout=store_layout,
        clip_start=clip_start,
        output_path=args.output,
        model_path=args.model,
        every_n_frames=args.every_n_frames,
        api_url=args.api_url,
    )


if __name__ == "__main__":
    main()
