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
from datetime import timedelta
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
from .cookie.routes import router as cookie_router
from .heartbeat.probe import HeartbeatProbe
from .observability import configure_logging
from .persistence.cookie_store import CookieStore
from .persistence.engine import get_session
from .scheduler.scheduler import build_scheduler, register_heartbeat_job
from .security.cipher import Cipher
from .security.cookie import CookieValidator
from .security.keyvault import Secrets, load_secrets
from .wodbuster_client.client import WodBusterClient

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _build_cookie_stack(
    settings: Settings, secrets: Secrets
) -> tuple[
    Cipher | None, WodBusterClient | None, CookieValidator | None, CookieStore | None
]:
    """Build the four cookie-flow singletons if their inputs are present.

    Each dependency is optional so partial local setups still boot:

    - :class:`Cipher` needs ``cookie_encryption_key``. Missing = no
      encrypted persistence; the ``/cookie`` route will 503 loudly.
    - :class:`WodBusterClient` needs ``wodbuster_gym`` and
      ``wodbuster_idu``. Missing = the validator has nothing to probe;
      route also 503s.
    - :class:`CookieValidator` and :class:`CookieStore` are wired only
      when both of their inputs above are present.

    The 503 vs 500 choice lives at the route layer. Here we simply
    return ``None`` for the missing pieces so the wiring reflects
    reality and tests can construct partial apps deliberately.
    """
    cipher: Cipher | None = None
    if secrets.cookie_encryption_key:
        cipher = Cipher.from_base64(secrets.cookie_encryption_key)

    wodbuster_client: WodBusterClient | None = None
    if settings.wodbuster_gym and settings.wodbuster_idu:
        wodbuster_client = WodBusterClient(
            gym=settings.wodbuster_gym, idu=settings.wodbuster_idu
        )

    validator = CookieValidator(wodbuster_client) if wodbuster_client else None
    store = CookieStore(cipher) if cipher else None
    return cipher, wodbuster_client, validator, store


def _build_heartbeat_probe(
    settings: Settings,
    store: CookieStore | None,
    validator: CookieValidator | None,
) -> HeartbeatProbe | None:
    """Build the shared :class:`HeartbeatProbe` if both inputs are wired.

    Returns ``None`` when either dependency is missing so the scheduler
    can decide not to register a job in that state. The probe is a
    lightweight object; a fresh instance per app is fine.
    """
    if store is None or validator is None:
        return None
    ceiling = timedelta(days=settings.cookie_projected_ttl_ceiling_days)
    return HeartbeatProbe(store, validator, ceiling=ceiling)


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
    # Cookie stack: idempotent — respect pre-seeded state so tests can
    # inject fakes.
    if not hasattr(app.state, "cipher") or app.state.cipher is None:
        cipher, wb_client, validator, store = _build_cookie_stack(settings, secrets)
        app.state.cipher = cipher
        app.state.wodbuster_client = wb_client
        app.state.cookie_validator = validator
        app.state.cookie_store = store
    if not hasattr(app.state, "heartbeat_probe") or app.state.heartbeat_probe is None:
        app.state.heartbeat_probe = _build_heartbeat_probe(
            settings, app.state.cookie_store, app.state.cookie_validator
        )
    # Scheduler: build lazily and register the heartbeat job only when
    # its probe dependency is wired. Tests that inject their own
    # scheduler (or want none at all) can pre-seed ``app.state.scheduler``.
    if not hasattr(app.state, "scheduler") or app.state.scheduler is None:
        app.state.scheduler = None
        if app.state.heartbeat_probe is not None:
            scheduler = build_scheduler()
            register_heartbeat_job(scheduler, app.state.heartbeat_probe, get_session)
            scheduler.start()
            app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            # ``wait=False`` so a lingering tick does not block the
            # ASGI shutdown sequence past its deadline.
            scheduler.shutdown(wait=False)
            app.state.scheduler = None


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

    session_middleware = build_session_middleware(effective_settings, effective_secrets)

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
    cipher, wb_client, validator, store = _build_cookie_stack(
        effective_settings, effective_secrets
    )
    app.state.cipher = cipher
    app.state.wodbuster_client = wb_client
    app.state.cookie_validator = validator
    app.state.cookie_store = store
    app.state.heartbeat_probe = _build_heartbeat_probe(
        effective_settings, store, validator
    )
    # The scheduler itself is built lazily inside the lifespan hook
    # so tests that construct an app without entering its lifespan
    # never spin up a real BackgroundScheduler thread.
    app.state.scheduler = None

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
    app.include_router(cookie_router)
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
