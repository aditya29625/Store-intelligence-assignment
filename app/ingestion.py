"""
ingestion.py — POST /events/ingest handler.

Design decisions:
- Idempotent: event_id is the primary key. Duplicate inserts are silently ignored.
- Partial success: malformed events return structured errors in the response.
- Batch insert: uses SQLite executemany for performance on large batches.
- Max batch size: 500 events per request.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.database import get_db
from app.models import EventIn, IngestRequest, IngestResponse, Event, EventMetadata
from app.ws import broadcast_event

logger = logging.getLogger("ingestion")

router = APIRouter()

MAX_BATCH_SIZE = 500


@router.post("/events/ingest", response_model=IngestResponse)
async def ingest_events(
    payload: IngestRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    import time
    t0 = time.time()
    trace_id = request.headers.get("X-Trace-Id", "?")

    events = payload.events
    if len(events) > MAX_BATCH_SIZE:
        # Truncate silently — document in response
        logger.warning(f"[{trace_id}] Batch size {len(events)} > {MAX_BATCH_SIZE}; truncating.")
        events = events[:MAX_BATCH_SIZE]

    ingested = 0
    duplicates = 0
    errors: list[dict] = []

    rows_to_insert = []
    for i, evt in enumerate(events):
        try:
            meta = evt.metadata.model_dump() if evt.metadata else {}
            rows_to_insert.append({
                "event_id": evt.event_id,
                "store_id": evt.store_id,
                "camera_id": evt.camera_id,
                "visitor_id": evt.visitor_id,
                "event_type": evt.event_type,
                "timestamp": evt.timestamp,
                "zone_id": evt.zone_id,
                "dwell_ms": evt.dwell_ms,
                "is_staff": evt.is_staff,
                "confidence": evt.confidence,
                "metadata": meta,
            })
        except Exception as e:
            errors.append({"index": i, "event_id": getattr(evt, "event_id", None), "error": str(e)})

    # Bulk insert with INSERT OR IGNORE for SQLite idempotency
    if rows_to_insert:
        try:
            for row in rows_to_insert:
                result = db.execute(
                    text("""
                        INSERT OR IGNORE INTO events
                            (event_id, store_id, camera_id, visitor_id, event_type,
                             timestamp, zone_id, dwell_ms, is_staff, confidence, metadata)
                        VALUES
                            (:event_id, :store_id, :camera_id, :visitor_id, :event_type,
                             :timestamp, :zone_id, :dwell_ms, :is_staff, :confidence, :metadata)
                    """),
                    {**row, "metadata": str(row["metadata"])},
                )
                if result.rowcount == 1:
                    ingested += 1
                    # Broadcast to WebSocket subscribers (non-blocking)
                    try:
                        await broadcast_event(row)
                    except Exception:
                        pass
                else:
                    duplicates += 1
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"[{trace_id}] Bulk insert failed: {e}")
            raise

    latency_ms = round((time.time() - t0) * 1000, 1)
    logger.info(
        f"trace_id={trace_id} endpoint=/events/ingest "
        f"event_count={len(events)} ingested={ingested} duplicates={duplicates} "
        f"errors={len(errors)} latency_ms={latency_ms} status_code=200"
    )

    return IngestResponse(
        ingested=ingested,
        duplicates=duplicates,
        errors=errors,
        total_received=len(payload.events),
    )
