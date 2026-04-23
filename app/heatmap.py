"""
heatmap.py — GET /stores/{store_id}/heatmap

Zone visit frequency and avg dwell, normalised 0-100.
Includes data_confidence flag if fewer than 20 sessions.
"""

import logging
import time as time_module
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.models import HeatmapResponse, HeatmapZone

logger = logging.getLogger("heatmap")
router = APIRouter()

# Zone SKU mapping (embedded for quick lookup)
ZONE_SKU_MAP = {
    "SKINCARE": "MOISTURISER",
    "HAIRCARE": "SHAMPOO",
    "ACCESSORIES": "ACCESSORIES",
    "PERFUMERY": "FRAGRANCE",
    "MAKEUP": "COSMETICS",
    "WELLNESS": "SUPPLEMENTS",
    "MEN": "GROOMING",
    "BODYCARE": "LOTION",
    "SPA": "SPA_PRODUCTS",
    "PREMIUM": "LUXURY",
    "KIDS": "KIDS_CARE",
    "FRAGRANCE": "PERFUME",
    "BILLING": None,
    "BILLING_QUEUE": None,
    "ENTRY": None,
    "EXIT": None,
}


@router.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse)
async def get_heatmap(
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
        # Zone stats from ZONE_EXIT events (which carry dwell_ms)
        zone_rows = db.execute(text("""
            SELECT
                zone_id,
                COUNT(*) as visit_count,
                AVG(CASE WHEN dwell_ms > 0 THEN dwell_ms ELSE NULL END) as avg_dwell
            FROM events
            WHERE store_id = :sid
              AND is_staff = 0
              AND event_type IN ('ZONE_EXIT', 'ZONE_DWELL')
              AND zone_id NOT IN ('ENTRY', 'EXIT')
              AND zone_id IS NOT NULL
              AND timestamp BETWEEN :ts AND :te
            GROUP BY zone_id
            ORDER BY visit_count DESC
        """), {"sid": store_id, "ts": ts_start, "te": ts_end}).fetchall()

        # Session count for data_confidence
        session_row = db.execute(text("""
            SELECT COUNT(DISTINCT visitor_id) as cnt
            FROM events
            WHERE store_id = :sid
              AND is_staff = 0
              AND event_type = 'ENTRY'
              AND timestamp BETWEEN :ts AND :te
        """), {"sid": store_id, "ts": ts_start, "te": ts_end}).fetchone()
        session_count = session_row.cnt if session_row else 0

    except Exception as e:
        logger.error(f"[{trace_id}] Heatmap query failed: {e}")
        raise HTTPException(status_code=503, detail={"error": "Database unavailable", "code": "DB_UNAVAILABLE"})

    # Normalise visit_count to 0-100
    max_visits = max((r.visit_count for r in zone_rows), default=1)
    zones_out = []
    for r in zone_rows:
        normalised = round((r.visit_count / max(max_visits, 1)) * 100, 1)
        avg_dwell = round(r.avg_dwell or 0, 0)
        zones_out.append(
            HeatmapZone(
                zone_id=r.zone_id,
                sku_zone=ZONE_SKU_MAP.get(r.zone_id),
                visit_count=r.visit_count,
                avg_dwell_ms=avg_dwell,
                normalised_score=normalised,
            )
        )

    latency_ms = round((time_module.time() - t0) * 1000, 1)
    logger.info(
        f"trace_id={trace_id} store_id={store_id} endpoint=/heatmap "
        f"latency_ms={latency_ms} status_code=200"
    )

    return HeatmapResponse(
        store_id=store_id,
        date=today_str,
        zones=zones_out,
        data_confidence=session_count >= 20,
    )
