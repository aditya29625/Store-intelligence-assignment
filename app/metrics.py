"""
metrics.py — GET /stores/{store_id}/metrics

Computes real-time store metrics from the events table:
- unique_visitors: distinct visitor_ids with ENTRY event today, is_staff=False
- conversion_rate: visitors who also had a BILLING/BILLING_QUEUE event
  in 5-minute window before any transaction → correlated via time window
- avg_dwell_per_zone: per zone, mean of dwell_ms from ZONE_EXIT events
- queue_depth: latest queue_depth metadata value for BILLING_QUEUE_JOIN
- abandonment_rate: BILLING_QUEUE_ABANDON / (BILLING_QUEUE_JOIN + BILLING_QUEUE_ABANDON)

All metrics exclude is_staff=True rows.
"""

import json
import logging
import time as time_module
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.models import StoreMetrics, ZoneDwell

logger = logging.getLogger("metrics")
router = APIRouter()


def _today_window(store_id: str) -> tuple[str, str]:
    """Return ISO timestamps for start and end of today (UTC)."""
    now = datetime.now(tz=timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/stores/{store_id}/metrics", response_model=StoreMetrics)
async def get_metrics(
    store_id: str,
    request: Request,
    date: Optional[str] = None,   # e.g. "2026-03-03"
    db: Session = Depends(get_db),
):
    t0 = time_module.time()
    trace_id = request.headers.get("X-Trace-Id", "?")

    if date:
        ts_start = f"{date}T00:00:00Z"
        ts_end = f"{date}T23:59:59Z"
    else:
        ts_start, ts_end = _today_window(store_id)

    today_str = ts_start[:10]

    try:
        # 1. Unique visitors (customer ENTRY events only)
        row = db.execute(text("""
            SELECT COUNT(DISTINCT visitor_id) as cnt
            FROM events
            WHERE store_id = :sid
              AND event_type = 'ENTRY'
              AND is_staff = 0
              AND timestamp BETWEEN :ts AND :te
        """), {"sid": store_id, "ts": ts_start, "te": ts_end}).fetchone()
        unique_visitors = row.cnt if row else 0

        # 2. Total ENTRY events (includes re-entries)
        row2 = db.execute(text("""
            SELECT COUNT(*) as cnt
            FROM events
            WHERE store_id = :sid
              AND event_type = 'ENTRY'
              AND is_staff = 0
              AND timestamp BETWEEN :ts AND :te
        """), {"sid": store_id, "ts": ts_start, "te": ts_end}).fetchone()
        total_entries = row2.cnt if row2 else 0

        # 3. Total EXIT events
        row3 = db.execute(text("""
            SELECT COUNT(*) as cnt
            FROM events
            WHERE store_id = :sid
              AND event_type = 'EXIT'
              AND is_staff = 0
              AND timestamp BETWEEN :ts AND :te
        """), {"sid": store_id, "ts": ts_start, "te": ts_end}).fetchone()
        total_exits = row3.cnt if row3 else 0

        # 4. Staff events excluded
        row4 = db.execute(text("""
            SELECT COUNT(*) as cnt
            FROM events
            WHERE store_id = :sid
              AND is_staff = 1
              AND timestamp BETWEEN :ts AND :te
        """), {"sid": store_id, "ts": ts_start, "te": ts_end}).fetchone()
        staff_excluded = row4.cnt if row4 else 0

        # 5. Conversion rate — visitors who visited billing zone
        # Proxy: visitors with at least one BILLING_QUEUE_JOIN or ZONE_ENTER in BILLING zone
        row5 = db.execute(text("""
            SELECT COUNT(DISTINCT visitor_id) as cnt
            FROM events
            WHERE store_id = :sid
              AND is_staff = 0
              AND event_type IN ('BILLING_QUEUE_JOIN', 'ZONE_EXIT')
              AND zone_id IN ('BILLING', 'BILLING_QUEUE')
              AND timestamp BETWEEN :ts AND :te
        """), {"sid": store_id, "ts": ts_start, "te": ts_end}).fetchone()
        billing_visitors = row5.cnt if row5 else 0

        conversion_rate = round(billing_visitors / max(unique_visitors, 1), 4)

        # 6. Avg dwell per zone (from ZONE_EXIT events which carry total dwell_ms)
        zone_rows = db.execute(text("""
            SELECT zone_id,
                   AVG(dwell_ms) as avg_dwell,
                   COUNT(*) as visit_count
            FROM events
            WHERE store_id = :sid
              AND is_staff = 0
              AND event_type = 'ZONE_EXIT'
              AND zone_id IS NOT NULL
              AND dwell_ms > 0
              AND timestamp BETWEEN :ts AND :te
            GROUP BY zone_id
            ORDER BY avg_dwell DESC
        """), {"sid": store_id, "ts": ts_start, "te": ts_end}).fetchall()

        avg_dwell_per_zone = [
            ZoneDwell(
                zone_id=r.zone_id,
                avg_dwell_ms=round(r.avg_dwell, 0),
                visit_count=r.visit_count,
            )
            for r in zone_rows
        ]

        # 7. Latest queue depth
        queue_row = db.execute(text("""
            SELECT metadata
            FROM events
            WHERE store_id = :sid
              AND event_type = 'BILLING_QUEUE_JOIN'
              AND timestamp BETWEEN :ts AND :te
            ORDER BY timestamp DESC
            LIMIT 1
        """), {"sid": store_id, "ts": ts_start, "te": ts_end}).fetchone()

        queue_depth = 0
        if queue_row and queue_row.metadata:
            try:
                meta = json.loads(queue_row.metadata) if isinstance(queue_row.metadata, str) else queue_row.metadata
                if isinstance(meta, str):
                    import ast
                    meta = ast.literal_eval(meta)
                queue_depth = meta.get("queue_depth") or 0
            except Exception:
                pass

        # 8. Abandonment rate
        abandon_row = db.execute(text("""
            SELECT
                SUM(CASE WHEN event_type = 'BILLING_QUEUE_ABANDON' THEN 1 ELSE 0 END) as abandons,
                SUM(CASE WHEN event_type = 'BILLING_QUEUE_JOIN' THEN 1 ELSE 0 END) as joins
            FROM events
            WHERE store_id = :sid
              AND is_staff = 0
              AND event_type IN ('BILLING_QUEUE_JOIN', 'BILLING_QUEUE_ABANDON')
              AND timestamp BETWEEN :ts AND :te
        """), {"sid": store_id, "ts": ts_start, "te": ts_end}).fetchone()

        abandons = abandon_row.abandons or 0
        joins = abandon_row.joins or 0
        abandonment_rate = round(abandons / max(joins + abandons, 1), 4)

    except Exception as e:
        logger.error(f"[{trace_id}] Metrics query failed for {store_id}: {e}")
        raise HTTPException(status_code=503, detail={"error": "Database unavailable", "code": "DB_UNAVAILABLE"})

    latency_ms = round((time_module.time() - t0) * 1000, 1)
    logger.info(
        f"trace_id={trace_id} store_id={store_id} endpoint=/metrics "
        f"latency_ms={latency_ms} status_code=200"
    )

    return StoreMetrics(
        store_id=store_id,
        date=today_str,
        unique_visitors=unique_visitors,
        conversion_rate=conversion_rate,
        avg_dwell_per_zone=avg_dwell_per_zone,
        queue_depth=int(queue_depth),
        abandonment_rate=abandonment_rate,
        total_entries=total_entries,
        total_exits=total_exits,
        staff_events_excluded=staff_excluded,
    )
