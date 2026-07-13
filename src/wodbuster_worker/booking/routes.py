"""Booking history + cancel routes (US6.2, H.1 lite).

Two routes:

- ``GET /history`` — the operator's recent booking attempts. One row
  per outcome, newest first, with a cancel button on every
  ``granted`` row.
- ``POST /bookings/{id}/cancel`` — invokes the
  :func:`cancel_booking` service and redirects back to /history with
  a flash-style result. CSRF-protected. Idempotent per CC-015.

Kept in its own router so the rules router stays focused on rule
CRUD. Both routes are auth-gated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..auth.csrf import get_csrf_token, verify_csrf
from ..auth.deps import require_session
from ..booking.cancellation import (
    BookingAlreadyCancelledError,
    BookingNotFoundError,
    CancellationUpstreamError,
    cancel_booking,
    list_recent_bookings,
)
from ..persistence.engine import get_session
from ..persistence.models import BookingOutcome

_log = structlog.get_logger(__name__)

router = APIRouter(tags=["history"])


_DAY_LABELS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


def _templates(request: Request) -> Jinja2Templates:
    templates = getattr(request.app.state, "templates", None)
    if templates is None:  # pragma: no cover - misconfiguration
        raise RuntimeError("app.state.templates not configured")
    assert isinstance(templates, Jinja2Templates)
    return templates


@router.get("/history", name="history_list")
def history_list(
    request: Request,
    operator_id: int = Depends(require_session),
    flash: str | None = None,
    flash_kind: str = "info",
) -> Response:
    """List the operator's most recent booking outcomes."""
    templates = _templates(request)
    with get_session() as session:
        outcomes = list_recent_bookings(session, operator_id)
        rows = [_outcome_to_row(o) for o in outcomes]
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            "rows": rows,
            "csrf_token": get_csrf_token(request) or "",
            "flash": flash,
            "flash_kind": flash_kind if flash_kind in {"info", "warning", "error"} else "info",
        },
    )


@router.post(
    "/bookings/{booking_id}/cancel",
    name="booking_cancel",
    dependencies=[Depends(verify_csrf)],
)
def booking_cancel(
    booking_id: int,
    request: Request,
    operator_id: int = Depends(require_session),
) -> Response:
    """Cancel one booking and redirect back to /history with a flash message."""
    _ = request  # signature parity with other routes

    client = getattr(request.app.state, "wodbuster_client", None)
    store = getattr(request.app.state, "cookie_store", None)
    if client is None or store is None:
        # Booking stack not wired (config missing). Fail loud so the
        # operator sees the actual reason rather than a silent noop.
        return _redirect_with_flash(
            "Booking service unavailable — check WodBuster configuration.",
            kind="error",
        )

    with get_session() as session:
        try:
            cancel_booking(
                session,
                operator_id=operator_id,
                booking_id=booking_id,
                client=client,
                cookie_store=store,
            )
        except BookingNotFoundError:
            raise HTTPException(status_code=404) from None
        except BookingAlreadyCancelledError:
            return _redirect_with_flash(
                "Already cancelled — no action taken.", kind="info"
            )
        except CancellationUpstreamError as exc:
            _log.warning(
                "booking.cancel.upstream_error",
                operator_id=operator_id,
                booking_id=booking_id,
                error=str(exc),
            )
            return _redirect_with_flash(
                f"Cancel failed: {exc}", kind="error"
            )

    return _redirect_with_flash(
        "Booking cancelled. WodBuster and Telegram updated.", kind="info"
    )


def _redirect_with_flash(message: str, *, kind: str) -> RedirectResponse:
    """303 back to /history with a URL-encoded flash message."""
    from urllib.parse import urlencode

    query = urlencode({"flash": message, "flash_kind": kind})
    return RedirectResponse(url=f"/history?{query}", status_code=303)


def _outcome_to_row(outcome: BookingOutcome) -> dict[str, Any]:
    """Build a view-model dict for a single history row."""
    slot = outcome.target_slot.astimezone(UTC)
    return {
        "id": int(outcome.id),
        "target_class": outcome.target_class,
        "target_slot": slot,
        "day_label": _DAY_LABELS[slot.weekday()],
        "slot_label": slot.strftime("%d %b %H:%M UTC"),
        "terminal_status": outcome.terminal_status,
        "fallback_index": outcome.granted_fallback_index,
        "attempted_at": outcome.attempted_at.astimezone(UTC),
        "cancellable": outcome.terminal_status == "granted"
        and outcome.target_slot.astimezone(UTC) > datetime.now(tz=UTC),
    }


__all__ = ["router"]
