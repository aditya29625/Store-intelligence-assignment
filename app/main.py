"""
main.py — FastAPI application entrypoint.

Includes:
- Structured request logging middleware (trace_id, latency, store_id)
- Graceful DB error handling (503 instead of stack traces)
- WebSocket endpoint /ws
- All route registration
- CORS configuration for dashboard
"""

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.database import init_db
from app.ingestion import router as ingest_router
from app.metrics import router as metrics_router
from app.funnel import router as funnel_router
from app.heatmap import router as heatmap_router
from app.anomalies import router as anomalies_router
from app.health import router as health_router
from app.ws import manager

# ─── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


# ─── Database initialisation ──────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Initialising database ...")
    try:
        init_db()
        logger.info("✅ Database ready.")
    except Exception as e:
        logger.error(f"❌ DB init failed: {e}")
    yield
    logger.info("👋 Shutting down.")


# ─── App factory ─────────────────────────────────────────────────────────────
app = FastAPI(
    title="Apex Retail — Store Intelligence API",
    description="Real-time retail analytics from CCTV events",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS for dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Request logging middleware ───────────────────────────────────────────────
@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id", str(uuid.uuid4())[:8])
    request.state.trace_id = trace_id
    t0 = time.time()

    # Attempt to extract store_id from path
    path = request.url.path
    store_id = "?"
    parts = path.split("/")
    if "stores" in parts:
        idx = parts.index("stores")
        if idx + 1 < len(parts):
            store_id = parts[idx + 1]

    try:
        response = await call_next(request)
    except Exception as exc:
        logger.error(
            f"trace_id={trace_id} store_id={store_id} path={path} "
            f"error={type(exc).__name__}: {exc}"
        )
        return JSONResponse(
            status_code=503,
            content={"error": "Internal service error", "code": "INTERNAL_ERROR", "trace_id": trace_id},
        )

    latency_ms = round((time.time() - t0) * 1000, 1)
    logger.info(
        f"trace_id={trace_id} store_id={store_id} "
        f"method={request.method} path={path} "
        f"status_code={response.status_code} latency_ms={latency_ms}"
    )
    response.headers["X-Trace-Id"] = trace_id
    return response


# ─── Routes ──────────────────────────────────────────────────────────────────
app.include_router(ingest_router)
app.include_router(metrics_router)
app.include_router(funnel_router)
app.include_router(heatmap_router)
app.include_router(anomalies_router)
app.include_router(health_router)


@app.get("/")
async def root():
    return {
        "service": "store-intelligence-api",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
    }


# ─── WebSocket ────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive; client sends pings
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.warning(f"WS error: {e}")
        manager.disconnect(websocket)


# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        reload=False,
        log_level="info",
    )
