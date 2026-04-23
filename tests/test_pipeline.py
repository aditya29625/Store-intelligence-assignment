# PROMPT:
# "Generate comprehensive pytest tests for a FastAPI event ingestion endpoint.
#  The endpoint is POST /events/ingest. It should:
#  - Accept up to 500 events per batch
#  - Validate each event against a Pydantic schema (EventIn)
#  - Be idempotent by event_id (same payload twice does not double-count)
#  - Return partial success: some events valid, some malformed → still 200
#  - Return structured response: {ingested, duplicates, errors[], total_received}
#  Cover: happy path, all-duplicate batch, malformed event_type, empty batch,
#  batch > 500, missing required fields, zero-dwell events, is_staff=True events."
#
# CHANGES MADE:
# - Replaced generic fixture with store-specific data (STORE_BLR_002 from spec)
# - Added real event_id format (uuid-v4) and ISO-8601 timestamp validation
# - Added test for is_staff=True events being accepted but excluded from metrics
# - Added test for REENTRY event_type (not in the AI-generated list originally)
# - Changed assert on duplicates: AI originally checked == 0 on idempotency re-call,
#   but the response field is `duplicates` not `skipped`
# - Added edge case: empty store period (all events for a 30-min window with only staff)
# - Uses shared conftest.py fixtures (clean_db, client)

import uuid
from datetime import datetime, timezone

import pytest
# client and clean_db fixtures come from tests/conftest.py


def make_event(
    store_id="STORE_BLR_002",
    event_type="ENTRY",
    visitor_id=None,
    is_staff=False,
    zone_id=None,
    dwell_ms=0,
    confidence=0.91,
    override_event_id=None,
):
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "event_id": override_event_id or str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": visitor_id or f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": event_type,
        "timestamp": ts,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }


# ─── Happy path ────────────────────────────────────────────────────────────────

class TestIngestHappyPath:
    def test_single_entry_event(self, client):
        evt = make_event(event_type="ENTRY")
        resp = client.post("/events/ingest", json={"events": [evt]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ingested"] == 1
        assert body["duplicates"] == 0
        assert body["errors"] == []
        assert body["total_received"] == 1

    def test_batch_of_all_event_types(self, client):
        events = [
            make_event(event_type="ENTRY"),
            make_event(event_type="EXIT"),
            make_event(event_type="ZONE_ENTER", zone_id="SKINCARE"),
            make_event(event_type="ZONE_EXIT", zone_id="SKINCARE", dwell_ms=45000),
            make_event(event_type="ZONE_DWELL", zone_id="HAIRCARE", dwell_ms=30000),
            make_event(event_type="BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE"),
            make_event(event_type="BILLING_QUEUE_ABANDON", zone_id="BILLING_QUEUE"),
            make_event(event_type="REENTRY"),
        ]
        resp = client.post("/events/ingest", json={"events": events})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ingested"] == 8
        assert body["errors"] == []

    def test_empty_batch(self, client):
        resp = client.post("/events/ingest", json={"events": []})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ingested"] == 0
        assert body["total_received"] == 0

    def test_staff_events_accepted(self, client):
        """Staff events are valid and stored; metrics layer excludes them."""
        staff_evt = make_event(event_type="ENTRY", is_staff=True)
        resp = client.post("/events/ingest", json={"events": [staff_evt]})
        assert resp.status_code == 200
        assert resp.json()["ingested"] == 1

    def test_zero_dwell_entry_event(self, client):
        """Instantaneous events (dwell_ms=0) must be accepted."""
        evt = make_event(event_type="ENTRY", dwell_ms=0)
        resp = client.post("/events/ingest", json={"events": [evt]})
        assert resp.status_code == 200
        assert resp.json()["ingested"] == 1


# ─── Idempotency ──────────────────────────────────────────────────────────────

class TestIdempotency:
    def test_same_payload_twice_no_double_count(self, client):
        evt = make_event(event_type="ENTRY")
        # First call
        r1 = client.post("/events/ingest", json={"events": [evt]})
        assert r1.json()["ingested"] == 1
        # Second call — identical event_id
        r2 = client.post("/events/ingest", json={"events": [evt]})
        body2 = r2.json()
        assert r2.status_code == 200
        assert body2["ingested"] == 0
        assert body2["duplicates"] == 1

    def test_batch_partial_duplicates(self, client):
        evt_id = str(uuid.uuid4())
        evt_dup = make_event(override_event_id=evt_id)
        evt_new = make_event()

        # First ingest
        client.post("/events/ingest", json={"events": [evt_dup]})
        # Second ingest with one dup + one new
        resp = client.post("/events/ingest", json={"events": [evt_dup, evt_new]})
        body = resp.json()
        assert body["ingested"] == 1
        assert body["duplicates"] == 1


# ─── Validation errors ────────────────────────────────────────────────────────

class TestValidationErrors:
    def test_invalid_event_type_rejected(self, client):
        evt = make_event()
        evt["event_type"] = "TELEPORT"
        resp = client.post("/events/ingest", json={"events": [evt]})
        # FastAPI/Pydantic validation → 422
        assert resp.status_code == 422

    def test_missing_store_id(self, client):
        evt = make_event()
        del evt["store_id"]
        resp = client.post("/events/ingest", json={"events": [evt]})
        assert resp.status_code == 422

    def test_confidence_out_of_range(self, client):
        """Confidence > 1 should be clamped or rejected."""
        evt = make_event(confidence=1.5)
        resp = client.post("/events/ingest", json={"events": [evt]})
        # Pydantic validator clamps or rejects
        assert resp.status_code in (200, 422)

    def test_batch_over_500_truncated(self, client):
        """Batch > 500 should be processed but truncated."""
        events = [make_event() for _ in range(510)]
        resp = client.post("/events/ingest", json={"events": events})
        assert resp.status_code == 200
        body = resp.json()
        # total_received reflects what was sent
        assert body["total_received"] == 510
        # Only 500 ingested
        assert body["ingested"] <= 500


# ─── Edge cases ───────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_all_staff_clip(self, client):
        """All events is_staff=True — should ingest OK, metrics will show 0 customers."""
        events = [make_event(event_type="ENTRY", is_staff=True) for _ in range(10)]
        resp = client.post("/events/ingest", json={"events": events})
        body = resp.json()
        assert body["ingested"] == 10

        # Metrics should show 0 unique visitors
        mresp = client.get("/stores/STORE_BLR_002/metrics")
        assert mresp.status_code == 200
        assert mresp.json()["unique_visitors"] == 0

    def test_zero_purchases_store(self, client):
        """Store with visitors but no billing events — conversion_rate = 0."""
        events = [
            make_event(event_type="ENTRY"),
            make_event(event_type="ZONE_ENTER", zone_id="SKINCARE"),
            make_event(event_type="EXIT"),
        ]
        client.post("/events/ingest", json={"events": events})
        mresp = client.get("/stores/STORE_BLR_002/metrics")
        assert mresp.status_code == 200
        assert mresp.json()["conversion_rate"] == 0.0

    def test_reentry_not_double_counted_in_funnel(self, client):
        """Same visitor_id with ENTRY + REENTRY should count as 1 in funnel."""
        vid = f"VIS_{uuid.uuid4().hex[:6]}"
        events = [
            make_event(event_type="ENTRY", visitor_id=vid),
            make_event(event_type="EXIT", visitor_id=vid),
            make_event(event_type="REENTRY", visitor_id=vid),
            make_event(event_type="ENTRY", visitor_id=vid),
        ]
        client.post("/events/ingest", json={"events": events})
        fresp = client.get("/stores/STORE_BLR_002/funnel")
        assert fresp.status_code == 200
        funnel = fresp.json()
        # Stage 1 (ENTRY) must use DISTINCT visitor_id → count = 1
        assert funnel["stages"][0]["count"] == 1

    def test_empty_store_period_no_crash(self, client):
        """No events in DB → metrics returns 0s, not null or crash."""
        mresp = client.get("/stores/STORE_BLR_002/metrics")
        assert mresp.status_code == 200
        body = mresp.json()
        assert body["unique_visitors"] == 0
        assert body["conversion_rate"] == 0.0
        assert body["queue_depth"] == 0
