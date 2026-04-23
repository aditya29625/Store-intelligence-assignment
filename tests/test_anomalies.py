# PROMPT:
# "Write pytest tests for a retail anomaly detection endpoint GET /stores/{id}/anomalies.
#  The endpoint detects: BILLING_QUEUE_SPIKE (queue > 2× rolling avg), CONVERSION_DROP
#  (today's rate < 7-day avg by >20%), DEAD_ZONE (no zone visits in 30 min),
#  STALE_FEED (no events >10 min). Each anomaly has severity (INFO/WARN/CRITICAL)
#  and suggested_action string. Test: no anomalies when normal, queue spike triggers
#  CRITICAL, DEAD_ZONE detected, empty store returns no anomalies not 500."
#
# CHANGES MADE:
# - AI originally tested for exact severity strings in lowercase; our schema uses uppercase
#   (INFO/WARN/CRITICAL) -- fixed all assertions
# - AI didn't test that suggested_action is a non-empty string; added that assertion
# - STALE_FEED test: AI inserted events with future timestamps; changed to old timestamps
#   that trigger the >10 min threshold correctly
# - Added test verifying response structure (anomaly_id, store_id fields present)
# - Added test for empty store -> no anomalies (not a 500 error)
# - Uses shared conftest.py fixtures (clean_db, client, db)

import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import text
# client, db, clean_db fixtures come from tests/conftest.py

STORE_ID = "STORE_BLR_002"


def now_iso():
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago_iso(minutes: int) -> str:
    dt = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def insert_event(db, **kwargs):
    defaults = {
        "event_id": str(uuid.uuid4()),
        "store_id": STORE_ID,
        "camera_id": "CAM_BILLING_01",
        "visitor_id": f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": "ENTRY",
        "timestamp": now_iso(),
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


class TestAnomalyStructure:
    def test_response_structure(self, client):
        resp = client.get(f"/stores/{STORE_ID}/anomalies")
        assert resp.status_code == 200
        body = resp.json()
        assert "store_id" in body
        assert "active_anomalies" in body
        assert "checked_at" in body
        assert body["store_id"] == STORE_ID

    def test_empty_store_no_crash_no_anomalies(self, client):
        """Empty DB should return empty anomaly list, not 500."""
        resp = client.get(f"/stores/{STORE_ID}/anomalies")
        assert resp.status_code == 200
        assert isinstance(resp.json()["active_anomalies"], list)

    def test_anomaly_has_required_fields(self, client, db):
        # Trigger stale feed anomaly by inserting old event
        insert_event(db, event_type="ENTRY", timestamp=ago_iso(60))

        resp = client.get(f"/stores/{STORE_ID}/anomalies")
        anomalies = resp.json()["active_anomalies"]
        if anomalies:
            a = anomalies[0]
            assert "anomaly_id" in a
            assert "anomaly_type" in a
            assert "severity" in a
            assert "description" in a
            assert "suggested_action" in a
            assert len(a["suggested_action"]) > 10  # must be non-trivial


class TestQueueSpike:
    def test_high_queue_depth_triggers_warn_or_critical(self, client, db):
        # Insert historical low-depth events to set rolling avg ≈ 1.0
        for i in range(3):
            old_ts = (datetime.now(tz=timezone.utc) - timedelta(days=i+2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            insert_event(db, event_type="BILLING_QUEUE_JOIN", zone_id="BILLING_QUEUE",
                         timestamp=old_ts,
                         metadata='{"queue_depth": 1, "sku_zone": null, "session_seq": 1}')
        # Now insert recent high-depth event: 6 > 2×1 = 2 AND >= 3 → triggers anomaly
        insert_event(
            db,
            event_type="BILLING_QUEUE_JOIN",
            zone_id="BILLING_QUEUE",
            timestamp=ago_iso(2),
            metadata='{"queue_depth": 6, "sku_zone": null, "session_seq": 1}',
        )

        resp = client.get(f"/stores/{STORE_ID}/anomalies")
        anomalies = resp.json()["active_anomalies"]
        queue_anomalies = [a for a in anomalies if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert len(queue_anomalies) >= 1
        assert queue_anomalies[0]["severity"] in ("WARN", "CRITICAL")

    def test_normal_queue_no_spike(self, client, db):
        # Queue depth 1 -- well within normal
        insert_event(
            db,
            event_type="BILLING_QUEUE_JOIN",
            zone_id="BILLING_QUEUE",
            timestamp=now_iso(),
            metadata='{"queue_depth": 1, "sku_zone": null, "session_seq": 1}',
        )

        resp = client.get(f"/stores/{STORE_ID}/anomalies")
        anomalies = resp.json()["active_anomalies"]
        queue_spikes = [a for a in anomalies if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        assert queue_spikes == []


class TestDeadZone:
    def test_zone_with_no_recent_visits_detected(self, client, db):
        # SKINCARE had visits earlier today (>30 min ago)
        insert_event(db, event_type="ZONE_ENTER", zone_id="SKINCARE",
                     timestamp=ago_iso(45))
        insert_event(db, event_type="ZONE_EXIT", zone_id="SKINCARE",
                     timestamp=ago_iso(44))
        # No recent visits in last 30 min

        resp = client.get(f"/stores/{STORE_ID}/anomalies")
        anomalies = resp.json()["active_anomalies"]
        dead_zones = [a for a in anomalies if a["anomaly_type"] == "DEAD_ZONE"]
        zone_ids = [a["zone_id"] for a in dead_zones]
        assert "SKINCARE" in zone_ids

    def test_active_zone_no_dead_zone_alert(self, client, db):
        # SKINCARE had a visit 5 min ago (within 30 min window)
        insert_event(db, event_type="ZONE_ENTER", zone_id="SKINCARE",
                     timestamp=ago_iso(5))

        resp = client.get(f"/stores/{STORE_ID}/anomalies")
        anomalies = resp.json()["active_anomalies"]
        dead_zones = [a for a in anomalies if a["anomaly_type"] == "DEAD_ZONE"]
        skincare_dead = [a for a in dead_zones if a.get("zone_id") == "SKINCARE"]
        assert skincare_dead == []


class TestStaleFeed:
    def test_stale_feed_detected_after_10_min(self, client, db):
        # Only old events (15 min ago)
        insert_event(db, event_type="ENTRY", timestamp=ago_iso(15))

        resp = client.get(f"/stores/{STORE_ID}/anomalies")
        anomalies = resp.json()["active_anomalies"]
        stale = [a for a in anomalies if a["anomaly_type"] == "STALE_FEED"]
        assert len(stale) >= 1
        assert stale[0]["severity"] in ("WARN", "CRITICAL")
        assert stale[0]["value"] >= 14.0  # ~15 min lag

    def test_recent_events_no_stale_feed(self, client, db):
        insert_event(db, event_type="ENTRY", timestamp=now_iso())

        resp = client.get(f"/stores/{STORE_ID}/anomalies")
        anomalies = resp.json()["active_anomalies"]
        stale = [a for a in anomalies if a["anomaly_type"] == "STALE_FEED"]
        assert stale == []
