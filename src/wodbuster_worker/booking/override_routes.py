"""Single-day booking override routes (T-BDO-006, ADR-0012).

Three routes under ``/history/overrides``:

- ``GET  /history/overrides/{rule_id}/{target_date}``        the edit form
- ``POST /history/overrides/{rule_id}/{target_date}``        upsert
- ``POST /history/overrides/{rule_id}/{target_date}/revert`` delete

Ownership is resolved exactly once per request, by
:func:`_resolve_rule`: the acting gym account comes from the web session
and the rule from :func:`get_rule_for_operator`, which returns ``None``
for a rule outside that account. Both that case and a rule that does not
exist raise the same bodiless 404, so the route is not an enumeration
oracle (CC-013). No route reads a gym account from the path, the query
string or the body.

Both writes are CSRF-protected and re-check the edit cutoff and the
already-executed guard server side. What the form rendered is a UI
convenience, never the enforcement point (FR-007, INV-005).

This task covers class time, class type and the skip mark. The
second-attempt controls (T-BDO-016) land later; the rule's second shot
is displayed read-only here because FR-017 requires the user to see what
will still run.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..auth.csrf import get_csrf_token, verify_csrf
from ..auth.deps import require_session
from ..gyms.context import active_gym_account_id
from ..gyms.service import gym_client_factory, resolve_gym_client
from ..i18n import lang_url, t
from ..persistence.cookie_store import CookieStore
from ..persistence.engine import get_session
from ..persistence.models import BookingDayOverride, SchedulerRule
from ..rules.classes import (
    AvailableClasses,
    DateSchedule,
    fetch_available_classes,
    fetch_classes_for_date,
)
from ..rules.service import get_rule_for_operator
from .overrides import (
    OverrideSkipConflictError,
    OverrideWindowClosedError,
    delete_override,
    effective_slot_for,
    is_editable,
    load_override,
    save_override,
)

# The history module's clock seam, reused rather than duplicated: the
# edit action /history renders and the cutoff these routes enforce must
# read the same "now", or the UI and the guard disagree by a request.
from .routes import _utcnow

router = APIRouter(prefix="/history/overrides", tags=["overrides"])

_HHMM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")

# Mirrors ``scheduler_rule.class_type`` so an effective value substitutes
# without truncation.
_MAX_CLASS_TYPE_LEN = 200


def _templates(request: Request) -> Jinja2Templates:
    templates = getattr(request.app.state, "templates", None)
    if templates is None:  # pragma: no cover - misconfiguration
        raise RuntimeError("app.state.templates not configured")
    assert isinstance(templates, Jinja2Templates)
    return templates


def _resolve_rule(
    request: Request, session: Session, rule_id: int, target_date: date
) -> SchedulerRule:
    """Return the acting gym account's rule for ``target_date``, or 404.

    The single ownership check for all three routes (CC-013). A rule that
    belongs to somebody else, a rule that does not exist, and a date the
    rule does not project all raise the identical bodiless 404.
    """
    gym_account_id = active_gym_account_id(request)
    if gym_account_id is None:
        raise HTTPException(status_code=404)
    rule = get_rule_for_operator(session, gym_account_id, rule_id)
    if rule is None or target_date.weekday() != int(rule.day_of_week):
        raise HTTPException(status_code=404)
    return rule


@router.get("/{rule_id}/{target_date}", name="override_form")
def override_form(
    rule_id: int,
    target_date: date,
    request: Request,
    operator_id: int = Depends(require_session),
) -> Response:
    """Render the override form for one projected day."""
    _ = operator_id  # ownership is resolved through the active gym account
    now = _utcnow()
    with get_session() as session:
        rule = _resolve_rule(request, session, rule_id, target_date)
        if not is_editable(rule, target_date, now):
            # Past the cutoff there is nothing to offer, and a past date
            # is the same condition (FR-005, FR-006).
            raise HTTPException(status_code=404)
        existing = load_override(session, rule_id=int(rule.id), target_date=target_date)
        form_values = {
            "class_type": _or_rule(existing.class_type if existing else None, rule.class_type),
            "class_time": _or_rule(existing.class_time if existing else None, rule.class_time),
        }
        return _render(
            request,
            rule=rule,
            target_date=target_date,
            form_values=form_values,
            errors={},
            existing=existing,
            schedule=_probe_date(
                request,
                rule=rule,
                target_date=target_date,
                class_time=form_values["class_time"],
            ),
        )


@router.post(
    "/{rule_id}/{target_date}",
    name="override_save",
    dependencies=[Depends(verify_csrf)],
)
def override_save(
    rule_id: int,
    target_date: date,
    request: Request,
    class_type: str = Form(default=""),
    class_time: str = Form(default=""),
    skip_day: str = Form(default=""),
    operator_id: int = Depends(require_session),
) -> Response:
    """Upsert the override for one projected day (FR-001, FR-002, FR-003)."""
    _ = operator_id
    now = _utcnow()
    with get_session() as session:
        rule = _resolve_rule(request, session, rule_id, target_date)
        if not is_editable(rule, target_date, now):
            # Redirect rather than 422 so the user lands on the refreshed
            # day state instead of a stale form (Scenario 5, CC-003).
            return _redirect_with_flash(t("flash.override.window_closed"), kind="error")

        submitted_type = class_type.strip()
        submitted_time = class_time.strip()
        form_values = {"class_type": submitted_type, "class_time": submitted_time}

        if _checked(skip_day):
            return _skip(
                request,
                session,
                rule=rule,
                target_date=target_date,
                form_values=form_values,
                now=now,
            )

        errors = _field_errors(submitted_type, submitted_time)
        if errors:
            return _render(
                request,
                rule=rule,
                target_date=target_date,
                form_values=form_values,
                errors=errors,
                existing=load_override(session, rule_id=int(rule.id), target_date=target_date),
                schedule=_probe_date(
                    request, rule=rule, target_date=target_date, class_time=submitted_time
                ),
                status_code=422,
            )

        # NULL on a dimension the override leaves alone, so the row
        # records what changed rather than a copy of the rule.
        override_type = submitted_type if submitted_type != str(rule.class_type) else None
        override_time = submitted_time if submitted_time != str(rule.class_time) else None
        if override_type is None and override_time is None:
            # Submitting the rule's own values carries no effect, which
            # ``ck_booking_day_override_has_change`` rejects. Treat it as
            # what the user meant: back to the rule (FR-022).
            return _revert(session, rule=rule, target_date=target_date, now=now)

        schedule = _probe_date(
            request,
            rule=rule,
            target_date=target_date,
            class_time=submitted_time,
        )
        blocked = (
            schedule is not None
            and schedule.published
            and not schedule.has(submitted_type, submitted_time)
        )
        if blocked:
            # A published schedule that does not carry the pair is the one
            # blocking case (FR-008, CC-004).
            return _render(
                request,
                rule=rule,
                target_date=target_date,
                form_values=form_values,
                errors={"class_type": t("override.error.combination_unavailable")},
                existing=load_override(session, rule_id=int(rule.id), target_date=target_date),
                schedule=schedule,
                status_code=422,
            )

        try:
            save_override(
                session,
                rule=rule,
                target_date=target_date,
                class_type=override_type,
                class_time=override_time,
                # True only against a published schedule that confirmed
                # the pair; anything else re-validates at trigger time.
                validated=schedule is not None and schedule.published,
                now=now,
            )
        except OverrideWindowClosedError:
            return _redirect_with_flash(_closed_message(rule, target_date, now), kind="error")
    return _redirect_with_flash(t("flash.override.saved"), kind="info")


@router.post(
    "/{rule_id}/{target_date}/revert",
    name="override_revert",
    dependencies=[Depends(verify_csrf)],
)
def override_revert(
    rule_id: int,
    target_date: date,
    request: Request,
    operator_id: int = Depends(require_session),
) -> Response:
    """Delete the override and return the day to the rule (FR-022, CC-015)."""
    _ = operator_id
    now = _utcnow()
    with get_session() as session:
        rule = _resolve_rule(request, session, rule_id, target_date)
        return _revert(session, rule=rule, target_date=target_date, now=now)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _revert(
    session: Session, *, rule: SchedulerRule, target_date: date, now: datetime
) -> RedirectResponse:
    """Delete the override, reporting a closed window as a flash.

    Deleting nothing is not an error: a double submission must be
    idempotent, not a 500.
    """
    try:
        delete_override(session, rule=rule, target_date=target_date, now=now)
    except OverrideWindowClosedError:
        return _redirect_with_flash(_closed_message(rule, target_date, now), kind="error")
    return _redirect_with_flash(t("flash.override.reverted"), kind="info")


def _skip(
    request: Request,
    session: Session,
    *,
    rule: SchedulerRule,
    target_date: date,
    form_values: dict[str, str],
    now: datetime,
) -> Response:
    """Mark the day as skipped (FR-002, FR-003, INV-002).

    A skip is exclusive and always valid: it carries no alternative
    target, so there is nothing to probe and nothing to validate. The
    save clears whatever the previous override held, which is how a time
    override is turned into a skip in one upsert.
    """
    if form_values["class_type"] or form_values["class_time"]:
        # Only reachable through a crafted POST: the skip control posts
        # on its own. Answer with a field error, not an integrity error.
        return _render(
            request,
            rule=rule,
            target_date=target_date,
            form_values=form_values,
            errors={"class_type": t("override.error.skip_exclusive")},
            existing=load_override(session, rule_id=int(rule.id), target_date=target_date),
            schedule=_probe_date(
                request, rule=rule, target_date=target_date, class_time=str(rule.class_time)
            ),
            status_code=422,
        )
    try:
        save_override(
            session,
            rule=rule,
            target_date=target_date,
            skip_day=True,
            now=now,
        )
    except OverrideSkipConflictError:  # pragma: no cover - guarded above
        return _redirect_with_flash(t("override.error.skip_exclusive"), kind="error")
    except OverrideWindowClosedError:
        return _redirect_with_flash(_closed_message(rule, target_date, now), kind="error")
    return _redirect_with_flash(t("flash.override.skipped"), kind="info")


def _checked(raw: str) -> bool:
    """Whether a checkbox-shaped form value was submitted as set."""
    return raw.strip().lower() in {"1", "true", "on", "yes"}


def _closed_message(rule: SchedulerRule, target_date: date, now: datetime) -> str:
    """Tell the two write-refusal causes apart for the user.

    ``save_override`` and ``delete_override`` raise one exception type for
    both the cutoff and the already-executed guard; re-testing the cutoff
    here is the cheapest way to say which one fired.
    """
    if not is_editable(rule, target_date, now):
        return t("flash.override.window_closed")
    return t("flash.override.already_executed")


def _field_errors(class_type: str, class_time: str) -> dict[str, str]:
    if not class_type or len(class_type) > _MAX_CLASS_TYPE_LEN:
        return {"class_type": t("override.error.invalid_class_type")}
    if not _HHMM.match(class_time):
        return {"class_time": t("override.error.invalid_time")}
    return {}


def _or_rule(override_value: str | None, rule_value: object) -> str:
    """Return the override's value, falling back to the rule's."""
    return override_value if override_value else str(rule_value)


def _redirect_with_flash(message: str, *, kind: str) -> RedirectResponse:
    """303 back to /history so the user sees the refreshed day state."""
    query = urlencode({"flash": message, "flash_kind": kind})
    return RedirectResponse(url=f"{lang_url('/history')}?{query}", status_code=303)


def _probe_date(
    request: Request,
    *,
    rule: SchedulerRule,
    target_date: date,
    class_time: str,
) -> DateSchedule | None:
    """Probe the gym schedule for the day the executor will read.

    The ticks are derived from the *effective* slot, so a late-evening
    class whose UTC instant falls on the neighbouring calendar day is
    checked against the same day the booking attempt will (plan AMB-005).
    A malformed time falls back to the rule's, which only affects which
    day is probed for selector seeding; the submitted value is rejected
    by :func:`_field_errors` before it can reach a save.
    """
    probe_time = class_time if _HHMM.match(class_time) else str(rule.class_time)
    target_slot = effective_slot_for(rule, target_date, probe_time)
    gym_account_id = int(rule.gym_account_id)
    resolved = _resolve_client(request, gym_account_id)
    if resolved is None:
        return None
    store, client = resolved
    return fetch_classes_for_date(store, client, gym_account_id, target_slot=target_slot)


def _week_reference(request: Request, gym_account_id: int) -> AvailableClasses | None:
    """Week-scoped fallback set, offered when the date has no schedule."""
    resolved = _resolve_client(request, gym_account_id)
    if resolved is None:
        return None
    store, client = resolved
    return fetch_available_classes(store, client, gym_account_id)


def _resolve_client(request: Request, gym_account_id: int) -> tuple[CookieStore, Any] | None:
    """Return the cookie store + per-gym client, or ``None`` when unwired."""
    store = getattr(request.app.state, "cookie_store", None)
    factory = gym_client_factory(request.app.state)
    if not isinstance(store, CookieStore) or factory is None:
        return None
    with get_session() as session:
        resolved = resolve_gym_client(factory, session, gym_account_id)
    if resolved is None:
        return None
    client, _idu = resolved
    return store, client


def _seed_selectors(
    request: Request, *, gym_account_id: int, schedule: DateSchedule | None
) -> tuple[list[str], list[str], str | None]:
    """Return ``(class_types, time_slots, notice)`` for the selectors.

    The plan's behaviour matrix, in one place: real pairs for the date
    when the schedule is published, the week-scoped reference set
    (labelled as known weekday combinations) when it is not, and empty
    lists when neither probe answers, which degrades the fields to
    free-form carrying the rule's values.
    """
    if schedule is not None and schedule.published:
        return schedule.class_types, schedule.time_slots, None
    notice = "not_published" if schedule is not None else "probe_unavailable"
    reference = _week_reference(request, gym_account_id)
    if reference is None:
        return [], [], notice
    return reference.class_types, reference.time_slots, notice


def _render(
    request: Request,
    *,
    rule: SchedulerRule,
    target_date: date,
    form_values: dict[str, str],
    errors: dict[str, str],
    existing: BookingDayOverride | None,
    schedule: DateSchedule | None,
    status_code: int = 200,
) -> Response:
    """Render the override form."""
    gym_account_id = int(rule.gym_account_id)
    class_types, time_slots, notice = _seed_selectors(
        request, gym_account_id=gym_account_id, schedule=schedule
    )
    base = f"/history/overrides/{int(rule.id)}/{target_date.isoformat()}"
    templates = _templates(request)
    return templates.TemplateResponse(
        request=request,
        name="booking/override_form.html",
        context={
            "action_url": lang_url(base),
            "revert_url": lang_url(f"{base}/revert") if existing is not None else None,
            "back_url": lang_url("/history"),
            "target_slot": effective_slot_for(
                rule,
                target_date,
                form_values["class_time"]
                if _HHMM.match(form_values["class_time"])
                else str(rule.class_time),
            ),
            "target_date_label": target_date.strftime("%a %d %b"),
            "form_values": form_values,
            "errors": errors,
            "rule_class_type": str(rule.class_type),
            "rule_class_time": str(rule.class_time),
            "second_shot_class_type": rule.second_shot_class_type,
            "second_shot_class_time": rule.second_shot_class_time,
            "picker_class_types": class_types,
            "picker_time_slots": time_slots,
            "probe_notice": notice,
            "is_skipped": existing is not None and bool(existing.skip_day),
            # A skip has no combination, so "not validated" would be a
            # warning about nothing.
            "not_validated": (
                existing is not None
                and not bool(existing.skip_day)
                and not bool(existing.validated)
            ),
            "csrf_token": get_csrf_token(request) or "",
        },
        status_code=status_code,
    )


__all__ = ["router"]
