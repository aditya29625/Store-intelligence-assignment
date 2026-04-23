"""
simulate.py — Synthetic event generator.

When real CCTV clips are unavailable, this module generates a realistic
stream of events matching the challenge schema. It simulates:
- Customer entries and exits (with realistic dwell times)
- Zone visits (ZONE_ENTER → ZONE_DWELL → ZONE_EXIT)
- Billing queue events (BILLING_QUEUE_JOIN, BILLING_QUEUE_ABANDON)
- Staff movements (is_staff=True)
- Re-entry events
- Edge cases: groups, empty periods, crowded billing

Usage (standalone):
    python pipeline/simulate.py \
        --store-id STORE_BLR_002 \
        --duration-minutes 60 \
        --visitors 45 \
        --output data/events.jsonl \
        --api-url http://localhost:8000

Usage (real-time streaming mode, for dashboard demo):
    python pipeline/simulate.py --realtime --api-url http://localhost:8000
"""

import argparse
import json
import logging
import random
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.emit import EventEmitter, build_event

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("simulate")


# -------------------------------------------------------------------------------------
# Zone pools per camera type (matching store_layout.json zones)
# -------------------------------------------------------------------------------------
FLOOR_ZONES = [
    {"zone_id": "SKINCARE", "sku_zone": "MOISTURISER"},
    {"zone_id": "HAIRCARE", "sku_zone": "SHAMPOO"},
    {"zone_id": "ACCESSORIES", "sku_zone": "ACCESSORIES"},
    {"zone_id": "PERFUMERY", "sku_zone": "FRAGRANCE"},
    {"zone_id": "MAKEUP", "sku_zone": "COSMETICS"},
    {"zone_id": "WELLNESS", "sku_zone": "SUPPLEMENTS"},
]

BILLING_ZONES = [
    {"zone_id": "BILLING_QUEUE", "sku_zone": None},
    {"zone_id": "BILLING", "sku_zone": None},
]

CAMERA_MAPPING = {
    "entry": "CAM_ENTRY_01",
    "floor": "CAM_FLOOR_01",
    "billing": "CAM_BILLING_01",
}


def make_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_vid() -> str:
    return "VIS_" + uuid.uuid4().hex[:6]


def post_events(api_url: str, events: list, timeout: int = 10) -> bool:
    try:
        resp = requests.post(
            f"{api_url}/events/ingest",
            json={"events": events},
            timeout=timeout,
        )
        return resp.status_code in (200, 201)
    except Exception as e:
        logger.error(f"POST failed: {e}")
        return False


def generate_visitor_session(
    store_id: str,
    session_start: datetime,
    visitor_id: str,
    is_staff: bool = False,
    is_reentry: bool = False,
    will_purchase: bool = False,
    group_size: int = 1,
) -> list[dict]:
    """
    Generate a full sequence of events for a single visitor session.
    Returns list of events ordered by timestamp.
    """
    events = []
    seq = 0
    t = session_start

    def evt(etype, camera="entry", zone=None, dwell_ms=0, confidence=None, queue_depth=None, sku_zone=None, vid=None):
        nonlocal seq, t
        seq += 1
        if confidence is None:
            confidence = round(random.uniform(0.72, 0.97), 3)
        return build_event(
            store_id=store_id,
            camera_id=CAMERA_MAPPING.get(camera, "CAM_ENTRY_01"),
            visitor_id=vid or visitor_id,
            event_type=etype,
            timestamp=make_iso(t),
            zone_id=zone,
            dwell_ms=dwell_ms,
            is_staff=is_staff,
            confidence=confidence,
            queue_depth=queue_depth,
            sku_zone=sku_zone,
            session_seq=seq,
        )

    # REENTRY event (before ENTRY if this is a re-entry)
    if is_reentry:
        events.append(evt("REENTRY", camera="entry"))
        t += timedelta(seconds=random.uniform(0.5, 2))

    # ENTRY
    events.append(evt("ENTRY", camera="entry"))
    t += timedelta(seconds=random.uniform(2, 8))

    if is_staff:
        # Staff walk through all zones
        for zone in random.sample(FLOOR_ZONES, len(FLOOR_ZONES)):
            t += timedelta(seconds=random.uniform(5, 20))
            events.append(evt("ZONE_ENTER", camera="floor", zone=zone["zone_id"], sku_zone=zone["sku_zone"]))
            dwell = random.randint(15000, 90000)
            t += timedelta(milliseconds=dwell)
            events.append(evt("ZONE_EXIT", camera="floor", zone=zone["zone_id"], dwell_ms=dwell, sku_zone=zone["sku_zone"]))
        # Staff sometimes go to billing
        if random.random() < 0.5:
            t += timedelta(seconds=10)
            events.append(evt("ZONE_ENTER", camera="billing", zone="BILLING"))
            t += timedelta(seconds=random.uniform(30, 120))
            events.append(evt("ZONE_EXIT", camera="billing", zone="BILLING", dwell_ms=random.randint(30000, 120000)))
        # Staff EXIT
        t += timedelta(seconds=random.uniform(5, 20))
        events.append(evt("EXIT", camera="entry"))
        return events

    # Customer: visit 1-3 zones
    num_zones = random.randint(1, min(3, len(FLOOR_ZONES)))
    visited_zones = random.sample(FLOOR_ZONES, num_zones)

    for zone in visited_zones:
        t += timedelta(seconds=random.uniform(5, 30))
        events.append(evt("ZONE_ENTER", camera="floor", zone=zone["zone_id"], sku_zone=zone["sku_zone"]))

        # Dwell time: 30s to 4 minutes
        total_dwell_ms = random.randint(20000, 240000)
        # Emit ZONE_DWELL every 30s
        emitted_dwell = 0
        while total_dwell_ms - emitted_dwell >= 30000:
            emitted_dwell += 30000
            t += timedelta(milliseconds=30000)
            events.append(evt("ZONE_DWELL", camera="floor", zone=zone["zone_id"],
                              dwell_ms=30000, sku_zone=zone["sku_zone"]))
        # Final partial dwell
        remaining = total_dwell_ms - emitted_dwell
        if remaining > 0:
            t += timedelta(milliseconds=remaining)

        events.append(evt("ZONE_EXIT", camera="floor", zone=zone["zone_id"],
                          dwell_ms=total_dwell_ms, sku_zone=zone["sku_zone"]))

    # Billing
    if will_purchase or random.random() < 0.35:
        t += timedelta(seconds=random.uniform(10, 30))
        queue_depth = random.randint(0, 4)
        if queue_depth > 0:
            events.append(evt("BILLING_QUEUE_JOIN", camera="billing",
                               zone="BILLING_QUEUE", queue_depth=queue_depth))
            t += timedelta(seconds=random.uniform(60, 180) * queue_depth)

        events.append(evt("ZONE_ENTER", camera="billing", zone="BILLING"))

        # Abandon vs purchase
        if not will_purchase and random.random() < 0.2:
            t += timedelta(seconds=random.uniform(20, 60))
            events.append(evt("BILLING_QUEUE_ABANDON", camera="billing", zone="BILLING"))
        else:
            t += timedelta(seconds=random.uniform(30, 120))
            events.append(evt("ZONE_EXIT", camera="billing", zone="BILLING",
                               dwell_ms=random.randint(30000, 120000)))

    # EXIT
    t += timedelta(seconds=random.uniform(5, 15))
    events.append(evt("EXIT", camera="entry"))
    return events


def generate_session_batch(
    store_id: str,
    start_time: datetime,
    duration_minutes: int = 60,
    num_visitors: int = 45,
    num_staff: int = 3,
    purchase_rate: float = 0.35,
) -> list[dict]:
    """
    Generate a realistic day's worth of events for one store.
    """
    all_events = []
    end_time = start_time + timedelta(minutes=duration_minutes)

    # Staff events (run throughout the day)
    for _ in range(num_staff):
        staff_id = "VIS_STAFF_" + uuid.uuid4().hex[:4]
        # Staff enters at start
        staff_start = start_time + timedelta(minutes=random.uniform(0, 5))
        staff_events = generate_visitor_session(
            store_id=store_id,
            session_start=staff_start,
            visitor_id=staff_id,
            is_staff=True,
        )
        all_events.extend(staff_events)

    # Customer visitor events
    visitor_ids = []
    for i in range(num_visitors):
        # Random arrival time
        arrival_offset = random.expovariate(1 / (duration_minutes / num_visitors))
        arrival_offset = min(arrival_offset, duration_minutes - 5)
        session_start = start_time + timedelta(minutes=arrival_offset)
        if session_start >= end_time:
            session_start = end_time - timedelta(minutes=5)

        visitor_id = make_vid()
        visitor_ids.append(visitor_id)
        will_purchase = random.random() < purchase_rate

        # Group entry: ~15% chance of group of 2-3
        group_size = 1
        if random.random() < 0.15:
            group_size = random.randint(2, 3)

        for g in range(group_size):
            gid = visitor_id if g == 0 else make_vid()
            # Slight time offset for group members
            gstart = session_start + timedelta(seconds=random.uniform(0, 3) * g)
            events = generate_visitor_session(
                store_id=store_id,
                session_start=gstart,
                visitor_id=gid,
                will_purchase=will_purchase and (g == 0),
            )
            all_events.extend(events)

    # Re-entry: ~10% of visitors come back
    reentry_candidates = random.sample(visitor_ids, max(1, int(len(visitor_ids) * 0.1)))
    for vid in reentry_candidates:
        # Re-enter 10-30 minutes after their last exit
        reentry_start = end_time - timedelta(minutes=random.uniform(5, 20))
        events = generate_visitor_session(
            store_id=store_id,
            session_start=reentry_start,
            visitor_id=vid,
            is_reentry=True,
            will_purchase=random.random() < 0.5,
        )
        all_events.extend(events)

    # Sort by timestamp
    all_events.sort(key=lambda e: e["timestamp"])
    return all_events


def run_batch(
    store_id: str,
    output_path: str,
    api_url: str = None,
    duration_minutes: int = 60,
    num_visitors: int = 45,
    start_time: datetime = None,
):
    if start_time is None:
        start_time = datetime.now(tz=timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)

    logger.info(f"Generating {num_visitors} visitor sessions for {store_id} over {duration_minutes} min ...")
    events = generate_session_batch(
        store_id=store_id,
        start_time=start_time,
        duration_minutes=duration_minutes,
        num_visitors=num_visitors,
    )

    with EventEmitter(output_path=output_path) as emitter:
        for evt in events:
            emitter.emit(evt)

    logger.info(f"Wrote {len(events)} events to {output_path}")

    if api_url:
        logger.info(f"POSTing {len(events)} events to {api_url}/events/ingest ...")
        # Batch in chunks of 500
        BATCH = 500
        for i in range(0, len(events), BATCH):
            batch = events[i:i + BATCH]
            ok = post_events(api_url, batch)
            logger.info(f"  Batch {i // BATCH + 1}: {'✅' if ok else '❌'} ({len(batch)} events)")


def run_realtime(
    store_id: str,
    api_url: str,
    visitors_per_minute: float = 1.5,
    purchase_rate: float = 0.35,
    duration_seconds: int = 300,
):
    """
    Stream events in real time (simulated) to the API.
    Events are generated slightly faster than real time and posted with actual wall-clock delays.
    """
    logger.info(f"🔴 Real-time simulation for {store_id} → {api_url}")
    start_wall = time.time()
    start_sim = datetime.now(tz=timezone.utc)
    session_count = 0
    pending_events = []

    while (time.time() - start_wall) < duration_seconds:
        # Spawn a new visitor session every ~40s
        inter_arrival = random.expovariate(visitors_per_minute / 60.0)
        time.sleep(min(inter_arrival, 5))  # cap sleep so we stay responsive

        vid = make_vid()
        sim_now = start_sim + timedelta(seconds=(time.time() - start_wall))
        events = generate_visitor_session(
            store_id=store_id,
            session_start=sim_now,
            visitor_id=vid,
            will_purchase=random.random() < purchase_rate,
        )
        session_count += 1

        # Post immediately (collapsed time — all events at once)
        ok = post_events(api_url, events)
        logger.info(
            f"Session {session_count}: {vid} → {len(events)} events {'✅' if ok else '❌'}"
        )

    logger.info("Real-time simulation complete.")


def main():
    parser = argparse.ArgumentParser(description="Synthetic event generator for Apex Retail")
    parser.add_argument("--store-id", default="STORE_BLR_002")
    parser.add_argument("--duration-minutes", type=int, default=60)
    parser.add_argument("--visitors", type=int, default=45)
    parser.add_argument("--output", default="data/events.jsonl")
    parser.add_argument("--api-url", default=None)
    parser.add_argument("--realtime", action="store_true", help="Stream events in real time")
    parser.add_argument("--realtime-duration", type=int, default=300, help="Realtime duration in seconds")
    parser.add_argument("--all-stores", action="store_true", help="Generate for all 5 stores")
    args = parser.parse_args()

    stores = [
        "STORE_BLR_001", "STORE_BLR_002", "STORE_MUM_001", "STORE_DEL_001", "STORE_HYD_001"
    ] if args.all_stores else [args.store_id]

    if args.realtime:
        run_realtime(
            store_id=args.store_id,
            api_url=args.api_url or "http://localhost:8000",
            duration_seconds=args.realtime_duration,
        )
    else:
        for store_id in stores:
            start = datetime.now(tz=timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
            out = args.output if not args.all_stores else f"data/events_{store_id}.jsonl"
            run_batch(
                store_id=store_id,
                output_path=out,
                api_url=args.api_url,
                duration_minutes=args.duration_minutes,
                num_visitors=args.visitors,
                start_time=start,
            )


if __name__ == "__main__":
    main()
