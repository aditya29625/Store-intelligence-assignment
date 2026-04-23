# DESIGN.md — Apex Retail Store Intelligence System

## Architecture Overview

The system is a four-stage pipeline: raw CCTV footage → structured event stream → analytics API → live dashboard. Every design decision was made to optimise for accuracy of a single metric: **offline conversion rate**.

```
CCTV Clips
    ↓
pipeline/detect.py  (YOLOv8m + ByteTrack)
    ↓
pipeline/emit.py    (structured JSONL events)
    ↓
POST /events/ingest (FastAPI + SQLite)
    ↓
GET /stores/{id}/metrics | /funnel | /heatmap | /anomalies | /health
    ↓
WebSocket /ws → React Dashboard (live)
```

### Stage 1: Detection Layer

**Model**: YOLOv8m (medium variant). Processes every Nth frame (default: 3) at 15fps → effective rate of 5fps. YOLO's built-in **ByteTrack** integration (`model.track(persist=True)`) provides smooth trajectory tracking with track ID persistence across frames.

**Re-ID across exits**: When a track is lost (person exits frame), the track state is archived in a `ReIDStore`. When a new detection appears near the same point with similar appearance (BGR histogram correlation + spatial proximity), it is matched back to the original `visitor_id` and a `REENTRY` event is emitted instead of a second `ENTRY`. This directly addresses the reentry inflation problem described in the spec.

**Direction detection**: Entry vs. exit is determined by the centroid Y-trajectory of a track on the entry camera. A person moving from a high Y (near entry threshold) to a low Y (deeper into store) is classified as `ENTRY`. Reverse trajectory = `EXIT`. This is more robust than a hard line because it handles camera tilt variation.

**Group handling**: YOLO detects individuals, not groups. Three people entering simultaneously are detected as three separate bounding boxes with separate track IDs → three `ENTRY` events. This is the correct behaviour as specified.

**Partial occlusion**: Confidence scores below a threshold are NOT dropped. Low-confidence detections are emitted with their raw confidence value. This allows downstream systems to filter by confidence threshold, and keeps the data honest. Dropping low-confidence events silently inflates apparent accuracy.

**Staff detection**: Two-signal heuristic:
1. **Colour**: HSV colour range matching against common retail uniform colours (navy blue, red aprons, black). Analysed on the torso crop of each detected person.
2. **Zone persistence**: A track that visits ≥4 distinct zones over ≥90 observed frames is flagged as staff (customers typically visit 1-3 zones; staff traverse all).

Both signals set `is_staff=True` on emitted events. All metrics endpoints filter `WHERE is_staff = 0`.

### Stage 2: Event Schema

Events are design around the **session** as the atomic unit. Each event references a `visitor_id` token that persists across the entire visit (and re-visits via Re-ID). This makes session-level analytics trivially `GROUP BY visitor_id + DATE(timestamp)` at query time, without needing a separate session table.

The schema includes `confidence` as a first-class field rather than a filter: this is intentional. Suppressing low-confidence detections makes the system look better than it is. Exposing confidence allows operators to tune thresholds post-deployment.

### Stage 3: Intelligence API

**FastAPI** was chosen for: async I/O (critical for WebSocket broadcast), Pydantic schema validation (catch malformed events at boundary), auto-generated OpenAPI docs (reduces integration friction), and minimal boilerplate for route definition.

**SQLite** is used in development/Docker. The schema is event-centric: every raw event is stored verbatim, and all metrics are computed via SQL aggregation at query time. This avoids the pre-aggregation cache-staleness problem and means `GET /metrics` always returns current data.

**Idempotency**: The `events` table uses `event_id` as the primary key with `INSERT OR IGNORE` semantics. POSTing the same batch twice returns `{ingested: 0, duplicates: N}` without error. This is safe for pipeline retry scenarios.

**Structured logging**: Every request logs `trace_id`, `store_id`, `endpoint`, `latency_ms`, `event_count` (for ingest), `status_code`. The `trace_id` is read from the `X-Trace-Id` request header if present, otherwise generated. This enables end-to-end tracing from pipeline → API.

**Graceful degradation**: The database middleware catches all DB exceptions and returns HTTP 503 with `{"error": "...", "code": "DB_UNAVAILABLE"}`. No raw tracebacks reach the client.

### Stage 4: Live Dashboard

React + Vite + Recharts. WebSocket connection to `/ws` receives raw events as they are ingested. The dashboard maintains a rolling in-memory state of metrics (visitor count, zone heat, funnel) without polling. A "replay" mode re-feeds `sample_events.jsonl` through the API at configurable speed to demonstrate live updates without needing a live camera.

---

## AI-Assisted Decisions

### 1. Event Schema — `confidence` as a first-class field

**AI suggestion (Claude)**: Strip events below confidence 0.5 at the pipeline level to reduce noise.  
**My decision**: Reject this. Emit all detections with raw confidence values. Let the API consumer decide the threshold.  
**Reasoning**: In a production CV system, dropped detections are invisible problems. If we suppress low-confidence events, we have no way to debug systematic misses (e.g., a camera angle that consistently produces 0.4 confidence). Keeping sub-threshold events — flagged but present — is the production-aware approach.

### 2. Storage — SQLite vs PostgreSQL

**AI suggestion (ChatGPT)**: Use PostgreSQL with a dedicated sessions table pre-computed by a background worker, since per-request SQL aggregation would be slow.  
**My decision**: SQLite + real-time SQL aggregation. The challenge specifies 5 stores × 60 minutes × 3 cameras = ~30k events/day typical. This fits easily in SQLite's performance envelope. Pre-computed sessions introduce cache staleness — exactly what the spec says to avoid ("Real-time — not cached from yesterday").  
**Override condition**: At 40 live stores, I would switch to PostgreSQL + materialized views refreshed every 60 seconds as stated in CHOICES.md.

### 3. Re-ID approach — appearance histogram vs. embedding model

**AI suggestion (Gemini)**: Use a torchreid OSNet-0.25 model to generate 512-d appearance embeddings for proper Re-ID, similar to DeepSORT.  
**My decision**: Use BGR histogram correlation + spatial proximity instead.  
**Reasoning**: The footage is 1080p at 15fps with pre-applied face blur (per spec). Face-based Re-ID is unavailable. OSNet requires GPU for reasonable throughput and introduces a ~200MB model dependency. BGR histograms capture clothing colour — the primary discriminating feature in retail settings — at near-zero compute cost. For the timescales involved (re-entry within 30 minutes), this is sufficient. If I had ≥4 hours and a GPU, I'd switch to OSNet.
