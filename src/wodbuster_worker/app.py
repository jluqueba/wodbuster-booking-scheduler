"""FastAPI application entry point.

Owns the top-level FastAPI instance plus the startup lifespan that
resolves runtime configuration and secrets exactly once per process,
and the middleware stack for session + auth (US-009).

The lifespan hook stores the resolved ``Settings``, ``Secrets``,
Authlib ``OAuth`` registry, and Jinja2 template loader on
``app.state`` so downstream handlers can read them via
``request.app.state.<name>`` without re-hitting Key Vault or
re-parsing templates on every request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .auth.csrf import get_csrf_token
from .auth.deps import AuthRedirectRequired, require_session
from .auth.oauth import build_oauth
from .auth.routes import router as auth_router
from .auth.session import IdleTimeoutMiddleware, build_session_middleware
from .config import Settings, get_settings
from .observability import configure_logging
from .security.keyvault import Secrets, load_secrets

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Resolve settings, secrets, OAuth registry and templates once.

    The lifespan API is the modern replacement for the deprecated
    ``@app.on_event("startup")`` hook. Starlette runs this context
    manager on startup, yields to the app, and re-enters on shutdown.
    """
    settings: Settings = getattr(app.state, "settings", None) or get_settings()
    secrets: Secrets = getattr(app.state, "secrets", None) or load_secrets(settings)
    configure_logging(settings.log_level)
    app.state.settings = settings
    app.state.secrets = secrets
    if not hasattr(app.state, "oauth") or app.state.oauth is None:
        app.state.oauth = build_oauth(settings, secrets)
    if not hasattr(app.state, "templates") or app.state.templates is None:
        app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    try:
        yield
    finally:
        # No teardown resources today. Placeholder for engine.dispose(),
        # scheduler shutdown, and httpx client close in later phases.
        pass


def create_app(
    *, settings: Settings | None = None, secrets: Secrets | None = None
) -> FastAPI:
    """Build a fresh FastAPI instance.

    The default entry point (module-level ``app``) uses this with no
    arguments. Tests inject fabricated ``settings`` / ``secrets`` so
    the middleware stack (which reads the session key at construction
    time) can be exercised without touching Key Vault or ``.env``.

    Ordering of middleware matters: :class:`SessionMiddleware` must
    populate ``scope["session"]`` before :class:`IdleTimeoutMiddleware`
    inspects it. Starlette runs middleware in reverse-registration
    order per request; adding the idle middleware *after* the session
    middleware means the session runs first on the inbound leg.
    """
    effective_settings = settings if settings is not None else get_settings()
    effective_secrets = (
        secrets if secrets is not None else load_secrets(effective_settings)
    )

    session_middleware = build_session_middleware(
        effective_settings, effective_secrets
    )

    app = FastAPI(
        title="WodBuster Booking Worker",
        version="0.1.0",
        lifespan=lifespan,
        middleware=[session_middleware],
    )
    # Seed state so lifespan sees pre-injected values from tests.
    app.state.settings = effective_settings
    app.state.secrets = effective_secrets
    app.state.oauth = build_oauth(effective_settings, effective_secrets)
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    app.add_middleware(
        IdleTimeoutMiddleware,
        idle_minutes=effective_settings.session_idle_minutes,
        absolute_hours=effective_settings.session_absolute_hours,
    )

    _register_exception_handlers(app)
    _register_routes(app)
    return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Wire the ``AuthRedirectRequired`` handler.

    The handler emits a bare 302 with an empty body so no operator
    data can leak through (CC-011).
    """

    @app.exception_handler(AuthRedirectRequired)
    async def _redirect_on_anon(
        _request: Request, exc: AuthRedirectRequired
    ) -> Response:
        return RedirectResponse(url=exc.location, status_code=302)


def health() -> dict[str, str]:
    """Liveness probe.

    Per ADR-0006 this endpoint is the Healthchecks.io dead-man target
    and the Container App probe. Phase 1 returns a static payload;
    later phases will additionally verify that APScheduler is alive
    and the Postgres connection is usable before answering 200. Kept
    at module scope so the F1 smoke test can import it directly
    without spinning up the whole FastAPI app.
    """
    return {"status": "ok"}


def _register_routes(app: FastAPI) -> None:
    """Register the built-in routes (health, dashboard) and mount ``/auth``."""
    app.include_router(auth_router)
    app.add_api_route("/health", health, methods=["GET"])

    @app.get("/")
    def index(
        request: Request, operator_id: int = Depends(require_session)
    ) -> Response:
        """Minimal authenticated dashboard.

        Rendered by ``templates/index.html``. The template exposes the
        session CSRF token via a ``meta`` tag and an ``hx-headers``
        attribute on ``<body>`` so HTMX-driven forms carry the token
        automatically.
        """
        templates: Jinja2Templates = request.app.state.templates
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "operator_id": operator_id,
                "csrf_token": get_csrf_token(request) or "",
            },
        )


# Lazy module-level ``app`` binding (PEP 562). ``create_app`` reads the
# session encryption key at construction time, and importing this
# module for a stray helper (e.g. the F1 smoke test importing
# :func:`health`) must not require ``SESSION_ENCRYPTION_SECRET`` to
# be set. The FastAPI ASGI entry point still resolves ``app`` on
# ``uvicorn wodbuster_worker.app:app``, which triggers the lazy path.
_APP: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    global _APP
    if name == "app":
        if _APP is None:
            _APP = create_app()
        return _APP
    raise AttributeError(name)

