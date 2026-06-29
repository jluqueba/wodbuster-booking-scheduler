"""FastAPI application entry point.

This module wires only the minimal surface needed for F1: a single FastAPI
instance exposing `GET /health`. Routes, scheduler bootstrap, persistence,
WodBuster client, notifications, and security wiring land in later phases
under their respective subpackages.
"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="WodBuster Booking Worker", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe.

    Per ADR-0006 this endpoint is the Healthchecks.io dead-man target and the
    Container App probe. Phase 1 returns a static payload; later phases will
    additionally verify that APScheduler is alive and the SQLite file is
    writable before answering 200.
    """
    return {"status": "ok"}
