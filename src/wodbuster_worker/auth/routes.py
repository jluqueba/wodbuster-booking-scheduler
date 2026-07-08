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
from typing import Any

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..persistence.engine import get_session as db_session
from ..persistence.models import FederatedIdentity
from .csrf import CSRF_COOKIE_NAME, issue_csrf_token, verify_csrf
from .oauth import SUPPORTED_PROVIDERS, extract_identity
from .session import touch_session

router = APIRouter(prefix="/auth", tags=["auth"])


def _templates(request: Request) -> Jinja2Templates:
    """Fetch the process-wide :class:`Jinja2Templates` from app state.

    Wired in :mod:`app`. Kept as a helper so tests that instantiate
    a minimal app can inject their own template loader.
    """
    templates = getattr(request.app.state, "templates", None)
    if templates is None:  # pragma: no cover - misconfiguration
        raise RuntimeError(
            "app.state.templates is not configured; wire it in lifespan()."
        )
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

    operator_id = _lookup_operator(provider, subject_id)
    if operator_id is None:
        # Deny with no state change. The provider is on the allow-list
        # of *providers*, but this specific identity is not on the
        # allow-list of *operators*. Do NOT create an operator_profile
        # here; that is the bootstrap command's job.
        return _render_denial(request)

    # Success: rotate the session (mitigate session-fixation), stamp
    # timestamps, and set the CSRF token.
    request.session.clear()
    request.session["operator_id"] = operator_id
    request.session["display_name"] = display_name
    touch_session(request.session)
    csrf_token = issue_csrf_token(request)

    response = RedirectResponse(url="/", status_code=302)
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
    response = RedirectResponse(url="/", status_code=302)
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


def _lookup_operator(provider: str, subject_id: str) -> int | None:
    """Return the ``operator_id`` bound to ``(provider, subject_id)``.

    Returns ``None`` when the tuple is absent from
    ``federated_identity``, which the caller treats as a hard deny
    without side effects.
    """
    with db_session() as session:
        stmt = select(FederatedIdentity.operator_id).where(
            FederatedIdentity.provider == provider,
            FederatedIdentity.subject_id == subject_id,
        )
        result = session.execute(stmt).scalar_one_or_none()
    return int(result) if result is not None else None


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
