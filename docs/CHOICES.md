# CHOICES.md — Three Key Decisions

---

## Decision 1: Detection Model — YOLOv8m

### Options Considered

| Model | Pros | Cons |
|-------|------|------|
| **YOLOv8m** (chosen) | Best accuracy/speed on CPU, built-in ByteTrack, wide community support | Larger than YOLOv8n (52MB vs 6MB) |
| YOLOv8n (nano) | Fastest, lightest | ~4% lower mAP, struggles with occlusion |
| RT-DETR (Transformer) | Higher accuracy, better occlusion handling | 2× slower, no built-in tracker |
| MediaPipe Pose | Good for single person, human-specific | Poor multi-person tracking, no track IDs |
| YOLOv9 | Marginal accuracy improvement | Less mature tooling, no ultralytics integration |

### What AI Suggested

I asked Claude to compare YOLOv8m vs RT-DETR for a retail tracking scenario. Claude recommended RT-DETR for higher accuracy on partially occluded people in crowded scenes, citing its transformer attention mechanism that handles overlapping bounding boxes better.

### What I Chose and Why

**YOLOv8m** — and I partially disagreed with the AI.

The spec states footage is 1080p at 15fps. Processing in real time requires roughly 15 fps throughput. On a CPU, RT-DETR achieves ~8 fps on 1080p; YOLOv8m achieves ~14 fps. For a system that "must run via docker compose up" on commodity hardware, throughput beats marginal accuracy.

More importantly, YOLOv8m's native ByteTrack integration means tracking state is maintained by the same optimised CUDA/CPU pathway as detection. With RT-DETR, I would need to bolt on a separate ByteTrack implementation — adding ~300 lines of code and a new failure surface.

**Where AI was right**: For a camera with heavy occlusion (billing queue, spec's known edge case), RT-DETR would perform better. My mitigation: sample billing camera at every 2nd frame (instead of 3rd) to compensate for lower per-frame recall.

**VLM Usage**: I evaluated using GPT-4V for zone classification — prompt: "Given this retail store frame divided into a 4×4 grid, label which grid cells contain: skincare products, haircare, billing counter." Testing on 20 frames showed 78% accuracy on zone classification, but latency was 1.8s/frame via API (far too slow for 15fps processing). I chose rule-based zone assignment via bounding polygon containment instead — 100% deterministic, zero latency.

---

## Decision 2: Event Schema Design

### Options Considered

**Option A — Event-centric (chosen)**: One event per state change. Every ZONE_ENTER, ZONE_EXIT, DWELL is a separate row. Schema stays flat and append-only.

**Option B — Session-centric**: One row per visitor session. A `sessions` table with arrays of zone visits, updated in-place as events arrive. Enables `O(1)` metrics queries.

**Option C — Denormalised daily aggregate**: Pre-computed summary tables updated by a background job every 5 minutes.

### What AI Suggested

ChatGPT recommended Option B (session-centric) arguing it makes `/funnel` and `/metrics` queries trivially fast. Claude disagreed and recommended Option A, noting that in-place session row updates are not idempotent — if an event is replayed (the spec requires `POST /events/ingest` to be idempotent), you cannot safely re-apply it to an aggregate row without checking whether it was applied before.

### What I Chose and Why

**Option A — Event-centric**, for three reasons:

1. **Idempotency**: Append-only events with `event_id` primary key make the ingest endpoint trivially idempotent (`INSERT OR IGNORE`). Session aggregations would require complex "unapply if already applied" logic.

2. **Queryability**: The spec requires `/funnel` to use "session as the unit, not raw events." With event-centric storage, this is `COUNT(DISTINCT visitor_id) WHERE event_type = 'ENTRY'`. I pay a small query-time cost, but the data model stays honest.

3. **Debuggability**: When a metric looks wrong, I can `SELECT * FROM events WHERE visitor_id = 'VIS_abc'` and reconstruct the exact journey. With session aggregates, that context is lost.

**Schema-level AI agreement**: Claude suggested adding `metadata` as a JSON blob column — I adopted this. It allowed `queue_depth` and `sku_zone` to live in the schema without new columns, keeping the table schema stable as the event type catalogue evolves.

**Where I disagreed with AI**: Claude initially suggested making `is_staff` nullable (NULL = unknown). I kept it as a non-nullable boolean defaulting to False. Unknown staff status is rare enough (< 1% of detections) that the cost of introducing null-handling throughout all downstream queries is not worth it.

---

## Decision 3: API Architecture — Real-time SQL vs. Cached Aggregates

### Options Considered

**Option A — Real-time SQL (chosen)**: Every `GET /metrics` runs a fresh SQL aggregation query over the events table.

**Option B — Redis-cached aggregates**: Background worker computes metrics every 30s and caches in Redis. API reads from Redis.

**Option C — Materialized views**: PostgreSQL materialized views refreshed every 60s via pg_cron.

### What AI Suggested

Both Claude and ChatGPT recommended Option B (Redis cache), arguing that at 40 stores × high event rate, real-time SQL aggregation would be the bottleneck. ChatGPT generated a Redis cache invalidation strategy with TTL=30s.

### What I Chose and Why

**Option A — Real-time SQL**, specifically because the spec says "Real-time — not cached from yesterday" for `/metrics`.

At 5 stores × 50 visitors × 10 avg events per session = 2,500 events/day per store = 12,500 total. SQLite handles this in <2ms per aggregation query. The Redis dependency would add 150MB RAM, a new service in docker-compose, and complex cache invalidation logic — all for a problem that doesn't exist at this data volume.

**Where AI was right**: At 40 live stores (spec mentions 40 physical stores) with real-time event ingestion, Option A becomes untenable. At that scale, I would introduce PostgreSQL with materialized views (Option C), refreshed every 60s — a good balance between freshness and query cost. The key insight is that "real-time" in the spec means "not from yesterday's batch job," not "sub-millisecond." A 60s refresh on materialized views is operationally "real-time" for a store analytics use case.

**How I made the architecture swap easy**: The metrics computation is isolated in `app/metrics.py` with no coupling to the storage layer beyond SQLAlchemy session injection. Swapping the backend from SQLite to PostgreSQL (or adding a cache layer) requires changing only `DATABASE_URL` in docker-compose.yml and adding one `CREATE MATERIALIZED VIEW` migration. No application code changes.
