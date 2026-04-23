"""
health.py — GET /health

Service health check:
- Database connectivity
- Last event timestamp per store
- STALE_FEED warning if last event > 10 min ago
"""

import logging
import time as time_module
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.models import HealthResponse, StoreHealth

logger = logging.getLogger("health")
router = APIRouter()

STALE_FEED_MINUTES = 10
VERSION = "1.0.0"


@router.get("/health", response_model=HealthResponse)
async def health_check(
    request: Request,
    db: Session = Depends(get_db),
):
    t0 = time_module.time()
    trace_id = request.headers.get("X-Trace-Id", "?")
    now = datetime.now(tz=timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    db_status = "ok"
    stores_health = []

    try:
        # Check DB reachability
        db.execute(text("SELECT 1"))

        # Get all known stores from events table
        store_rows = db.execute(text("""
            SELECT store_id, MAX(timestamp) as last_ts
            FROM events
            GROUP BY store_id
            ORDER BY store_id
        """)).fetchall()

        for row in store_rows:
            store_id = row.store_id
            last_ts_str = row.last_ts
            lag_minutes = None
            status = "OK"

            if last_ts_str:
                try:
                    last_dt = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
                    lag_minutes = round((now - last_dt).total_seconds() / 60, 1)
                    if lag_minutes > STALE_FEED_MINUTES:
                        status = "STALE_FEED"
                except Exception:
                    status = "UNKNOWN"
            else:
                status = "NO_DATA"

            stores_health.append(StoreHealth(
                store_id=store_id,
                status=status,
                last_event_at=last_ts_str,
                lag_minutes=lag_minutes,
            ))

        if not stores_health:
            # No stores yet — that's OK on first boot
            overall = "ok"
        else:
            stale_count = sum(1 for s in stores_health if s.status == "STALE_FEED")
            overall = "degraded" if stale_count > 0 else "ok"

    except Exception as e:
        logger.error(f"[{trace_id}] Health check DB error: {e}")
        db_status = "error"
        overall = "unhealthy"

    latency_ms = round((time_module.time() - t0) * 1000, 1)
    logger.info(
        f"trace_id={trace_id} endpoint=/health latency_ms={latency_ms} "
        f"status={overall} db={db_status} status_code=200"
    )

    return HealthResponse(
        service="store-intelligence-api",
        status=overall,
        version=VERSION,
        database=db_status,
        stores=stores_health,
        checked_at=now_str,
    )
