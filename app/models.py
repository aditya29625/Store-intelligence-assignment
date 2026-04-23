"""
models.py — SQLAlchemy ORM models + Pydantic validation schemas.
"""

import uuid
from datetime import datetime
from typing import Optional, Any

from sqlalchemy import Column, String, Boolean, Float, Integer, Text, DateTime, JSON
from sqlalchemy.sql import func
from pydantic import BaseModel, Field, field_validator

from app.database import Base


# ---------------------------------------------------------------------------
# ORM Model
# ---------------------------------------------------------------------------

class Event(Base):
    __tablename__ = "events"

    event_id = Column(String, primary_key=True, index=True)
    store_id = Column(String, nullable=False, index=True)
    camera_id = Column(String, nullable=False)
    visitor_id = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)
    timestamp = Column(String, nullable=False, index=True)  # ISO-8601 string
    zone_id = Column(String, nullable=True)
    dwell_ms = Column(Integer, nullable=False, default=0)
    is_staff = Column(Boolean, nullable=False, default=False)
    confidence = Column(Float, nullable=False, default=0.9)
    metadata_json = Column("metadata", JSON, nullable=True)
    ingested_at = Column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Pydantic schemas (API I/O validation)
# ---------------------------------------------------------------------------

VALID_EVENT_TYPES = {
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
    "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY",
}


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: Optional[int] = None

    model_config = {"extra": "allow"}


class EventIn(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: str
    timestamp: str
    zone_id: Optional[str] = None
    dwell_ms: int = 0
    is_staff: bool = False
    confidence: float = Field(ge=0.0, le=1.0, default=0.9)
    metadata: Optional[EventMetadata] = None

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v: str) -> str:
        if v not in VALID_EVENT_TYPES:
            raise ValueError(f"Unknown event_type '{v}'. Valid: {VALID_EVENT_TYPES}")
        return v

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        return round(max(0.0, min(1.0, v)), 4)


class IngestRequest(BaseModel):
    events: list[EventIn]


class IngestResponse(BaseModel):
    ingested: int
    duplicates: int
    errors: list[dict]
    total_received: int


class ZoneDwell(BaseModel):
    zone_id: str
    avg_dwell_ms: float
    visit_count: int


class StoreMetrics(BaseModel):
    store_id: str
    date: str
    unique_visitors: int
    conversion_rate: float
    avg_dwell_per_zone: list[ZoneDwell]
    queue_depth: int
    abandonment_rate: float
    total_entries: int
    total_exits: int
    staff_events_excluded: int


class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    date: str
    stages: list[FunnelStage]
    sessions_total: int


class HeatmapZone(BaseModel):
    zone_id: str
    sku_zone: Optional[str]
    visit_count: int
    avg_dwell_ms: float
    normalised_score: float  # 0-100


class HeatmapResponse(BaseModel):
    store_id: str
    date: str
    zones: list[HeatmapZone]
    data_confidence: bool  # False if < 20 sessions


class Anomaly(BaseModel):
    anomaly_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    anomaly_type: str   # BILLING_QUEUE_SPIKE | CONVERSION_DROP | DEAD_ZONE | STALE_FEED
    severity: str       # INFO | WARN | CRITICAL
    description: str
    suggested_action: str
    detected_at: str
    store_id: str
    zone_id: Optional[str] = None
    value: Optional[float] = None
    threshold: Optional[float] = None


class AnomaliesResponse(BaseModel):
    store_id: str
    active_anomalies: list[Anomaly]
    checked_at: str


class StoreHealth(BaseModel):
    store_id: str
    status: str         # OK | STALE_FEED | NO_DATA
    last_event_at: Optional[str]
    lag_minutes: Optional[float]


class HealthResponse(BaseModel):
    service: str
    status: str
    version: str
    database: str
    stores: list[StoreHealth]
    checked_at: str
