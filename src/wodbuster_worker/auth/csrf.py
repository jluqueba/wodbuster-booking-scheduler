"""CSRF protection compatible with HTMX (US9.6).

Uses the double-submit cookie pattern:

- On successful OAuth callback, :func:`issue_csrf_token` generates a
  16-byte urlsafe token, stores it in the session as ``csrf_token``,
  and instructs the caller to append a non-``HttpOnly`` cookie
  ``wodbuster_csrf`` carrying the same value.
- HTMX reads the cookie via ``hx-headers`` (documented in
  ``templates/index.html``) and echoes it in the ``X-CSRF-Token``
  request header on every ``POST``.
- :func:`verify_csrf` is the FastAPI dependency wired into every
  state-mutating route. It reads ``X-CSRF-Token`` (or the ``_csrf``
  form field, for a hypothetical non-HTMX fallback), compares to the
  session token in constant time, and raises 403 on mismatch.

The pattern relies on the browser Same-Origin Policy: an attacker on
another origin cannot read ``wodbuster_csrf`` and cannot forge the
header. Session-cookie theft is out of scope (that is what
``HttpOnly`` on the *session* cookie protects against; the CSRF
cookie is intentionally readable by first-party JS).
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request

# Cookie name for the double-submit value. Intentionally distinct from
# the session cookie so a browser Set-Cookie flush on logout can drop
# just the one without breaking future logins.
CSRF_COOKIE_NAME = "wodbuster_csrf"

# Session key holding the canonical CSRF token. Compared against the
# ``X-CSRF-Token`` header or ``_csrf`` form field on every mutating
# request. Kept private to this module; callers use the helpers below.
_SESSION_KEY = "csrf_token"

# Header HTMX will send on every POST (via ``hx-headers`` set on
# ``<body>`` in the base template). Configured to match the meta-tag
# name so JS can read it as ``document.querySelector('meta[name=...]')``.
_CSRF_HEADER = "X-CSRF-Token"

# Fallback form field for non-HTMX flows. Not currently used by any
# template, but supported so a plain HTML form can also authenticate
# without JavaScript.
_CSRF_FORM_FIELD = "_csrf"


def issue_csrf_token(request: Request) -> str:
    """Generate + store a fresh CSRF token on the request session.

    Called from the OAuth callback right after ``operator_id`` lands.
    Returns the raw token string so the caller can set the
    ``wodbuster_csrf`` cookie on the redirect response.

    Regenerating on every successful login (rather than every request)
    keeps HTMX simple: the meta tag rendered by
    ``templates/index.html`` remains valid for the whole session.
    """
    token = secrets.token_urlsafe(16)
    request.session[_SESSION_KEY] = token
    return token


def get_csrf_token(request: Request) -> str | None:
    """Return the current session's CSRF token, or ``None`` if absent."""
    value = request.session.get(_SESSION_KEY)
    return value if isinstance(value, str) else None


async def verify_csrf(request: Request) -> None:
    """FastAPI dependency: raise 403 if the request lacks a valid token.

    Reads the token from the ``X-CSRF-Token`` header first (HTMX path),
    then falls back to the ``_csrf`` form field for plain-HTML forms.
    A missing session token *or* a missing request token is a 403; the
    comparison uses :func:`hmac.compare_digest` to keep timing
    consistent.

    Applied on every state-mutating POST. Routes that also require an
    authenticated operator layer this dependency alongside
    :func:`require_session`; ordering does not matter because both
    dependencies read from ``request.session`` and neither mutates it.
    """
    expected = get_csrf_token(request)
    if expected is None:
        _raise_forbidden()

    provided = request.headers.get(_CSRF_HEADER)
    if provided is None:
        # Try the form-encoded fallback. This awaits the body, which
        # FastAPI would otherwise consume via a ``Form`` parameter;
        # doing it here means dependency ordering does not matter.
        form = await request.form()
        raw = form.get(_CSRF_FORM_FIELD)
        provided = raw if isinstance(raw, str) else None

    if provided is None or not hmac.compare_digest(expected or "", provided):
        _raise_forbidden()


def _raise_forbidden() -> None:
    """Raise the standard 403 for a failing CSRF check.

    Extracted so the message text and status stay consistent across
    both the "no session token" and "mismatched token" branches; the
    global exception handler in :mod:`app` renders a template body
    with no operator data.
    """
    raise HTTPException(
        status_code=403,
        detail="CSRF token missing or invalid",
    )


__all__ = [
    "CSRF_COOKIE_NAME",
    "get_csrf_token",
    "issue_csrf_token",
    "verify_csrf",
]
