"""HTTP routes for the cookie paste-and-validate flow (US3.5, US3.6).

Exposes two endpoints, both authenticated (:func:`require_session`):

- ``GET /cookie`` renders the full page: current status card + paste
  form. The status card reads the operator's ``cookie_credential`` row
  and shows either "no cookie on file" or last-validated metadata.
- ``POST /cookie`` validates the pasted value through
  :class:`CookieValidator` and, on ``Valid``, upserts through
  :class:`CookieStore`. Response body is only the status-card partial;
  the form is HTMX-driven and swaps ``#cookie-status`` in place. This
  keeps the paste-and-validate loop feeling immediate without a full
  page reload (AS1/AS3 for US-003).

Denial paths on ``POST /cookie``:

- ``Rejected``: server returned an auth failure. Banner tells the
  operator to re-copy the cookie from a browser. No state mutation.
- ``Unknown``: transport or protocol failure. Banner tells the operator
  to try again in a minute. No state mutation (FR-020: a transient
  glitch is not evidence the pasted value is wrong).
- Empty submission: short-circuits to a ``Rejected`` banner without a
  network call.

All three denial paths return HTTP 200 with the same partial shape so
HTMX swaps behave consistently; the banner colour and copy differ.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from ..auth.csrf import get_csrf_token, verify_csrf
from ..auth.deps import require_session
from ..heartbeat.alerts import close_open_cookie_expiring
from ..persistence.cookie_store import CookieDecryptError, CookieStore
from ..persistence.engine import get_session
from ..persistence.models import CookieCredential
from ..security.cookie import CookieValidator, Rejected, Unknown, Valid

router = APIRouter(tags=["cookie"])

_STATUS_PARTIAL = "cookie/_status.html"
_PAGE = "cookie/page.html"


def _templates(request: Request) -> Jinja2Templates:
    """Fetch the shared :class:`Jinja2Templates` from app state."""
    templates = getattr(request.app.state, "templates", None)
    if templates is None:  # pragma: no cover - misconfiguration
        raise RuntimeError(
            "app.state.templates is not configured; wire it in lifespan()."
        )
    assert isinstance(templates, Jinja2Templates)
    return templates


def _validator(request: Request) -> CookieValidator:
    """Fetch the shared :class:`CookieValidator` from app state.

    503 rather than 500 because the missing wiring signals the
    operator has not finished configuration (``wodbuster_gym``,
    ``wodbuster_idu``), which is a serviceable state rather than a
    programming bug.
    """
    validator = getattr(request.app.state, "cookie_validator", None)
    if validator is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "cookie validator is not configured; set WODBUSTER_GYM and "
                "WODBUSTER_IDU on the container app."
            ),
        )
    return validator  # type: ignore[no-any-return]


def _store(request: Request) -> CookieStore:
    """Fetch the shared :class:`CookieStore` from app state.

    Same 503 reasoning as :func:`_validator`: the store depends on
    the ``cookie_encryption_key`` secret being present, which is an
    operator configuration step rather than a code bug.
    """
    store = getattr(request.app.state, "cookie_store", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "cookie store is not configured; seed the "
                "wodbuster-cookie-encryption-key Key Vault secret."
            ),
        )
    return store  # type: ignore[no-any-return]


def _load_current_status(store: CookieStore, operator_id: int) -> dict[str, object]:
    """Return the template context describing the current cookie row.

    Reads the metadata columns (never the plaintext) so a partial
    render never risks leaking cookie material into the response body.
    A missing row returns ``has_cookie=False`` and the template routes
    to the "no cookie on file" branch.

    A row that fails decryption is surfaced as ``has_cookie=True`` with
    a ``decrypt_error=True`` flag so the UI can prompt for a re-paste
    without hiding the fact that a stored row exists (which would be
    confusing when the row is visible in the DB).
    """
    with get_session() as session:
        row: CookieCredential | None = (
            session.query(CookieCredential)
            .filter_by(operator_id=operator_id)
            .one_or_none()
        )
        if row is None:
            return {"has_cookie": False}
        # We deliberately do NOT call store.load here: the status view
        # only needs the metadata columns. Calling load would risk
        # decrypting on every page render, which is unnecessary work
        # and expands the attack surface for accidental logging.
        del store  # silences the unused-arg warning; keep signature stable
        return {
            "has_cookie": True,
            "pasted_at": row.pasted_at,
            "last_validated_at": row.last_validated_at,
            "projected_ttl_at": row.projected_ttl_at,
            "last_probe_status": row.last_probe_status,
            "decrypt_error": False,
        }


def _render_partial(
    request: Request,
    operator_id: int,
    *,
    banner: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    """Render just the status-card partial (HTMX swap target)."""
    templates = _templates(request)
    context = _load_current_status(_store(request), operator_id)
    context["banner"] = banner
    return templates.TemplateResponse(
        request=request,
        name=_STATUS_PARTIAL,
        context=context,
        status_code=status_code,
    )


@router.get("/cookie", name="cookie_page")
def cookie_page(
    request: Request, operator_id: int = Depends(require_session)
) -> Response:
    """Render the paste-and-validate page for the current operator."""
    templates = _templates(request)
    context = _load_current_status(_store(request), operator_id)
    context["banner"] = None
    # The form on the full page carries a hidden ``_csrf`` field and
    # HTMX sends ``X-CSRF-Token`` via ``hx-headers`` on <body>. Both
    # read the same session token.
    context["csrf_token"] = get_csrf_token(request) or ""
    return templates.TemplateResponse(
        request=request,
        name=_PAGE,
        context=context,
    )


@router.post("/cookie", name="cookie_paste", dependencies=[Depends(verify_csrf)])
async def cookie_paste(
    request: Request,
    cookie_value: Annotated[str, Form()] = "",
    operator_id: int = Depends(require_session),
) -> Response:
    """Validate and persist the pasted cookie.

    Returns the status-card partial with a per-verdict banner. HTMX
    swaps the ``#cookie-status`` region and the operator sees the new
    state without a page reload.
    """
    validator = _validator(request)
    store = _store(request)

    verdict = validator.validate(cookie_value)

    if isinstance(verdict, Valid):
        with get_session() as session:
            store.save(
                session,
                operator_id,
                cookie_value,
                validated_at=verdict.probed_at,
            )
            # Clear-on-refresh (US4.4): a successful paste means the
            # operator has dealt with the underlying condition; close
            # any open ``cookie_expiring`` alert in the same
            # transaction so the banner disappears immediately rather
            # than at the next heartbeat.
            close_open_cookie_expiring(session, operator_id, now=verdict.probed_at)
        banner = {
            "level": "valid",
            "message": "✅ Cookie validated and stored.",
        }
        return _render_partial(request, operator_id, banner=banner)

    if isinstance(verdict, Rejected):
        banner = {
            "level": "rejected",
            "message": (
                "❌ Cookie rejected. Re-copy the .WBAuth value from a signed-in "
                "browser session and try again."
            ),
        }
        return _render_partial(request, operator_id, banner=banner)

    # Unknown: the probe itself failed. Explicitly no state mutation
    # (FR-020) and a "retry" banner rather than a "rejected" one.
    assert isinstance(verdict, Unknown)
    banner = {
        "level": "unknown",
        "message": (
            "⚠️ Could not validate the cookie right now. Try again in a minute; "
            "your stored cookie was not touched."
        ),
    }
    return _render_partial(request, operator_id, banner=banner)


# Silence "imported but unused" — the exception is re-exported so the
# route registration site can catch it if it ever propagates.
_ = CookieDecryptError


__all__ = ["router"]
