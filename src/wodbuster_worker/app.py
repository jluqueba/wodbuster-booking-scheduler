"""FastAPI application entry point.

Owns the top-level FastAPI instance plus the startup lifespan that
resolves runtime configuration and secrets exactly once per process.
Routes, scheduler bootstrap, persistence wiring, WodBuster client, and
notifications land in later phases under their respective subpackages.

The lifespan hook stores the resolved ``Settings`` and ``Secrets`` on
``app.state`` so downstream handlers and background jobs can read them
via ``request.app.state.settings`` and ``.secrets`` without re-hitting
Key Vault on every request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import get_settings
from .security.keyvault import load_secrets


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Resolve settings and secrets once, attach to ``app.state``.

    The lifespan API is the modern replacement for the deprecated
    ``@app.on_event("startup")`` hook. Starlette runs this context
    manager on startup, yields to the app, and re-enters on shutdown.
    """
    settings = get_settings()
    app.state.settings = settings
    app.state.secrets = load_secrets(settings)
    try:
        yield
    finally:
        # No teardown resources today. Placeholder for engine.dispose(),
        # scheduler shutdown, and httpx client close in later phases.
        pass


app = FastAPI(
    title="WodBuster Booking Worker",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe.

    Per ADR-0006 this endpoint is the Healthchecks.io dead-man target and the
    Container App probe. Phase 1 returns a static payload; later phases will
    additionally verify that APScheduler is alive and the SQLite file is
    writable before answering 200.
    """
    return {"status": "ok"}
