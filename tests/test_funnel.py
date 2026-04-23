# PROMPT:
# "Write pytest tests for a retail conversion funnel endpoint GET /stores/{id}/funnel.
#  The funnel has 4 stages: ENTRY → ZONE_VISIT → BILLING_QUEUE → PURCHASE.
#  Each stage must use visitor_id as the unit (session-level, not event count).
#  Re-entry: same visitor_id appearing multiple times must count once.
#  Test: normal funnel, full conversion, zero billing, re-entry deduplication,
#  funnel drop-off percentages are correct, empty store returns all zeros."
#
# CHANGES MADE:
# - AI generated tests using separate PURCHASE event type that doesn't exist in schema;
#   replaced with the billing zone proxy logic our funnel.py actually uses
# - Fixed drop-off calculation test: AI had rounding to 2 decimals, our code uses 1
# - Added test verifying stages list has exactly 4 entries
# - Split "re-entry" test into cleaner sub-assertions (AI had one assert)
# - Added test for store with only staff events (funnel should be all zeros)
# - Uses shared conftest.py fixtures (clean_db, client, db)

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
# client, db, clean_db fixtures come from tests/conftest.py

STORE_ID = "STORE_BLR_002"


def ts():
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def insert_event(db, **kwargs):
    defaults = {
        "event_id": str(uuid.uuid4()),
        "store_id": STORE_ID,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": "ENTRY",
        "timestamp": ts(),
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": 0,
        "confidence": 0.9,
        "metadata": "{}",
    }
    defaults.update(kwargs)
    db.execute(text("""
        INSERT OR IGNORE INTO events
            (event_id, store_id, camera_id, visitor_id, event_type, timestamp,
             zone_id, dwell_ms, is_staff, confidence, metadata)
        VALUES (:event_id, :store_id, :camera_id, :visitor_id, :event_type, :timestamp,
                :zone_id, :dwell_ms, :is_staff, :confidence, :metadata)
    """), defaults)
    db.commit()


class TestFunnelStructure:
    def test_funnel_has_four_stages(self, client):
        resp = client.get(f"/stores/{STORE_ID}/funnel")
        assert resp.status_code == 200
        stages = resp.json()["stages"]
        assert len(stages) == 4
        names = [s["stage"] for s in stages]
        assert names == ["ENTRY", "ZONE_VISIT", "BILLING_QUEUE", "PURCHASE"]

    def test_empty_store_all_zeros(self, client):
        resp = client.get(f"/stores/{STORE_ID}/funnel")
        assert resp.status_code == 200
        for stage in resp.json()["stages"]:
            assert stage["count"] == 0

    def test_response_includes_sessions_total(self, client, db):
        insert_event(db, event_type="ENTRY")
        resp = client.get(f"/stores/{STORE_ID}/funnel")
        assert "sessions_total" in resp.json()
        assert resp.json()["sessions_total"] == 1


class TestFunnelCounts:
    def test_full_conversion_funnel(self, client, db):
        vid = f"VIS_{uuid.uuid4().hex[:6]}"
        insert_event(db, event_type="ENTRY", visitor_id=vid)
        insert_event(db, event_type="ZONE_ENTER", visitor_id=vid, zone_id="SKINCARE")
        insert_event(db, event_type="BILLING_QUEUE_JOIN", visitor_id=vid,
                     zone_id="BILLING_QUEUE", metadata='{"queue_depth": 1}')
        insert_event(db, event_type="ZONE_EXIT", visitor_id=vid,
                     zone_id="BILLING", dwell_ms=60000)

        resp = client.get(f"/stores/{STORE_ID}/funnel")
        stages = {s["stage"]: s for s in resp.json()["stages"]}
        assert stages["ENTRY"]["count"] == 1
        assert stages["ZONE_VISIT"]["count"] == 1
        assert stages["BILLING_QUEUE"]["count"] == 1

    def test_visitors_who_skip_zone_visit(self, client, db):
        # 5 entries, only 2 zone visits, 1 billing
        vids = [f"VIS_{uuid.uuid4().hex[:6]}" for _ in range(5)]
        for vid in vids:
            insert_event(db, event_type="ENTRY", visitor_id=vid)
        for vid in vids[:2]:
            insert_event(db, event_type="ZONE_ENTER", visitor_id=vid, zone_id="SKINCARE")
        insert_event(db, event_type="BILLING_QUEUE_JOIN", visitor_id=vids[0],
                     zone_id="BILLING_QUEUE", metadata='{"queue_depth": 0}')

        resp = client.get(f"/stores/{STORE_ID}/funnel")
        stages = {s["stage"]: s for s in resp.json()["stages"]}
        assert stages["ENTRY"]["count"] == 5
        assert stages["ZONE_VISIT"]["count"] == 2
        assert stages["BILLING_QUEUE"]["count"] == 1

    def test_all_staff_funnel_zeros(self, client, db):
        for _ in range(5):
            insert_event(db, event_type="ENTRY", is_staff=1)
            insert_event(db, event_type="ZONE_ENTER", is_staff=1, zone_id="SKINCARE")

        resp = client.get(f"/stores/{STORE_ID}/funnel")
        stages = resp.json()["stages"]
        # ENTRY stage for customers = 0 (staff entries excluded)
        assert stages[0]["count"] == 0


class TestFunnelReentry:
    def test_reentry_visitor_counted_once(self, client, db):
        """Same visitor_id entering twice should count as 1 unique session."""
        vid = f"VIS_{uuid.uuid4().hex[:6]}"
        insert_event(db, event_type="ENTRY", visitor_id=vid)
        insert_event(db, event_type="EXIT", visitor_id=vid)
        insert_event(db, event_type="REENTRY", visitor_id=vid)
        insert_event(db, event_type="ENTRY", visitor_id=vid)

        resp = client.get(f"/stores/{STORE_ID}/funnel")
        assert resp.status_code == 200
        entry_stage = resp.json()["stages"][0]
        assert entry_stage["count"] == 1  # DISTINCT visitor_id

    def test_multiple_visitors_some_reentry(self, client, db):
        v1 = "VIS_aaa111"
        v2 = "VIS_bbb222"
        v3 = "VIS_ccc333"

        insert_event(db, event_type="ENTRY", visitor_id=v1)
        insert_event(db, event_type="ENTRY", visitor_id=v2)
        insert_event(db, event_type="ENTRY", visitor_id=v3)
        # v1 re-enters — still only 3 unique
        insert_event(db, event_type="REENTRY", visitor_id=v1)
        insert_event(db, event_type="ENTRY", visitor_id=v1)

        resp = client.get(f"/stores/{STORE_ID}/funnel")
        assert resp.json()["stages"][0]["count"] == 3


class TestDropOffPercentage:
    def test_drop_off_calculated_correctly(self, client, db):
        """4 entries, 2 zone visits → ZONE_VISIT drop-off = 50.0%"""
        vids = [f"VIS_{uuid.uuid4().hex[:6]}" for _ in range(4)]
        for vid in vids:
            insert_event(db, event_type="ENTRY", visitor_id=vid)
        for vid in vids[:2]:
            insert_event(db, event_type="ZONE_ENTER", visitor_id=vid, zone_id="SKINCARE")

        resp = client.get(f"/stores/{STORE_ID}/funnel")
        stages = {s["stage"]: s for s in resp.json()["stages"]}
        assert stages["ENTRY"]["drop_off_pct"] == 0.0
        assert stages["ZONE_VISIT"]["drop_off_pct"] == 50.0
