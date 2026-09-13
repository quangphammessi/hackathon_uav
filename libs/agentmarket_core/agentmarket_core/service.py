"""Shared FastAPI service bootstrap.

Every service needs the same five things: JSON logging, CORS, a Postgres
connection that waits for the database instead of crash-looping against it,
an event bus wired up and torn down cleanly, and a `/health` endpoint that
reports its *dependencies'* health rather than just answering 200 because the
process is alive. Putting that here means a service module contains only its
routes and its domain wiring.

`/health` distinguishes liveness from readiness on purpose. A service whose
database is down is alive (do not restart it, restarting will not help) but
not ready (do not route traffic to it). Collapsing the two is how you get a
crash loop during a dependency blip.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentmarket_core import db
from agentmarket_core.adapters.bus import EventBus, build_bus
from agentmarket_core.config import settings
from agentmarket_core.logging_setup import configure_logging

log = logging.getLogger("agentmarket.service")


def create_app(
    name: str,
    *,
    on_startup: Callable[[EventBus], None] | None = None,
    needs_db: bool = True,
    bus_source: str | None = None,
    health_probes: Callable[[], dict] | None = None,
) -> tuple[FastAPI, dict]:
    """Build a service app. Returns `(app, context)`; `context["bus"]` is the
    live bus once startup has run."""
    configure_logging()
    context: dict = {"bus": None, "name": name}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if needs_db:
            db.wait_for_postgres()
            db.migrate()
        bus = build_bus(source=bus_source or name)
        context["bus"] = bus
        if on_startup:
            on_startup(bus)
        bus.start()
        log.info("%s started", name, extra={"backends": {
            "bus": settings.event_bus_backend,
            "product_store": settings.product_store_backend,
            "vector_store": settings.vector_store_backend,
            "feature_store": settings.feature_store_backend,
        }})
        try:
            yield
        finally:
            bus.stop()
            log.info("%s stopped", name)

    app = FastAPI(title=f"AgentMarket OS -- {name}", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_allow_origins.split(",")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["ops"])
    def health() -> dict:
        backends: dict = {}
        if context["bus"] is not None:
            backends["bus"] = context["bus"].health()
        if needs_db:
            try:
                db.query_one("SELECT 1 AS ok")
                backends["postgres"] = {"backend": "postgres", "status": "ok"}
            except Exception as exc:  # noqa: BLE001
                backends["postgres"] = {"backend": "postgres", "status": "degraded", "detail": str(exc)}
        if health_probes:
            backends.update(health_probes())
        degraded = [k for k, v in backends.items() if v.get("status") != "ok"]
        return {
            "service": name,
            "status": "degraded" if degraded else "ok",
            "version": "1.0.0",
            "environment": settings.environment,
            "backends": backends,
            "detail": f"degraded: {', '.join(degraded)}" if degraded else None,
        }

    @app.get("/live", tags=["ops"])
    def live() -> dict:
        """Liveness only -- the process is running. Never touches a dependency,
        so a database blip cannot trigger a restart loop."""
        return {"service": name, "status": "alive"}

    return app, context
