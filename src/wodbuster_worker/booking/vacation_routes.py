"""Vacation-mode routes (US7.3, US7.4).

Three routes under ``/vacation``:

- ``GET  /vacation``               list open windows + enable form.
- ``POST /vacation``                enable a new window (bulk cancels).
- ``POST /vacation/{id}/close``     end a window early.

Both mutating routes are CSRF-protected and auth-gated.

Design note: the create form takes ``start_date`` and ``end_date`` as
``YYYY-MM-DD`` strings (native ``<input type="date">``). They are
interpreted as calendar days in the operator's timezone and
persisted as ``[00:00, 23:59:59.999999]`` UTC after the timezone
conversion happens inside :func:`vacation.enable`. That keeps
"holiday from Mon through Wed" natural for the operator: the
booking on Wed at 21:30 local is covered.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..auth.csrf import get_csrf_token, verify_csrf
from ..auth.deps import require_session
from ..i18n import lang_url, t
from ..persistence.engine import get_session
from ..scheduler.rule_jobs import operator_timezone
from . import vacation as vacation_service
from .vacation import VacationNotFoundError, VacationRangeError

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/vacation", tags=["vacation"])


def _templates(request: Request) -> Jinja2Templates:
    templates = getattr(request.app.state, "templates", None)
    if templates is None:  # pragma: no cover - misconfiguration
        raise RuntimeError("app.state.templates is not configured; wire it in lifespan().")
    assert isinstance(templates, Jinja2Templates)
    return templates


@router.get("", name="vacation_list")
def vacation_list(
    request: Request,
    operator_id: int = Depends(require_session),
) -> Response:
    """Render open windows + the enable form."""
    templates = _templates(request)
    tz = operator_timezone()
    with get_session() as session:
        windows = vacation_service.list_open(session, operator_id)
        rows = [_window_to_row(w, tz) for w in windows]
    return templates.TemplateResponse(
        request=request,
        name="vacation.html",
        context={
            "windows": rows,
            "flash": request.query_params.get("flash"),
            "flash_kind": request.query_params.get("flash_kind", "info"),
            "csrf_token": get_csrf_token(request) or "",
        },
    )


@router.post(
    "",
    name="vacation_enable",
    dependencies=[Depends(verify_csrf)],
)
def vacation_enable(
    request: Request,
    start_date: str = Form(...),
    end_date: str = Form(...),
    operator_id: int = Depends(require_session),
) -> Response:
    """Open a new vacation window and bulk-cancel granted bookings."""
    client = getattr(request.app.state, "wodbuster_client", None)
    store = getattr(request.app.state, "cookie_store", None)
    if client is None or store is None:
        return _redirect_with_flash(
            t("flash.booking.service_unavailable"),
            kind="error",
        )

    tz = operator_timezone()
    try:
        start_dt = _parse_date_input(start_date, tz)
        end_dt = _parse_date_input(end_date, tz)
    except ValueError:
        return _redirect_with_flash(
            t("flash.vacation.invalid_date"),
            kind="error",
        )

    with get_session() as session:
        try:
            vacation_service.enable(
                session,
                operator_id=operator_id,
                start_date=start_dt,
                end_date=end_dt,
                client=client,
                cookie_store=store,
            )
        except VacationRangeError as exc:
            return _redirect_with_flash(str(exc), kind="error")
        session.commit()

    return _redirect_with_flash(
        t("flash.vacation.enabled", start=start_date, end=end_date),
        kind="info",
    )


@router.post(
    "/{window_id}/close",
    name="vacation_close",
    dependencies=[Depends(verify_csrf)],
)
def vacation_close(
    window_id: int,
    request: Request,
    operator_id: int = Depends(require_session),
) -> Response:
    """End an open vacation window early."""
    _ = request
    with get_session() as session:
        try:
            vacation_service.close_early(
                session,
                operator_id=operator_id,
                window_id=window_id,
            )
        except VacationNotFoundError:
            raise HTTPException(status_code=404) from None
        session.commit()
    return _redirect_with_flash(
        t("flash.vacation.closed"),
        kind="info",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redirect_with_flash(message: str, *, kind: str) -> RedirectResponse:
    query = urlencode({"flash": message, "flash_kind": kind})
    return RedirectResponse(url=f"{lang_url('/vacation')}?{query}", status_code=303)


def _parse_date_input(raw: str, tz: ZoneInfo) -> datetime:
    """Parse a ``YYYY-MM-DD`` field into a timezone-aware datetime.

    Anchors on midnight in the operator's zone so the service layer
    can floor/ceil consistently. Empty strings are rejected up-front
    because HTML form validation lets them through as empty.
    """
    if not raw:
        raise ValueError("empty date")
    parsed = datetime.strptime(raw, "%Y-%m-%d")
    return datetime.combine(parsed.date(), time.min, tzinfo=tz)


def _window_to_row(window: Any, tz: ZoneInfo) -> dict[str, Any]:
    """View-model for a single vacation row."""
    start_local = window.start_date.astimezone(tz)
    end_local = window.end_date.astimezone(tz)
    now = datetime.now(tz=UTC)
    return {
        "id": int(window.id),
        "start_label": start_local.strftime("%a %d %b %Y"),
        "end_label": end_local.strftime("%a %d %b %Y"),
        "start_dt": start_local,
        "end_dt": end_local,
        "start_iso": start_local.date().isoformat(),
        "end_iso": end_local.date().isoformat(),
        "active_now": window.start_date <= now <= window.end_date,
    }


__all__ = ["router"]
