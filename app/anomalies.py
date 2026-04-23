"""
anomalies.py — GET /stores/{store_id}/anomalies

Detects operational anomalies:
1. BILLING_QUEUE_SPIKE — current queue depth > 2× rolling average
2. CONVERSION_DROP — today's conversion rate < 7-day average by > 20%
3. DEAD_ZONE — no zone visits in last 30 minutes for a zone that normally has traffic
4. STALE_FEED — no events received for this store in > 10 minutes

Each anomaly has: severity (INFO/WARN/CRITICAL), description, suggested_action.
"""

import json
import logging
import time as time_module
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.models import AnomaliesResponse, Anomaly

logger = logging.getLogger("anomalies")
router = APIRouter()

# Thresholds
QUEUE_SPIKE_MULTIPLIER = 2.0    # queue > 2× rolling avg → CRITICAL
CONVERSION_DROP_PCT = 20.0      # 20% below 7-day avg → WARN
DEAD_ZONE_MINUTES = 30          # no visits in 30 min → INFO
STALE_FEED_MINUTES = 10         # no events in 10 min → WARN


@router.get("/stores/{store_id}/anomalies", response_model=AnomaliesResponse)
async def get_anomalies(
    store_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    t0 = time_module.time()
    trace_id = request.headers.get("X-Trace-Id", "?")

    now = datetime.now(tz=timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    today_start = now.strftime("%Y-%m-%dT00:00:00Z")
    today_end = now.strftime("%Y-%m-%dT23:59:59Z")
    lookback_30m = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lookback_7d = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    anomalies = []

    try:
        # ----------------------------------------------------------------
        # 1. BILLING_QUEUE_SPIKE
        # ----------------------------------------------------------------
        # Current queue depth (most recent BILLING_QUEUE_JOIN in last 30 min)
        queue_row = db.execute(text("""
            SELECT metadata FROM events
            WHERE store_id = :sid
              AND event_type = 'BILLING_QUEUE_JOIN'
              AND timestamp >= :lb
            ORDER BY timestamp DESC LIMIT 1
        """), {"sid": store_id, "lb": lookback_30m}).fetchone()

        current_depth = 0
        if queue_row and queue_row.metadata:
            try:
                meta = json.loads(queue_row.metadata) if isinstance(queue_row.metadata, str) else queue_row.metadata
                # Handle Python dict repr stored as string
                if isinstance(meta, str):
                    import ast
                    meta = ast.literal_eval(meta)
                current_depth = meta.get("queue_depth") or 0
            except Exception:
                pass

        # Rolling average queue depth — parse metadata in Python to handle format variations
        avg_rows = db.execute(text("""
            SELECT metadata FROM events
            WHERE store_id = :sid
              AND event_type = 'BILLING_QUEUE_JOIN'
              AND timestamp BETWEEN :lb7 AND :now
              AND metadata IS NOT NULL
        """), {"sid": store_id, "lb7": lookback_7d, "now": now_str}).fetchall()

        depths = []
        for r in avg_rows:
            if r.metadata:
                try:
                    meta = json.loads(r.metadata) if isinstance(r.metadata, str) else r.metadata
                    if isinstance(meta, str):
                        import ast
                        meta = ast.literal_eval(meta)
                    d = meta.get("queue_depth")
                    if d is not None:
                        depths.append(float(d))
                except Exception:
                    pass

        avg_depth = sum(depths) / max(len(depths), 1) if depths else 1.0

        if current_depth > QUEUE_SPIKE_MULTIPLIER * avg_depth and current_depth >= 3:
            anomalies.append(Anomaly(
                anomaly_id=str(uuid.uuid4()),
                anomaly_type="BILLING_QUEUE_SPIKE",
                severity="CRITICAL",
                description=f"Queue depth is {current_depth} (avg: {avg_depth:.1f}). Spike detected.",
                suggested_action="Open additional billing counter immediately. Alert floor manager.",
                detected_at=now_str,
                store_id=store_id,
                zone_id="BILLING_QUEUE",
                value=float(current_depth),
                threshold=round(QUEUE_SPIKE_MULTIPLIER * avg_depth, 1),
            ))
        elif current_depth > avg_depth * 1.5 and current_depth >= 2:
            anomalies.append(Anomaly(
                anomaly_id=str(uuid.uuid4()),
                anomaly_type="BILLING_QUEUE_SPIKE",
                severity="WARN",
                description=f"Queue depth {current_depth} rising above average ({avg_depth:.1f}).",
                suggested_action="Monitor billing queue. Consider opening second counter.",
                detected_at=now_str,
                store_id=store_id,
                zone_id="BILLING_QUEUE",
                value=float(current_depth),
                threshold=round(avg_depth * 1.5, 1),
            ))

        # ----------------------------------------------------------------
        # 2. CONVERSION_DROP
        # ----------------------------------------------------------------
        # Today's conversion rate
        today_entry_row = db.execute(text("""
            SELECT COUNT(DISTINCT visitor_id) as cnt FROM events
            WHERE store_id = :sid AND event_type = 'ENTRY' AND is_staff = 0
              AND timestamp BETWEEN :ts AND :te
        """), {"sid": store_id, "ts": today_start, "te": today_end}).fetchone()

        today_billing_row = db.execute(text("""
            SELECT COUNT(DISTINCT visitor_id) as cnt FROM events
            WHERE store_id = :sid
              AND event_type IN ('BILLING_QUEUE_JOIN', 'ZONE_ENTER')
              AND zone_id IN ('BILLING', 'BILLING_QUEUE') AND is_staff = 0
              AND timestamp BETWEEN :ts AND :te
        """), {"sid": store_id, "ts": today_start, "te": today_end}).fetchone()

        today_visitors = today_entry_row.cnt if today_entry_row else 0
        today_billing = today_billing_row.cnt if today_billing_row else 0
        today_cr = today_billing / max(today_visitors, 1)

        # 7-day average conversion rate
        hist_row = db.execute(text("""
            SELECT
                COUNT(DISTINCT e1.visitor_id) FILTER (WHERE e1.event_type='ENTRY') as entries,
                COUNT(DISTINCT e2.visitor_id) FILTER (WHERE e2.event_type IN ('BILLING_QUEUE_JOIN','ZONE_ENTER') AND e2.zone_id IN ('BILLING','BILLING_QUEUE')) as billing
            FROM events e1
            LEFT JOIN events e2 ON e1.store_id = e2.store_id AND e1.visitor_id = e2.visitor_id
            WHERE e1.store_id = :sid AND e1.is_staff = 0
              AND e1.timestamp BETWEEN :lb7 AND :now
        """), {"sid": store_id, "lb7": lookback_7d, "now": today_start}).fetchone()

        hist_entries = hist_row.entries if hist_row and hist_row.entries else 0
        hist_billing = hist_row.billing if hist_row and hist_row.billing else 0
        hist_cr = hist_billing / max(hist_entries, 1) if hist_entries > 0 else today_cr

        drop_pct = ((hist_cr - today_cr) / max(hist_cr, 0.01)) * 100

        if drop_pct >= CONVERSION_DROP_PCT and today_visitors >= 5:
            severity = "CRITICAL" if drop_pct >= 40 else "WARN"
            anomalies.append(Anomaly(
                anomaly_id=str(uuid.uuid4()),
                anomaly_type="CONVERSION_DROP",
                severity=severity,
                description=f"Conversion rate dropped {drop_pct:.1f}% vs 7-day avg ({hist_cr:.1%} → {today_cr:.1%}).",
                suggested_action="Check for product availability issues, staff engagement, or promotional effectiveness.",
                detected_at=now_str,
                store_id=store_id,
                value=round(today_cr, 4),
                threshold=round(hist_cr, 4),
            ))

        # ----------------------------------------------------------------
        # 3. DEAD_ZONE — zones with no visits in last 30 min
        # ----------------------------------------------------------------
        # Get zones that had visits earlier today
        active_zones_row = db.execute(text("""
            SELECT DISTINCT zone_id FROM events
            WHERE store_id = :sid
              AND event_type IN ('ZONE_ENTER', 'ZONE_EXIT')
              AND zone_id NOT IN ('BILLING', 'BILLING_QUEUE', 'ENTRY', 'EXIT')
              AND zone_id IS NOT NULL
              AND is_staff = 0
              AND timestamp BETWEEN :ts AND :te
        """), {"sid": store_id, "ts": today_start, "te": today_end}).fetchall()
        all_active_zones = {r.zone_id for r in active_zones_row}

        # Zones with visits in last 30 min
        recent_zones_row = db.execute(text("""
            SELECT DISTINCT zone_id FROM events
            WHERE store_id = :sid
              AND event_type IN ('ZONE_ENTER', 'ZONE_EXIT')
              AND zone_id NOT IN ('BILLING', 'BILLING_QUEUE', 'ENTRY', 'EXIT')
              AND zone_id IS NOT NULL
              AND is_staff = 0
              AND timestamp >= :lb
        """), {"sid": store_id, "lb": lookback_30m}).fetchall()
        recently_active = {r.zone_id for r in recent_zones_row}

        dead_zones = all_active_zones - recently_active
        for zone in dead_zones:
            anomalies.append(Anomaly(
                anomaly_id=str(uuid.uuid4()),
                anomaly_type="DEAD_ZONE",
                severity="INFO",
                description=f"Zone '{zone}' has had no customer visits in the last {DEAD_ZONE_MINUTES} minutes.",
                suggested_action=f"Check if merchandise in {zone} needs restocking or repositioning.",
                detected_at=now_str,
                store_id=store_id,
                zone_id=zone,
            ))

        # ----------------------------------------------------------------
        # 4. STALE_FEED (checked in health.py too, duplicated here for anomaly surface)
        # ----------------------------------------------------------------
        last_event_row = db.execute(text("""
            SELECT MAX(timestamp) as last_ts FROM events
            WHERE store_id = :sid
        """), {"sid": store_id}).fetchone()

        if last_event_row and last_event_row.last_ts:
            try:
                last_ts_str = last_event_row.last_ts
                last_ts = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
                lag = (now - last_ts).total_seconds() / 60
                if lag > STALE_FEED_MINUTES:
                    anomalies.append(Anomaly(
                        anomaly_id=str(uuid.uuid4()),
                        anomaly_type="STALE_FEED",
                        severity="WARN" if lag < 60 else "CRITICAL",
                        description=f"No events received for {lag:.0f} minutes. Last event at {last_ts_str}.",
                        suggested_action="Check camera health, network connectivity, and pipeline process status.",
                        detected_at=now_str,
                        store_id=store_id,
                        value=round(lag, 1),
                        threshold=float(STALE_FEED_MINUTES),
                    ))
            except Exception:
                pass

    except Exception as e:
        logger.error(f"[{trace_id}] Anomaly detection failed: {e}")
        raise HTTPException(status_code=503, detail={"error": "Database unavailable", "code": "DB_UNAVAILABLE"})

    latency_ms = round((time_module.time() - t0) * 1000, 1)
    logger.info(
        f"trace_id={trace_id} store_id={store_id} endpoint=/anomalies "
        f"anomaly_count={len(anomalies)} latency_ms={latency_ms} status_code=200"
    )

    return AnomaliesResponse(
        store_id=store_id,
        active_anomalies=anomalies,
        checked_at=now_str,
    )
