"""Static content routes (FAQ + about surfaces).

Kept in its own router so the app.py wiring stays tidy: content
pages have no state, no CSRF, no session dependency (they are safe
to serve to anonymous callers too, though today they render inside
the authed shell for consistency).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from ..auth.csrf import get_csrf_token
from ..auth.deps import require_session

router = APIRouter(tags=["static"])


def _templates(request: Request) -> Jinja2Templates:
    templates = getattr(request.app.state, "templates", None)
    if templates is None:  # pragma: no cover - misconfiguration
        raise RuntimeError("app.state.templates not configured")
    assert isinstance(templates, Jinja2Templates)
    return templates


@router.get("/faq", name="faq")
def faq(
    request: Request,
    operator_id: int = Depends(require_session),
) -> Response:
    """Render the frequently-asked-questions page."""
    _ = operator_id  # gate by session but no per-operator data
    templates = _templates(request)
    return templates.TemplateResponse(
        request=request,
        name="faq.html",
        context={
            "csrf_token": get_csrf_token(request) or "",
        },
    )


__all__ = ["router"]
