"""OAuth login / callback / logout routes (US9.3, US9.4).

Layout:

- ``GET  /auth/{provider}/login``: kick off the OAuth dance. Validate
  ``provider``, generate a random ``state``, store it in the session,
  and hand off to Authlib's ``authorize_redirect`` which builds the
  provider-specific authorization URL.
- ``GET  /auth/{provider}/callback``: complete the OAuth dance,
  extract the normalized identity, check the ``federated_identity``
  allow-list, and either seat a session or render a denial page.
- ``POST /auth/logout``: clear the session and redirect back to the
  default login flow. CSRF-protected.

The router is registered under ``prefix="/auth"`` in ``app.py`` so the
route names on this file remain ``/{provider}/...``.

All redirects use ``RedirectResponse(status_code=302)``. Bodies stay
empty on the denial and redirect paths per CC-011 / FR-030 (no
operator data leaked).
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

import structlog
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..gyms.discovery import GymSelectorError
from ..gyms.service import add_discovered_gym_accounts
from ..i18n import lang_prefix, t_lang
from ..notifications.telegram import TelegramError, send_message
from ..persistence.cookie_store import CookieDecryptError
from ..persistence.engine import get_session as db_session
from ..persistence.models import FederatedIdentity, GymAccount, OperatorProfile
from ..persistence.users import ban_is_active
from ..wodbuster_client.client import (
    WodBusterAuthError,
    WodBusterProtocolError,
    WodBusterTransportError,
)
from .csrf import CSRF_COOKIE_NAME, issue_csrf_token, verify_csrf
from .oauth import SUPPORTED_PROVIDERS, extract_email, extract_identity
from .session import touch_session

router = APIRouter(prefix="/auth", tags=["auth"])

log = structlog.get_logger(__name__)


def _templates(request: Request) -> Jinja2Templates:
    """Fetch the process-wide :class:`Jinja2Templates` from app state.

    Wired in :mod:`app`. Kept as a helper so tests that instantiate
    a minimal app can inject their own template loader.
    """
    templates = getattr(request.app.state, "templates", None)
    if templates is None:  # pragma: no cover - misconfiguration
        raise RuntimeError("app.state.templates is not configured; wire it in lifespan().")
    assert isinstance(templates, Jinja2Templates)
    return templates


def _oauth(request: Request) -> OAuth:
    """Fetch the process-wide :class:`OAuth` registry from app state."""
    oauth = getattr(request.app.state, "oauth", None)
    if oauth is None:  # pragma: no cover - misconfiguration
        raise RuntimeError("app.state.oauth is not configured; wire it in lifespan().")
    return oauth


def _reject_unknown_provider(provider: str) -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=404, detail="unknown provider")


def _refresh_accessible_gyms(request: Request, operator_id: int) -> None:
    """Discover the gyms this identity can access right after sign-in.

    A single ``.WBAuth`` authenticates every accessible gym, so an existing
    stored cookie is enough to refresh the list and surface newly accessible
    gyms in the switcher. Best-effort: a missing cookie, a selector failure,
    or a gym error must never block the login, so all such failures are
    logged and swallowed.
    """
    cookie_store = getattr(request.app.state, "cookie_store", None)
    selector = getattr(request.app.state, "gym_selector", None)
    factory = getattr(request.app.state, "gym_discovery_factory", None)
    if cookie_store is None or selector is None or factory is None:
        return
    try:
        with db_session() as session:
            account_ids = session.scalars(
                select(GymAccount.id)
                .where(GymAccount.user_id == operator_id)
                .order_by(GymAccount.id)
            ).all()
            cookie_value: str | None = None
            for account_id in account_ids:
                try:
                    cookie_value = cookie_store.load(session, int(account_id))
                except CookieDecryptError:
                    continue
                if cookie_value:
                    break
            if not cookie_value:
                return
            add_discovered_gym_accounts(
                session,
                user_id=operator_id,
                gyms=selector(cookie_value),
                cookie_value=cookie_value,
                cookie_store=cookie_store,
                client_factory=factory,
            )
            session.commit()
    except (
        GymSelectorError,
        WodBusterAuthError,
        WodBusterProtocolError,
        WodBusterTransportError,
        ValueError,
        SQLAlchemyError,
    ):
        log.warning("auth.gym_refresh_failed", operator_id=operator_id)


@router.get("/{provider}/login", name="auth_login")
async def login(provider: str, request: Request) -> Response:
    """Kick off the OAuth flow for ``provider``.

    Generates a fresh ``state`` and stores it under
    ``oauth_state_{provider}`` so the callback can verify. Authlib
    also stores its own state internally, but we keep our own copy so
    the state remains tied to *this* session across the whole flow.
    """
    _reject_unknown_provider(provider)

    state = secrets.token_urlsafe(16)
    request.session[f"oauth_state_{provider}"] = state
    # Remember which language branch the operator started on so we
    # can drop them back on the matching root after the callback.
    request.session["oauth_lang_prefix"] = lang_prefix()

    client = _oauth(request).create_client(provider)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail=f"provider {provider!r} is not configured",
        )

    redirect_uri = str(request.url_for("auth_callback", provider=provider))
    # Authlib's ``authorize_redirect`` returns a Starlette response.
    response = await client.authorize_redirect(request, redirect_uri, state=state)
    assert isinstance(response, Response)
    return response


@router.get("/{provider}/callback", name="auth_callback")
async def callback(provider: str, request: Request) -> Response:
    """Complete the OAuth dance and either seat a session or deny.

    Denial rendering uses ``templates/auth/denied.html`` with status
    403. The body is fixed and never mentions the presented identity;
    this satisfies FR-030 and AS3.
    """
    _reject_unknown_provider(provider)

    client = _oauth(request).create_client(provider)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail=f"provider {provider!r} is not configured",
        )

    try:
        token = await client.authorize_access_token(request)
    except OAuthError:
        # Authlib raises on state mismatch, denied consent, etc. We
        # deliberately do not surface the details; the operator flow
        # is fully controlled, so a failure here is almost always a
        # tampering attempt or a browser back-button retry.
        return _render_denial(request)

    user_info = await _fetch_user_info(client, provider, token)
    if not user_info:
        return _render_denial(request)

    try:
        _, subject_id, display_name = extract_identity(provider, user_info)
    except ValueError:
        return _render_denial(request)

    email = extract_email(user_info)
    lookup = _lookup_operator(provider, subject_id)
    if lookup is None:
        # A new identity: create a pending signup and show the pending page.
        # The admin approves it before the user gains access (ADR-0010).
        new_id = _create_pending_signup(provider, subject_id, display_name, email)
        log.info("auth.signup.pending", provider=provider, operator_id=new_id)
        _notify_admins_new_request(request, display_name)
        return _render_pending(request)

    operator_id, status = lookup
    if status == "rejected":
        # A rejected user can re-request simply by signing in again: re-open
        # the request as pending and re-notify the admin (ADR-0010). This makes
        # a mistaken rejection recoverable instead of locking the user out.
        _reopen_rejected_request(operator_id)
        _store_login_email(operator_id, email)
        log.info("auth.signup.reopened", provider=provider, operator_id=operator_id)
        _notify_admins_new_request(request, display_name)
        return _render_pending(request)
    if status == "pending":
        _store_login_email(operator_id, email)
        return _render_pending(request)

    # status == 'active': block a banned user, else capture email and seat.
    if _operator_is_banned(operator_id):
        log.info("auth.login.banned", provider=provider, operator_id=operator_id)
        return _render_banned(request)
    _store_login_email(operator_id, email)

    # Success: rotate the session (mitigate session-fixation), stamp
    # timestamps, and set the CSRF token.
    prefix = request.session.get("oauth_lang_prefix", "") or ""
    request.session.clear()
    request.session["operator_id"] = operator_id
    # Seed nav identity from the STORED profile (the editable source of
    # truth) so the avatar/greeting reflect the user's own edits, not the
    # raw provider fields. Fall back to the provider name on first login.
    stored_name, stored_short, stored_lang = _load_profile_fields(operator_id)
    request.session["display_name"] = stored_name or display_name
    request.session["short_name"] = stored_short
    # Cache the language so LanguageMiddleware resolves it without a
    # per-request DB round trip (ADR-0008).
    request.session["lang"] = stored_lang
    # Cache the admin flag so admin-only UI (nav link, banner) renders
    # without a per-request query (ADR-0010).
    request.session["is_admin"] = _operator_is_admin(operator_id)
    touch_session(request.session)
    csrf_token = issue_csrf_token(request)

    # Refresh the gyms this identity can access so new ones show up in the
    # switcher. Best-effort: never block or fail sign-in.
    _refresh_accessible_gyms(request, operator_id)

    response = RedirectResponse(url=f"{prefix}/", status_code=302)
    # Non-HttpOnly CSRF cookie so HTMX JS can read it and echo the
    # X-CSRF-Token header. The value is bound to the session by the
    # double-submit check; disclosure to first-party JS is safe.
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        secure=True,
        httponly=False,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/suspended", name="auth_suspended")
def suspended(request: Request) -> Response:
    """Public page a banned session is bounced to (ADR-0010)."""
    return _render_banned(request)


@router.post("/logout", name="auth_logout", dependencies=[Depends(verify_csrf)])
async def logout(request: Request) -> Response:
    """Clear the session and land the operator back on the marketing page.

    CSRF-protected. Also deletes the ``wodbuster_csrf`` cookie so a
    subsequent request cannot present a stale double-submit value
    against a fresh session.

    The redirect target is ``/`` (the anonymous landing page) rather
    than ``/auth/{provider}/login``. Going through the OAuth flow
    would silently re-authenticate the browser (Microsoft still has
    the operator's SSO cookies), leaving the user apparently "still
    logged in" from their perspective. Landing on ``/`` shows the
    marketing hero with a "Sign in" button and requires an intentional
    click to re-enter the app.
    """
    request.session.clear()
    response = RedirectResponse(url=f"{lang_prefix()}/", status_code=302)
    response.delete_cookie(key=CSRF_COOKIE_NAME, path="/")
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fetch_user_info(
    client: Any, provider: str, token: dict[str, Any]
) -> dict[str, Any] | None:
    """Return the provider's user-info payload as a plain dict.

    Authlib exposes two shapes: for OIDC providers the token already
    contains a decoded ``userinfo`` dict; for OAuth-only providers
    (GitHub) we call ``client.get('user', token=token)``.
    """
    userinfo = token.get("userinfo") if isinstance(token, dict) else None
    if isinstance(userinfo, dict):
        return userinfo

    if provider == "github":
        resp = await client.get("user", token=token)
        data = resp.json()
        return data if isinstance(data, dict) else None

    # Fallback: hit the userinfo endpoint if Authlib did not expand it.
    try:
        info = await client.userinfo(token=token)
    except Exception:  # pragma: no cover - defensive path
        return None
    return dict(info) if info is not None else None


def _operator_is_admin(operator_id: int) -> bool:
    """Return whether the operator has the admin flag (ADR-0010)."""
    with db_session() as session:
        flag = session.scalars(
            select(OperatorProfile.is_admin).where(OperatorProfile.id == operator_id)
        ).first()
    return bool(flag)


def _store_login_email(operator_id: int, email: str | None) -> None:
    """Persist the OAuth email on the profile when present and changed."""
    if not email:
        return
    with db_session() as session:
        profile = session.get(OperatorProfile, operator_id)
        if profile is not None and profile.email != email:
            profile.email = email
            session.commit()


def _load_profile_fields(operator_id: int) -> tuple[str, str, str]:
    """Return ``(display_name, short_name, communication_language)``.

    Read once at login to seed the session-cached nav identity and the
    language used by :class:`LanguageMiddleware`. Empty strings stand in
    for null name columns; the language falls back to the ``en`` default.
    """
    with db_session() as session:
        profile = session.get(OperatorProfile, operator_id)
        if profile is None:  # pragma: no cover - FK guarantees a row exists
            return ("", "", "en")
        return (
            profile.display_name,
            profile.short_name or "",
            profile.communication_language,
        )


def _lookup_operator(provider: str, subject_id: str) -> tuple[int, str] | None:
    """Return ``(operator_id, status)`` bound to ``(provider, subject_id)``.

    ``None`` means the identity is unknown, which the caller treats as a
    new signup (a pending profile) rather than a denial (ADR-0010).
    """
    with db_session() as session:
        stmt = (
            select(FederatedIdentity.operator_id, OperatorProfile.status)
            .join(OperatorProfile, OperatorProfile.id == FederatedIdentity.operator_id)
            .where(
                FederatedIdentity.provider == provider,
                FederatedIdentity.subject_id == subject_id,
            )
        )
        row = session.execute(stmt).first()
    return (int(row[0]), str(row[1])) if row is not None else None


def _create_pending_signup(
    provider: str, subject_id: str, display_name: str, email: str | None
) -> int:
    """Create a pending profile + federated identity for a new signup.

    Returns the new ``operator_id``. Only reached when the identity is
    unknown, so the ``(provider, subject_id)`` unique key is not violated.
    """
    with db_session() as session:
        profile = OperatorProfile(
            display_name=display_name or subject_id,
            email=email,
            status="pending",
        )
        session.add(profile)
        session.flush()
        session.add(
            FederatedIdentity(
                operator_id=int(profile.id),
                provider=provider,
                subject_id=subject_id,
                display_name=display_name or None,
            )
        )
        session.commit()
        return int(profile.id)


def _notify_admins_new_request(request: Request, display_name: str) -> None:
    """Best-effort Telegram ping to bound admins about a new signup (ADR-0010).

    Sends to every admin who has bound Telegram, rendered in that admin's
    language. Any send failure is logged and swallowed so signup never fails.
    """
    bot_token = getattr(request.app.state, "telegram_bot_token", None)
    if not bot_token:
        return
    with db_session() as session:
        admins = session.execute(
            select(
                OperatorProfile.telegram_chat_id,
                OperatorProfile.communication_language,
            ).where(
                OperatorProfile.is_admin.is_(True),
                OperatorProfile.telegram_chat_id.is_not(None),
            )
        ).all()
    for chat_id, lang in admins:
        message = t_lang(lang, "admin.notify.new_request", name=display_name)
        try:
            send_message(bot_token=bot_token, chat_id=str(chat_id), text=message)
        except TelegramError:
            log.warning("admin.notify.telegram_failed")


def _reopen_rejected_request(operator_id: int) -> None:
    """Flip a rejected profile back to pending so the user can re-request."""
    with db_session() as session:
        profile = session.get(OperatorProfile, operator_id)
        if profile is not None and profile.status == "rejected":
            profile.status = "pending"
            session.commit()


def _operator_is_banned(operator_id: int) -> bool:
    """Return whether the operator currently has an active ban (ADR-0010)."""
    with db_session() as session:
        row = session.execute(
            select(OperatorProfile.banned_until).where(OperatorProfile.id == operator_id)
        ).first()
    return row is not None and ban_is_active(row[0], datetime.now(tz=UTC))


def _render_pending(request: Request) -> Response:
    """Render the "request received" page with status 200.

    Shown to a signed-in-with-the-provider identity that is not yet
    approved. No session is seated and the body carries no operator data.
    """
    templates = _templates(request)
    return templates.TemplateResponse(
        request=request,
        name="auth/pending.html",
        context={},
        status_code=200,
    )


def _render_denial(request: Request) -> Response:
    """Render the generic denial template with status 403.

    Body contains no operator-linked strings; the template ships a
    static message. See ``templates/auth/denied.html``.
    """
    templates = _templates(request)
    return templates.TemplateResponse(
        request=request,
        name="auth/denied.html",
        context={},
        status_code=403,
    )


def _render_banned(request: Request) -> Response:
    """Render the "access suspended" page shown to a banned user (ADR-0010)."""
    templates = _templates(request)
    return templates.TemplateResponse(
        request=request,
        name="auth/banned.html",
        context={},
        status_code=200,
    )
