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

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth.csrf import get_csrf_token
from .auth.deps import AuthRedirectRequired
from .auth.oauth import build_oauth
from .auth.routes import router as auth_router
from .auth.session import IdleTimeoutMiddleware, build_session_middleware
from .booking.executor import BookingExecutor
from .booking.routes import router as history_router
from .booking.vacation_routes import router as vacation_router
from .config import Settings, get_settings
from .cookie.routes import router as cookie_router
from .heartbeat.next_window import compute_next_booking
from .heartbeat.probe import HeartbeatProbe
from .notifications.banners import load_banners_for_operator
from .notifications.dispatcher import NotificationDispatcher
from .notifications.telegram_bind import TelegramBindStore
from .notifications.telegram_webhook import router as telegram_router
from .observability import configure_logging
from .persistence.cookie_store import CookieStore
from .persistence.engine import get_session
from .routes.static_pages import router as static_pages_router
from .rules.routes import router as rules_router
from .scheduler.scheduler import (
    build_scheduler,
    register_anomaly_job,
    register_dispatcher_job,
    register_healthchecks_job,
    register_heartbeat_job,
    register_rule_bootstrap_jobs,
)
from .security.cipher import Cipher
from .security.cookie import CookieValidator
from .security.keyvault import Secrets, load_secrets
from .wodbuster_client.client import WodBusterClient

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _build_cookie_stack(
    settings: Settings, secrets: Secrets
) -> tuple[Cipher | None, WodBusterClient | None, CookieValidator | None, CookieStore | None]:
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
        wodbuster_client = WodBusterClient(gym=settings.wodbuster_gym, idu=settings.wodbuster_idu)

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


def _fetch_bot_username(bot_token: str) -> str | None:
    """Call ``getMe`` on the Bot API to resolve the bot's username.

    Runs once at app startup so the ``/telegram`` page can render a
    ``t.me/<bot>?start=<token>`` deep link. Failures are logged and
    return ``None``; the UI degrades to showing the raw token the
    operator can paste manually as ``/start <token>``.
    """
    import httpx  # local import: startup-only path.
    import structlog

    log = structlog.get_logger(__name__)
    url = f"https://api.telegram.org/bot{bot_token}/getMe"
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        log.warning("telegram.getme.transport_error", error=str(exc))
        return None
    if response.status_code != 200:
        log.warning("telegram.getme.unexpected_status", status=response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError:
        log.warning("telegram.getme.invalid_json")
        return None
    username = (payload.get("result") or {}).get("username")
    if not isinstance(username, str) or not username:
        log.warning("telegram.getme.no_username", payload=payload)
        return None
    return username


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
    # Telegram binding state (US9.8):
    #   * ``telegram_bind_store``   — in-memory one-shot token bag.
    #   * ``telegram_webhook_secret`` / ``telegram_bot_token`` are
    #     surfaced on ``app.state`` so the webhook route can validate
    #     the path secret and send replies without re-fetching Key
    #     Vault on every request.
    #   * ``telegram_bot_username`` is looked up once via
    #     ``getMe`` so the ``/telegram`` page can render a
    #     ``t.me/<bot>?start=<token>`` deep link.
    if not hasattr(app.state, "telegram_bind_store"):
        app.state.telegram_bind_store = TelegramBindStore()
    app.state.telegram_webhook_secret = secrets.telegram_webhook_secret
    app.state.telegram_bot_token = secrets.telegram_bot_token
    if not getattr(app.state, "telegram_bot_username", None) and secrets.telegram_bot_token:
        app.state.telegram_bot_username = _fetch_bot_username(secrets.telegram_bot_token)
    # Scheduler: build lazily and register the heartbeat + dispatcher
    # jobs plus a per-rule booking job. Tests that inject their own
    # scheduler (or want none at all) can pre-seed ``app.state.scheduler``.
    if not hasattr(app.state, "scheduler") or app.state.scheduler is None:
        app.state.scheduler = None
        if app.state.heartbeat_probe is not None:
            scheduler = build_scheduler()
            register_heartbeat_job(scheduler, app.state.heartbeat_probe, get_session)
            dispatcher = NotificationDispatcher(
                bot_token=secrets.telegram_bot_token,
                session_factory=get_session,
            )
            app.state.notification_dispatcher = dispatcher
            register_dispatcher_job(scheduler, dispatcher)
            # Per-run anomaly detector (US2.4): every 60s scan for
            # missed booking windows and open a heartbeat_anomaly
            # alert when a run left no outcome in the database.
            register_anomaly_job(scheduler, get_session)
            # External dead-man (US2.5): every 10 minutes POST to the
            # Healthchecks.io URL from Key Vault so a silent worker
            # (crash, network partition, container stuck restarting)
            # trips an out-of-band alert. Skip silently when the URL
            # is absent — local dev doesn't need the third party.
            if secrets.healthchecks_ping_url:
                register_healthchecks_job(scheduler, secrets.healthchecks_ping_url)
            # Booking wiring: only when the cookie store + wodbuster
            # client are both live. Missing dependencies mean bookings
            # cannot fire; the scheduler still hosts heartbeat and
            # dispatcher jobs so the operator sees the cookie state.
            if app.state.cookie_store is not None and app.state.wodbuster_client is not None:
                executor = BookingExecutor(
                    client=app.state.wodbuster_client,
                    session_factory=get_session,
                    cookie_store=app.state.cookie_store,
                )
                app.state.booking_executor = executor
                app.state.booking_scheduler = scheduler
                register_rule_bootstrap_jobs(
                    scheduler,
                    executor=executor,
                    session_factory=get_session,
                )
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


def create_app(*, settings: Settings | None = None, secrets: Secrets | None = None) -> FastAPI:
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
    effective_secrets = secrets if secrets is not None else load_secrets(effective_settings)

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
    cipher, wb_client, validator, store = _build_cookie_stack(effective_settings, effective_secrets)
    app.state.cipher = cipher
    app.state.wodbuster_client = wb_client
    app.state.cookie_validator = validator
    app.state.cookie_store = store
    app.state.heartbeat_probe = _build_heartbeat_probe(effective_settings, store, validator)
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
    async def _redirect_on_anon(_request: Request, exc: AuthRedirectRequired) -> Response:
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
    app.include_router(rules_router)
    app.include_router(history_router)
    app.include_router(vacation_router)
    app.include_router(telegram_router)
    app.include_router(static_pages_router)
    app.add_api_route("/health", health, methods=["GET"])
    # Static assets (brand CSS, later JS / images). Mounted after
    # routers so a stray path collision would surface as an app-side
    # 404 rather than the static handler swallowing it silently.
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/")
    def index(request: Request) -> Response:
        """Dashboard for signed-in operators; landing hero otherwise.

        Splitting the two flows here rather than gating with
        :func:`require_session` gives us a real landing page for the
        very first sign-in (better than the immediate redirect the
        gate would produce).
        """
        templates: Jinja2Templates = request.app.state.templates
        operator_id = request.session.get("operator_id")
        if isinstance(operator_id, int):
            # ``display_name`` was seated on the session by the OAuth
            # callback (auth/routes.py) alongside ``operator_id``. Fall
            # back to an empty string when it is missing rather than
            # a placeholder — the template picks its own copy.
            display_name = request.session.get("display_name") or ""
            from datetime import UTC, datetime

            now = datetime.now(tz=UTC)
            with get_session() as session:
                banners = load_banners_for_operator(session, operator_id)
                next_booking = compute_next_booking(session, operator_id, now)
            next_window_iso = (
                next_booking.window_open.isoformat() if next_booking is not None else None
            )
            target_slot_iso = (
                next_booking.target_slot.isoformat() if next_booking is not None else None
            )
            response = templates.TemplateResponse(
                request=request,
                name="dashboard.html",
                context={
                    "operator_id": operator_id,
                    "display_name": display_name,
                    "banners": banners,
                    "next_window_iso": next_window_iso,
                    "target_slot_iso": target_slot_iso,
                    "csrf_token": get_csrf_token(request) or "",
                },
            )
            # Prevent the browser back-button restoring a stale
            # snapshot after a mutation (e.g. rule delete). The
            # countdown script also listens for ``pageshow.persisted``
            # as a belt-and-braces fallback for browsers that ignore
            # ``no-store`` for bfcache.
            response.headers["Cache-Control"] = "no-store"
            return response
        return templates.TemplateResponse(
            request=request,
            name="landing.html",
            context={},
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
