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

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

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
from ..booking.upcoming import UpcomingSlot, list_upcoming_slots
from ..i18n import lang_url, t
from ..persistence.engine import get_session
from ..persistence.models import BookingOutcome
from ..scheduler.rule_jobs import operator_timezone

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
    now = datetime.now(tz=UTC)
    week_start = _current_week_start(now)
    with get_session() as session:
        upcoming = list_upcoming_slots(session, operator_id, now=now)
        outcomes = list_recent_bookings(session, operator_id, since=week_start)
        upcoming_days = _group_upcoming_by_day(upcoming)
        rows = [_outcome_to_row(o) for o in outcomes]
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            "upcoming_days": upcoming_days,
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
            t("flash.booking.service_unavailable"),
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
            return _redirect_with_flash(t("flash.booking.already_cancelled"), kind="info")
        except CancellationUpstreamError as exc:
            _log.warning(
                "booking.cancel.upstream_error",
                operator_id=operator_id,
                booking_id=booking_id,
                error=str(exc),
            )
            return _redirect_with_flash(
                t("flash.booking.cancel_failed", reason=str(exc)), kind="error"
            )

    return _redirect_with_flash(t("flash.booking.cancelled"), kind="info")


def _current_week_start(now: datetime) -> datetime:
    """Return Monday 00:00 of ``now``'s week, in the operator's zone, as UTC.

    The history "attempts" table is scoped to the current week so it
    can't grow unbounded. The week boundary is computed in the
    operator's timezone (``WORKER_TIMEZONE``) so "this week" matches
    their local calendar, then converted back to UTC for the query
    (attempts are stored UTC).
    """
    local = now.astimezone(operator_timezone())
    monday = (local - timedelta(days=local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday.astimezone(UTC)


def _redirect_with_flash(message: str, *, kind: str) -> RedirectResponse:
    """303 back to /history with a URL-encoded flash message."""
    query = urlencode({"flash": message, "flash_kind": kind})
    return RedirectResponse(url=f"{lang_url('/history')}?{query}", status_code=303)


def _outcome_to_row(outcome: BookingOutcome) -> dict[str, Any]:
    """Build a view-model dict for a single history row."""
    tz = operator_timezone()
    slot = outcome.target_slot.astimezone(tz)
    return {
        "id": int(outcome.id),
        "target_class": outcome.target_class,
        "target_slot": slot,
        "day_label": _DAY_LABELS[slot.weekday()],
        "slot_datetime_label": slot.strftime("%d %b at %H:%M"),
        "terminal_status": outcome.terminal_status,
        "fallback_index": outcome.granted_fallback_index,
        "attempted_at": outcome.attempted_at.astimezone(tz),
        "cancellable": outcome.terminal_status == "granted"
        and outcome.target_slot.astimezone(UTC) > datetime.now(tz=UTC),
    }


def _group_upcoming_by_day(
    slots: list[UpcomingSlot],
) -> list[dict[str, Any]]:
    """Group upcoming attendance slots by local calendar day.

    Times are shown in the operator's zone (``WORKER_TIMEZONE``) so
    the operator reads "Wed 22 Jul at 21:30" the way they wrote the
    rule, not in UTC. Both ``granted`` (already secured) and
    ``pending`` (scheduler hasn't fired yet) slots flow through
    here; the template renders a chip per ``kind`` and the cancel
    button only when the slot has a ``booking_id``.
    """
    tz = operator_timezone()
    groups: list[dict[str, Any]] = []
    current_key: str | None = None
    for slot in slots:
        local = slot.target_slot.astimezone(tz)
        key = local.date().isoformat()
        if key != current_key:
            groups.append(
                {
                    "date_label": local.strftime("%a %d %b"),
                    "date_dt": local,
                    "iso_date": key,
                    "rows": [],
                }
            )
            current_key = key
        groups[-1]["rows"].append(
            {
                "kind": slot.kind,
                "id": slot.booking_id,
                "target_class": slot.target_class,
                "time_label": local.strftime("%H:%M"),
                "slot_dt": local,
                "fallback_index": slot.fallback_index,
            }
        )
    return groups


__all__ = ["router"]
