"""
funnel.py — GET /stores/{store_id}/funnel

Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase
- Session is the unit (not raw events)
- Re-entries count as one session per visitor per day
- Drop-off % computed at each stage
"""

import logging
import time as time_module
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.models import FunnelResponse, FunnelStage

logger = logging.getLogger("funnel")
router = APIRouter()


@router.get("/stores/{store_id}/funnel", response_model=FunnelResponse)
async def get_funnel(
    store_id: str,
    request: Request,
    date: Optional[str] = None,
    db: Session = Depends(get_db),
):
    t0 = time_module.time()
    trace_id = request.headers.get("X-Trace-Id", "?")

    if date:
        ts_start = f"{date}T00:00:00Z"
        ts_end = f"{date}T23:59:59Z"
    else:
        now = datetime.now(tz=timezone.utc)
        ts_start = now.strftime("%Y-%m-%dT00:00:00Z")
        ts_end = now.strftime("%Y-%m-%dT23:59:59Z")

    today_str = ts_start[:10]

    try:
        # Stage 1: Unique sessions (distinct visitor_id with ENTRY, deduplicated)
        row1 = db.execute(text("""
            SELECT COUNT(DISTINCT visitor_id) as cnt
            FROM events
            WHERE store_id = :sid
              AND event_type = 'ENTRY'
              AND is_staff = 0
              AND timestamp BETWEEN :ts AND :te
        """), {"sid": store_id, "ts": ts_start, "te": ts_end}).fetchone()
        stage_entry = row1.cnt if row1 else 0

        # Stage 2: Visitors who visited at least one zone (ZONE_ENTER or ZONE_EXIT)
        row2 = db.execute(text("""
            SELECT COUNT(DISTINCT visitor_id) as cnt
            FROM events
            WHERE store_id = :sid
              AND event_type IN ('ZONE_ENTER', 'ZONE_EXIT', 'ZONE_DWELL')
              AND is_staff = 0
              AND zone_id NOT IN ('BILLING', 'BILLING_QUEUE', 'ENTRY', 'EXIT')
              AND timestamp BETWEEN :ts AND :te
        """), {"sid": store_id, "ts": ts_start, "te": ts_end}).fetchone()
        stage_zone = row2.cnt if row2 else 0

        # Stage 3: Visitors who joined billing queue or entered billing zone
        row3 = db.execute(text("""
            SELECT COUNT(DISTINCT visitor_id) as cnt
            FROM events
            WHERE store_id = :sid
              AND event_type IN ('BILLING_QUEUE_JOIN', 'ZONE_ENTER')
              AND zone_id IN ('BILLING', 'BILLING_QUEUE')
              AND is_staff = 0
              AND timestamp BETWEEN :ts AND :te
        """), {"sid": store_id, "ts": ts_start, "te": ts_end}).fetchone()
        stage_billing = row3.cnt if row3 else 0

        # Stage 4: Visitors who completed (no BILLING_QUEUE_ABANDON after BILLING_QUEUE_JOIN)
        # Proxy: visitors in billing zone but without abandon event
        row4a = db.execute(text("""
            SELECT DISTINCT visitor_id
            FROM events
            WHERE store_id = :sid
              AND event_type IN ('BILLING_QUEUE_JOIN', 'ZONE_ENTER')
              AND zone_id IN ('BILLING', 'BILLING_QUEUE')
              AND is_staff = 0
              AND timestamp BETWEEN :ts AND :te
        """), {"sid": store_id, "ts": ts_start, "te": ts_end}).fetchall()
        billing_visitors_set = {r.visitor_id for r in row4a}

        row4b = db.execute(text("""
            SELECT DISTINCT visitor_id
            FROM events
            WHERE store_id = :sid
              AND event_type = 'BILLING_QUEUE_ABANDON'
              AND is_staff = 0
              AND timestamp BETWEEN :ts AND :te
        """), {"sid": store_id, "ts": ts_start, "te": ts_end}).fetchall()
        abandon_set = {r.visitor_id for r in row4b}

        purchased = billing_visitors_set - abandon_set
        stage_purchase = len(purchased)

    except Exception as e:
        logger.error(f"[{trace_id}] Funnel query failed: {e}")
        raise HTTPException(status_code=503, detail={"error": "Database unavailable", "code": "DB_UNAVAILABLE"})

    def drop_off(current: int, previous: int) -> float:
        if previous == 0:
            return 0.0
        return round((previous - current) / previous * 100, 1)

    stages = [
        FunnelStage(stage="ENTRY", count=stage_entry, drop_off_pct=0.0),
        FunnelStage(stage="ZONE_VISIT", count=stage_zone, drop_off_pct=drop_off(stage_zone, stage_entry)),
        FunnelStage(stage="BILLING_QUEUE", count=stage_billing, drop_off_pct=drop_off(stage_billing, stage_zone)),
        FunnelStage(stage="PURCHASE", count=stage_purchase, drop_off_pct=drop_off(stage_purchase, stage_billing)),
    ]

    latency_ms = round((time_module.time() - t0) * 1000, 1)
    logger.info(
        f"trace_id={trace_id} store_id={store_id} endpoint=/funnel "
        f"latency_ms={latency_ms} status_code=200"
    )

    return FunnelResponse(
        store_id=store_id,
        date=today_str,
        stages=stages,
        sessions_total=stage_entry,
    )
