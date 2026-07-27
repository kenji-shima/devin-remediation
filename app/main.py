import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.db import Base, engine
from app.metrics import router as metrics_router
from app.poller import poll_loop
from app.webhook import router as webhook_router

# Without this, every app.* logger.info/logger.exception call in the codebase
# is silently dropped (root logger's default level is WARNING, no handler
# attached) -- never reaching `docker logs`. Uvicorn's own loggers configure
# themselves separately and are unaffected by this.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

# httpx logs an INFO line per outbound request by default -- with the
# dashboard polling /metrics/summary every 5s (which itself calls the Devin
# API twice), that's as noisy as the access log line it was meant to declutter.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_QUIET_ACCESS_LOG_PATHS = ("/metrics/summary", "/metrics/timeseries", "/metrics/sessions")


class _SuppressDashboardPolling(logging.Filter):
    """The dashboard polls the metrics endpoints every 5s -- left unfiltered,
    that drowns out the one-time signals (webhook received, escalations,
    errors) in `docker logs` within a few minutes. Scoped to uvicorn's own
    access logger only, so nothing else is affected.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(path in message for path in _QUIET_ACCESS_LOG_PATHS)


logging.getLogger("uvicorn.access").addFilter(_SuppressDashboardPolling())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Deferred to startup (not import time) so importing this module for tests
    # never touches the real production engine -- tests build their own
    # in-memory schema and override get_db instead.
    Base.metadata.create_all(bind=engine)

    # The lifecycle poller is a genuinely long-running loop -- asyncio.create_task,
    # NOT BackgroundTasks (which is per-request and would die/duplicate across
    # requests). Stopped via an Event rather than an abrupt task.cancel() so the
    # in-flight tick gets to finish cleanly.
    stop_event = asyncio.Event()
    poller_task = asyncio.create_task(poll_loop(stop_event))
    try:
        yield
    finally:
        stop_event.set()
        await poller_task


app = FastAPI(title="Devin Remediation Orchestrator", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"

app.include_router(webhook_router)
app.include_router(metrics_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")
