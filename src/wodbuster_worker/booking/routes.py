"""Booking history + cancel routes (US6.2, H.1 lite).

Routes:

- ``GET /history`` — the operator's recent booking attempts. One row
  per outcome, newest first, with a cancel button on every
  ``granted`` row.
- ``POST /bookings/{id}/cancel`` — invokes the
  :func:`cancel_booking` service and redirects back to /history with
  a flash-style result. CSRF-protected. Idempotent per CC-015.

Kept in its own router so the rules router stays focused on rule
CRUD. Every route is auth-gated.
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
from ..gyms.context import active_gym_account_id
from ..gyms.service import gym_client_factory, resolve_gym_client
from ..i18n import get_language, lang_url, t
from ..persistence.engine import get_session
from ..persistence.models import BookingOutcome, SchedulerRule
from ..rules.service import list_rules_for_operator
from ..scheduler.rule_jobs import operator_timezone
from .overrides import is_editable

_log = structlog.get_logger(__name__)

router = APIRouter(tags=["history"])


_DAY_LABEL_KEYS = (
    "day.monday",
    "day.tuesday",
    "day.wednesday",
    "day.thursday",
    "day.friday",
    "day.saturday",
    "day.sunday",
)


def _day_label(weekday: int) -> str:
    """Return the current-language name for ``weekday`` (0=Monday)."""
    return t(_DAY_LABEL_KEYS[weekday])


# Locale-neutral month abbreviations for the server-rendered fallback
# label only (data, not UI copy — same rationale as the Telegram
# tables in notifications/messages.py). The client-side upgrade
# (wb-datetime.js) replaces this text once JS runs; this only covers
# first paint / no-JS.
_MONTH_ABBR: dict[str, tuple[str, ...]] = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "es": ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"),
}


def _short_date_time_label(dt: datetime) -> str:
    """Fallback label ``"20 Aug 22:40"`` / ``"20 ago 22:40"``.

    No English connector word (dropped "at" rather than translate it)
    so the shape matches across languages, mirroring the Telegram
    ``format_slot`` convention.
    """
    month = _MONTH_ABBR.get(get_language(), _MONTH_ABBR["en"])[dt.month - 1]
    return f"{dt.day:02d} {month} {dt.strftime('%H:%M')}"


def _utcnow() -> datetime:
    """Current UTC instant.

    A single seam for "now" so time-sensitive views (the week-scoped
    attempts table) can be frozen deterministically in tests instead of
    depending on the wall clock of the machine running the suite.
    """
    return datetime.now(tz=UTC)


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
    now = _utcnow()
    week_start = _current_week_start(now)
    gym_account_id = active_gym_account_id(request)
    with get_session() as session:
        upcoming: list[UpcomingSlot] = []
        outcomes: list[BookingOutcome] = []
        rules_by_id: dict[int, SchedulerRule] = {}
        if gym_account_id is not None:
            upcoming = list_upcoming_slots(session, gym_account_id, now=now)
            outcomes = list_recent_bookings(session, gym_account_id, since=week_start)
            # The projection carries no rule object, and the edit cutoff
            # is rule arithmetic, so the rules are loaded once here
            # rather than re-queried per row.
            rules_by_id = {
                int(rule.id): rule for rule in list_rules_for_operator(session, gym_account_id)
            }
        upcoming_days = _group_upcoming_by_day(upcoming, rules_by_id=rules_by_id, now=now)
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

    factory = gym_client_factory(request.app.state)
    store = getattr(request.app.state, "cookie_store", None)
    if factory is None or store is None:
        # Booking stack not wired (config missing). Fail loud so the
        # operator sees the actual reason rather than a silent noop.
        return _redirect_with_flash(
            t("flash.booking.service_unavailable"),
            kind="error",
        )

    gym_account_id = active_gym_account_id(request)
    if gym_account_id is None:
        raise HTTPException(status_code=404)
    with get_session() as session:
        resolved = resolve_gym_client(factory, session, gym_account_id)
        if resolved is None:
            raise HTTPException(status_code=404)
        client, _idu = resolved
        try:
            cancel_booking(
                session,
                gym_account_id=gym_account_id,
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
        "day_label": _day_label(slot.weekday()),
        "slot_datetime_label": _short_date_time_label(slot),
        "terminal_status": outcome.terminal_status,
        # Orthogonal to the status (ADR-0012 Decision 4). It is the only
        # thing on the row that tells a plain granted from one substituted
        # after an unavailable override, and a vacation skip from a day
        # the user marked as skipped.
        "outcome_source": outcome.outcome_source,
        "fallback_index": outcome.granted_fallback_index,
        "attempted_at": outcome.attempted_at.astimezone(tz),
        "cancellable": outcome.terminal_status == "granted"
        and outcome.target_slot.astimezone(UTC) > _utcnow(),
    }


def _group_upcoming_by_day(
    slots: list[UpcomingSlot],
    *,
    rules_by_id: dict[int, SchedulerRule],
    now: datetime,
) -> list[dict[str, Any]]:
    """Group upcoming attendance slots by local calendar day.

    Times are shown in the operator's zone (``WORKER_TIMEZONE``) so
    the operator reads "Wed 22 Jul at 21:30" the way they wrote the
    rule, not in UTC. ``granted`` (already secured), ``pending``
    (scheduler hasn't fired yet), ``vacation`` and ``modified`` (a
    single-day override replaced the rule's target) all flow through
    here; the template renders a chip per ``kind`` and the cancel
    button only when the slot has a ``booking_id``.

    ``editable`` is computed server side from :func:`is_editable`, so a
    row past its cutoff renders no edit action at all rather than an
    action the save route would refuse (FR-006).
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
                "rule_class_type": slot.rule_class_type,
                "rule_class_time": slot.rule_class_time,
                "validated": slot.validated,
                **_override_actions(slot, rules_by_id=rules_by_id, now=now),
            }
        )
    return groups


def _override_actions(
    slot: UpcomingSlot,
    *,
    rules_by_id: dict[int, SchedulerRule],
    now: datetime,
) -> dict[str, Any]:
    """Return the edit/revert URLs a row may offer.

    Only ``pending``, ``modified`` and ``skipped`` rows are editable:
    ``granted`` is already booked and ``vacation`` wins over any override
    (FR-005, FR-029). Revert is offered only where there is something to
    revert, which includes a skipped day: there is no booking to cancel,
    but the skip itself can be undone (FR-022).
    """
    rule = rules_by_id.get(slot.rule_id) if slot.rule_id is not None else None
    editable = (
        slot.kind in {"pending", "modified", "skipped"}
        and rule is not None
        and slot.target_date is not None
        and is_editable(rule, slot.target_date, now)
    )
    if not editable or slot.target_date is None:
        return {"editable": False, "edit_url": None, "revert_url": None}
    base = f"/history/overrides/{slot.rule_id}/{slot.target_date.isoformat()}"
    return {
        "editable": True,
        "edit_url": lang_url(base),
        "revert_url": (
            lang_url(f"{base}/revert") if slot.kind in {"modified", "skipped"} else None
        ),
    }


__all__ = ["router"]
