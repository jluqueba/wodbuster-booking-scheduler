"""Scheduler rule CRUD routes (rule model v2).

Routes (all under ``/rules``):

- ``GET /rules``             list operator's rules (one row per day)
- ``GET /rules/new``         empty create form (multi-day)
- ``POST /rules``            fan-out create; 303 to list; 422 on failure
- ``GET /rules/api/classes`` JSON picker source (types + time slots)
- ``GET /rules/{id}``        edit form (single day)
- ``POST /rules/{id}``       update; 303 to list; 422 on failure; 404 for non-owned
- ``POST /rules/{id}/delete`` delete; 303 to list; 404 for non-owned

Both mutating flows are CSRF-protected. All routes are auth-gated.

The ``/api/*`` routes are declared above the ``/{rule_id}`` handlers
so FastAPI does not try to parse ``api`` as an integer.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..auth.csrf import get_csrf_token, verify_csrf
from ..auth.deps import require_session
from ..booking.executor import BookingExecutor
from ..persistence.cookie_store import CookieStore
from ..persistence.engine import get_session
from ..persistence.models import SchedulerRule
from ..scheduler.rule_jobs import register_rule_job, unregister_rule_job
from ..wodbuster_client.client import WodBusterClient
from .classes import AvailableClasses, fetch_available_classes
from .forms import parse_create_rule_form, parse_edit_rule_form
from .service import (
    create_rules_for_days,
    delete_rule,
    get_rule_for_operator,
    list_rules_for_operator,
    update_rule,
)

router = APIRouter(prefix="/rules", tags=["rules"])

_DAY_LABELS = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]

_TIME_FALLBACK: list[str] = []


def _templates(request: Request) -> Jinja2Templates:
    templates = getattr(request.app.state, "templates", None)
    if templates is None:  # pragma: no cover - misconfiguration
        raise RuntimeError(
            "app.state.templates is not configured; wire it in lifespan()."
        )
    assert isinstance(templates, Jinja2Templates)
    return templates


def _picker_or_none(request: Request, operator_id: int) -> AvailableClasses | None:
    """Best-effort fetch of the class-type / time-slot picker.

    Returns ``None`` when any dependency is missing (cookie stack not
    wired, no cookie on file, WodBuster unreachable). The form
    template renders free-text inputs in that state.
    """
    store = getattr(request.app.state, "cookie_store", None)
    client = getattr(request.app.state, "wodbuster_client", None)
    if not isinstance(store, CookieStore) or not isinstance(client, WodBusterClient):
        return None
    return fetch_available_classes(store, client, operator_id)


def _render_form(
    request: Request,
    *,
    template: str,
    heading: str,
    action_url: str,
    form_values: Mapping[str, object],
    errors: Mapping[str, str],
    picker: AvailableClasses | None,
    delete_url: str | None = None,
    status_code: int = 200,
) -> Response:
    templates = _templates(request)
    return templates.TemplateResponse(
        request=request,
        name=template,
        context={
            "heading": heading,
            "action_url": action_url,
            "form_values": form_values,
            "errors": errors,
            "delete_url": delete_url,
            "day_labels": _DAY_LABELS,
            "picker_class_types": picker.class_types if picker else [],
            "picker_time_slots": picker.time_slots if picker else _TIME_FALLBACK,
            "picker_unavailable": picker is None,
            "csrf_token": get_csrf_token(request) or "",
        },
        status_code=status_code,
    )


@router.get("", name="rules_list")
def rules_list(
    request: Request, operator_id: int = Depends(require_session)
) -> Response:
    """Render the operator's rule list."""
    templates = _templates(request)
    with get_session() as session:
        rules = list_rules_for_operator(session, operator_id)
        rows = [
            {
                "id": rule.id,
                "day_label": _DAY_LABELS[rule.day_of_week],
                "day_of_week": rule.day_of_week,
                "class_type": rule.class_type,
                "class_time": rule.class_time,
                "booking_opens_days_before": rule.booking_opens_days_before,
                "booking_opens_at": rule.booking_opens_at,
                "second_shot_class_type": rule.second_shot_class_type,
                "second_shot_class_time": rule.second_shot_class_time,
                "active": rule.active,
            }
            for rule in rules
        ]
    return templates.TemplateResponse(
        request=request,
        name="rules/list.html",
        context={
            "rules": rows,
            "csrf_token": get_csrf_token(request) or "",
        },
    )


@router.get("/new", name="rules_new")
def rules_new(
    request: Request, operator_id: int = Depends(require_session)
) -> Response:
    """Render an empty create form pre-seeded with the picker options."""
    picker = _picker_or_none(request, operator_id)
    return _render_form(
        request,
        template="rules/create.html",
        heading="New rule",
        action_url="/rules",
        form_values={"booking_opens_days_before": "2", "booking_opens_at": "21:30"},
        errors={},
        picker=picker,
    )


@router.post("", name="rules_create", dependencies=[Depends(verify_csrf)])
async def rules_create(
    request: Request, operator_id: int = Depends(require_session)
) -> Response:
    """Fan-out create: N rules for N selected days."""
    form_data = _str_only(dict(await request.form()))
    parsed = parse_create_rule_form(form_data)

    if not parsed.is_valid:
        picker = _picker_or_none(request, operator_id)
        return _render_form(
            request,
            template="rules/create.html",
            heading="New rule",
            action_url="/rules",
            form_values=form_data,
            errors=parsed.errors,
            picker=picker,
            status_code=422,
        )

    assert parsed.class_type is not None
    assert parsed.class_time is not None
    assert parsed.booking_opens_days_before is not None
    assert parsed.booking_opens_at is not None
    with get_session() as session:
        created_rules = create_rules_for_days(
            session,
            operator_id=operator_id,
            days_of_week=parsed.days_of_week,
            class_type=parsed.class_type,
            class_time=parsed.class_time,
            booking_opens_days_before=parsed.booking_opens_days_before,
            booking_opens_at=parsed.booking_opens_at,
            second_shot_class_type=parsed.second_shot_class_type,
            second_shot_class_time=parsed.second_shot_class_time,
        )

    _sync_after_create(request, list(created_rules))
    return RedirectResponse(url="/rules", status_code=303)


@router.get("/api/classes", name="rules_api_classes")
def rules_api_classes(
    request: Request, operator_id: int = Depends(require_session)
) -> Response:
    """Return the distinct class-types and time-slots from the gym schedule.

    Consumed both by the form (server-side render seeds the dropdowns)
    and available for future HTMX refreshes. Failure modes collapse
    to an empty payload so the client can render its fallback.
    """
    picker = _picker_or_none(request, operator_id)
    if picker is None:
        return JSONResponse({"class_types": [], "time_slots": [], "available": False})
    return JSONResponse(
        {
            "class_types": picker.class_types,
            "time_slots": picker.time_slots,
            "available": True,
        }
    )


@router.get("/{rule_id}", name="rules_edit")
def rules_edit(
    rule_id: int,
    request: Request,
    operator_id: int = Depends(require_session),
) -> Response:
    """Render the edit form for an owned rule; 404 for anyone else's."""
    with get_session() as session:
        rule = get_rule_for_operator(session, operator_id, rule_id)
        if rule is None:
            raise HTTPException(status_code=404)
        form_values = _rule_to_form_values(rule)

    picker = _picker_or_none(request, operator_id)
    return _render_form(
        request,
        template="rules/edit.html",
        heading="Edit rule",
        action_url=f"/rules/{rule_id}",
        form_values=form_values,
        errors={},
        picker=picker,
        delete_url=f"/rules/{rule_id}/delete",
    )


@router.post("/{rule_id}", name="rules_update", dependencies=[Depends(verify_csrf)])
async def rules_update(
    rule_id: int,
    request: Request,
    operator_id: int = Depends(require_session),
) -> Response:
    """Update a rule owned by the operator."""
    form_data = _str_only(dict(await request.form()))
    parsed = parse_edit_rule_form(form_data)

    with get_session() as session:
        rule = get_rule_for_operator(session, operator_id, rule_id)
        if rule is None:
            raise HTTPException(status_code=404)

        if not parsed.is_valid:
            picker = _picker_or_none(request, operator_id)
            return _render_form(
                request,
                template="rules/edit.html",
                heading="Edit rule",
                action_url=f"/rules/{rule_id}",
                form_values=form_data,
                errors=parsed.errors,
                picker=picker,
                delete_url=f"/rules/{rule_id}/delete",
                status_code=422,
            )

        assert parsed.day_of_week is not None
        assert parsed.class_type is not None
        assert parsed.class_time is not None
        assert parsed.booking_opens_days_before is not None
        assert parsed.booking_opens_at is not None
        updated = update_rule(
            session,
            rule,
            day_of_week=parsed.day_of_week,
            class_type=parsed.class_type,
            class_time=parsed.class_time,
            booking_opens_days_before=parsed.booking_opens_days_before,
            booking_opens_at=parsed.booking_opens_at,
            second_shot_class_type=parsed.second_shot_class_type,
            second_shot_class_time=parsed.second_shot_class_time,
        )

    _sync_after_update(request, updated)
    return RedirectResponse(url="/rules", status_code=303)


@router.post(
    "/{rule_id}/delete",
    name="rules_delete",
    dependencies=[Depends(verify_csrf)],
)
def rules_delete(
    rule_id: int,
    request: Request,
    operator_id: int = Depends(require_session),
) -> Response:
    """Delete a rule owned by the operator."""
    _ = request  # signature parity with the other routes
    with get_session() as session:
        rule = get_rule_for_operator(session, operator_id, rule_id)
        if rule is None:
            raise HTTPException(status_code=404)
        delete_rule(session, rule)
    _sync_after_delete(request, rule_id)
    return RedirectResponse(url="/rules", status_code=303)


def _rule_to_form_values(rule: SchedulerRule) -> dict[str, str]:
    """Convert a loaded rule into the flat form-values dict for edit."""
    values: dict[str, str] = {
        "day_of_week": str(rule.day_of_week),
        "class_type": rule.class_type,
        "class_time": rule.class_time,
        "booking_opens_days_before": str(rule.booking_opens_days_before),
        "booking_opens_at": rule.booking_opens_at,
    }
    if rule.second_shot_class_type is not None:
        values["second_shot_class_type"] = rule.second_shot_class_type
    if rule.second_shot_class_time is not None:
        values["second_shot_class_time"] = rule.second_shot_class_time
    return values


def _str_only(form_data: Mapping[str, object]) -> dict[str, str]:
    """Filter Starlette FormData to text-only entries."""
    return {k: v for k, v in form_data.items() if isinstance(v, str)}


# ---------------------------------------------------------------------------
# Scheduler sync (US1.10 hot reload)
# ---------------------------------------------------------------------------


def _scheduler_bits(request: Request) -> tuple[Any, BookingExecutor] | None:
    """Return (scheduler, executor) from app.state if booking is wired.

    Missing when the operator has not seeded ``wodbuster_gym`` /
    ``wodbuster_idu`` / cookie encryption key. In that state the app
    still serves rules CRUD but bookings cannot fire; rule mutations
    are no-ops from the scheduler's perspective.
    """
    scheduler = getattr(request.app.state, "booking_scheduler", None)
    executor = getattr(request.app.state, "booking_executor", None)
    if scheduler is None or executor is None:
        return None
    return scheduler, executor


def _sync_after_create(request: Request, rules: list[SchedulerRule]) -> None:
    """Register a booking job for every newly-created rule."""
    bits = _scheduler_bits(request)
    if bits is None:
        return
    scheduler, executor = bits
    for rule in rules:
        try:
            register_rule_job(
                scheduler,
                rule,
                executor=executor,
                session_factory=get_session,
            )
        except ValueError:
            # Malformed HH:MM would be a data bug; the form validator
            # already blocks this. Silence rather than crash the
            # request — the operator's data is on file even if the
            # scheduler skipped it.
            continue


def _sync_after_update(request: Request, rule: SchedulerRule) -> None:
    """Re-register the rule's job with fresh timing (idempotent)."""
    bits = _scheduler_bits(request)
    if bits is None:
        return
    scheduler, executor = bits
    with contextlib.suppress(ValueError):
        register_rule_job(
            scheduler,
            rule,
            executor=executor,
            session_factory=get_session,
        )


def _sync_after_delete(request: Request, rule_id: int) -> None:
    """Remove the booking job for the deleted rule."""
    bits = _scheduler_bits(request)
    if bits is None:
        return
    scheduler, _executor = bits
    unregister_rule_job(scheduler, rule_id)


__all__ = ["router"]
