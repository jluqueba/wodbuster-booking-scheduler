"""Scheduler rule CRUD routes (US5.2-5.5).

Five routes, all under ``/rules``:

- ``GET /rules`` — list the operator's rules.
- ``GET /rules/new`` — render an empty form.
- ``POST /rules`` — create a rule. On validation failure re-renders
  the form with error banners; on success redirects to the list.
- ``GET /rules/{id}`` — render the edit form for one rule.
- ``POST /rules/{id}`` — update the rule; same success / failure
  branching as create.
- ``POST /rules/{id}/delete`` — delete the rule.

All routes are auth-gated (:func:`require_session`) and every mutating
route runs the CSRF check.

Ownership: routes touching a specific rule call
:func:`get_rule_for_operator`. Any miss returns 404 rather than 403 so
we do not confirm existence to an unauthorized caller (CC-012).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..auth.csrf import get_csrf_token, verify_csrf
from ..auth.deps import require_session
from ..persistence.cookie_store import CookieStore
from ..persistence.engine import get_session
from ..wodbuster_client.client import (
    WodBusterAuthError,
    WodBusterClient,
    WodBusterProtocolError,
    WodBusterTransportError,
)
from .forms import parse_rule_form
from .schema_debug import summarize_shape
from .service import (
    create_rule,
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


def _templates(request: Request) -> Jinja2Templates:
    templates = getattr(request.app.state, "templates", None)
    if templates is None:  # pragma: no cover - misconfiguration
        raise RuntimeError(
            "app.state.templates is not configured; wire it in lifespan()."
        )
    assert isinstance(templates, Jinja2Templates)
    return templates


def _render_form(
    request: Request,
    *,
    heading: str,
    action_url: str,
    form_values: dict[str, str],
    errors: dict[str, str],
    delete_url: str | None = None,
    status_code: int = 200,
) -> Response:
    """Render the rule form (create or edit)."""
    templates = _templates(request)
    return templates.TemplateResponse(
        request=request,
        name="rules/form.html",
        context={
            "heading": heading,
            "action_url": action_url,
            "form_values": form_values,
            "errors": errors,
            "delete_url": delete_url,
            "day_labels": _DAY_LABELS,
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
        # Materialise the render context inside the session so lazy
        # attribute loads on ``preferences`` succeed even after the
        # session closes on `with` exit.
        rows = [
            {
                "id": rule.id,
                "day_label": _DAY_LABELS[rule.day_of_week],
                "window_offset_hours": rule.window_offset_hours,
                "active": rule.active,
                "preferences": [
                    {
                        "class_type": p.class_type,
                        "target_time_slot": p.target_time_slot,
                    }
                    for p in rule.preferences
                ],
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
    """Render an empty rule form."""
    _ = operator_id  # auth-only; nothing operator-scoped to load
    return _render_form(
        request,
        heading="Create rule",
        action_url="/rules",
        form_values={},
        errors={},
    )


@router.post("", name="rules_create", dependencies=[Depends(verify_csrf)])
async def rules_create(
    request: Request, operator_id: int = Depends(require_session)
) -> Response:
    """Persist a new rule (or re-render the form on validation failure)."""
    form_data = dict(await request.form())
    parsed = parse_rule_form(_str_only(form_data))

    if not parsed.is_valid:
        return _render_form(
            request,
            heading="Create rule",
            action_url="/rules",
            form_values=_str_only(form_data),
            errors=parsed.errors,
            status_code=422,
        )

    assert parsed.day_of_week is not None
    assert parsed.window_offset_hours is not None
    with get_session() as session:
        create_rule(
            session,
            operator_id=operator_id,
            day_of_week=parsed.day_of_week,
            window_offset_hours=parsed.window_offset_hours,
            preferences=parsed.preferences,
        )

    return RedirectResponse(url="/rules", status_code=303)


@router.get("/api/schema-debug", name="rules_api_schema_debug")
def rules_api_schema_debug(
    request: Request, operator_id: int = Depends(require_session)
) -> Response:
    """Return a PII-redacted shape summary of ``LoadClass.ashx``.

    Temporary endpoint used to discover the class-type and time-slot
    field names for the rules form uplift. Delete once
    ``GET /rules/api/classes`` lands in the follow-up PR.

    Requires the cookie stack to be wired (``/cookie`` path already
    503s the same way when it is not). Returns:

    - ``top_level_keys``: keys of the top-level response object.
    - ``top_level_summary``: shape summary two levels deep with
      ``AtletasEntrenando``-style PII fields redacted.

    Route lives at ``/rules/api/schema-debug`` — registered above
    ``/{rule_id}`` so FastAPI does not try to parse ``api`` as int.
    """
    store = getattr(request.app.state, "cookie_store", None)
    client = getattr(request.app.state, "wodbuster_client", None)
    if store is None or client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "cookie store or WodBuster client not configured; "
                "set WODBUSTER_GYM, WODBUSTER_IDU and the cookie key."
            ),
        )
    assert isinstance(store, CookieStore)
    assert isinstance(client, WodBusterClient)

    with get_session() as session:
        cookie_value = store.load(session, operator_id)
    if cookie_value is None:
        raise HTTPException(
            status_code=409,
            detail="no cookie on file; paste one at /cookie first.",
        )

    ticks = _today_ticks_utc()
    try:
        loaded = client.load_class(cookie_value, ticks)
    except WodBusterAuthError as exc:
        raise HTTPException(status_code=502, detail=f"auth: {exc}") from exc
    except WodBusterTransportError as exc:
        raise HTTPException(status_code=502, detail=f"transport: {exc}") from exc
    except WodBusterProtocolError as exc:
        raise HTTPException(status_code=502, detail=f"protocol: {exc}") from exc

    return JSONResponse(
        {
            "ticks": ticks,
            "top_level_keys": sorted(loaded.payload.keys()),
            "top_level_summary": summarize_shape(loaded.payload),
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

    return _render_form(
        request,
        heading="Edit rule",
        action_url=f"/rules/{rule_id}",
        form_values=form_values,
        errors={},
        delete_url=f"/rules/{rule_id}/delete",
    )


@router.post("/{rule_id}", name="rules_update", dependencies=[Depends(verify_csrf)])
async def rules_update(
    rule_id: int,
    request: Request,
    operator_id: int = Depends(require_session),
) -> Response:
    """Update a rule owned by the operator."""
    form_data = dict(await request.form())
    parsed = parse_rule_form(_str_only(form_data))

    with get_session() as session:
        rule = get_rule_for_operator(session, operator_id, rule_id)
        if rule is None:
            # Silently 404 non-owned rules even after form parse so
            # validation timing does not leak existence.
            raise HTTPException(status_code=404)

        if not parsed.is_valid:
            return _render_form(
                request,
                heading="Edit rule",
                action_url=f"/rules/{rule_id}",
                form_values=_str_only(form_data),
                errors=parsed.errors,
                delete_url=f"/rules/{rule_id}/delete",
                status_code=422,
            )

        assert parsed.day_of_week is not None
        assert parsed.window_offset_hours is not None
        update_rule(
            session,
            rule,
            day_of_week=parsed.day_of_week,
            window_offset_hours=parsed.window_offset_hours,
            preferences=parsed.preferences,
        )

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

    return RedirectResponse(url="/rules", status_code=303)


def _today_ticks_utc() -> int:
    """Unix timestamp of today at 00:00 UTC (matches Phase 0's ticks)."""
    now = datetime.now(tz=UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def _rule_to_form_values(rule) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """Convert a loaded rule into the flat form-values dict.

    The template renders every field from this dict so the create and
    edit flows use the same partial.
    """
    values: dict[str, str] = {
        "day_of_week": str(rule.day_of_week),
        "window_offset_hours": str(rule.window_offset_hours),
    }
    for pref in rule.preferences:
        values[f"preference_{pref.order_index}_class_type"] = pref.class_type
        values[f"preference_{pref.order_index}_time_slot"] = pref.target_time_slot
    return values


def _str_only(form_data: Mapping[str, object]) -> dict[str, str]:
    """Coerce Starlette FormData into a plain ``str -> str`` dict.

    ``FormData.get`` may return :class:`UploadFile` for file inputs;
    the rule form only has text inputs but the type-narrowing keeps
    the parser's contract explicit. Accepts a ``Mapping`` so the
    caller can pass a ``dict[str, UploadFile | str]`` from
    ``request.form()`` without an invariance error.
    """
    return {k: v for k, v in form_data.items() if isinstance(v, str)}


__all__ = ["router"]
