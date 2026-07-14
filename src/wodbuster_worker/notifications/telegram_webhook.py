"""Telegram webhook + operator-facing binding page (US9.8, US9.9).

Two routes live here:

- ``GET  /telegram``                    operator-facing page: shows
  current bind status and a "Generate link" button that mints a
  one-shot token and renders the ``t.me/<bot>?start=<token>``
  deep-link the operator clicks to DM the bot.
- ``POST /telegram/webhook/{secret}``   Telegram Bot API webhook.
  ``secret`` is a Key Vault-sourced path component (US9.9); a
  mismatch returns 404 so Telegram is the only party that can
  reach the handler. The only command implemented today is
  ``/start <token>`` — future slices (US6.3 ``/cancel``, US8.3
  ``/bookclass``) plug in here.

Auth model:

- The ``GET /telegram`` route is session-gated like every other
  UI page (``require_session``).
- The webhook route is intentionally NOT session-gated (Telegram
  never carries the session cookie); the path-secret gates it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..auth.csrf import get_csrf_token, verify_csrf
from ..auth.deps import require_session
from ..i18n import t
from ..persistence.engine import get_session
from ..persistence.models import OperatorProfile
from . import telegram as telegram_sender
from .telegram_bind import TelegramBindStore

_log = structlog.get_logger(__name__)

router = APIRouter(tags=["telegram"])


def _templates(request: Request) -> Jinja2Templates:
    templates = getattr(request.app.state, "templates", None)
    if templates is None:  # pragma: no cover - misconfiguration
        raise RuntimeError("app.state.templates not configured")
    assert isinstance(templates, Jinja2Templates)
    return templates


def _bind_store(request: Request) -> TelegramBindStore:
    store = getattr(request.app.state, "telegram_bind_store", None)
    if store is None:
        # Lazy default so tests that spin the app without pre-seeding
        # the state still work; production creates it in the lifespan.
        store = TelegramBindStore()
        request.app.state.telegram_bind_store = store
    assert isinstance(store, TelegramBindStore)
    return store


def _bot_username(request: Request) -> str | None:
    return getattr(request.app.state, "telegram_bot_username", None)


def _resolve_operator(session: Any, operator_id: int) -> OperatorProfile | None:
    result: OperatorProfile | None = session.get(OperatorProfile, operator_id)
    return result


# ---------------------------------------------------------------------------
# GET /telegram — status + generate bind link
# ---------------------------------------------------------------------------


@router.get("/telegram", name="telegram_page")
def telegram_page(
    request: Request,
    operator_id: int = Depends(require_session),
    flash: str | None = None,
    flash_kind: str = "info",
) -> Response:
    templates = _templates(request)
    with get_session() as session:
        operator = _resolve_operator(session, operator_id)
    bot_username = _bot_username(request)
    return templates.TemplateResponse(
        request=request,
        name="telegram.html",
        context={
            "chat_id": operator.telegram_chat_id if operator else None,
            "bot_username": bot_username,
            "deep_link": None,  # populated by POST after generating
            "token": None,
            "csrf_token": get_csrf_token(request) or "",
            "flash": flash,
            "flash_kind": flash_kind if flash_kind in {"info", "warning", "error"} else "info",
        },
    )


@router.post(
    "/telegram/generate",
    name="telegram_generate",
    dependencies=[Depends(verify_csrf)],
)
def telegram_generate_link(
    request: Request,
    operator_id: int = Depends(require_session),
) -> Response:
    """Mint a fresh bind token and re-render the page with the deep link."""
    templates = _templates(request)
    store = _bind_store(request)
    token = store.issue(operator_id)
    bot_username = _bot_username(request)
    deep_link = _build_deep_link(bot_username, token)
    with get_session() as session:
        operator = _resolve_operator(session, operator_id)
    return templates.TemplateResponse(
        request=request,
        name="telegram.html",
        context={
            "chat_id": operator.telegram_chat_id if operator else None,
            "bot_username": bot_username,
            "deep_link": deep_link,
            "token": token,
            "csrf_token": get_csrf_token(request) or "",
            "flash": None,
            "flash_kind": "info",
        },
    )


@router.post(
    "/telegram/unbind",
    name="telegram_unbind",
    dependencies=[Depends(verify_csrf)],
)
def telegram_unbind(
    request: Request,
    operator_id: int = Depends(require_session),
) -> Response:
    """Clear ``telegram_chat_id`` for the current operator."""
    with get_session() as session:
        operator = _resolve_operator(session, operator_id)
        if operator is not None and operator.telegram_chat_id is not None:
            operator.telegram_chat_id = None
            session.commit()
    return RedirectResponse(
        url="/telegram?" + urlencode({"flash": t("flash.telegram.unbound"), "flash_kind": "info"}),
        status_code=303,
    )


@router.post(
    "/telegram/test",
    name="telegram_test",
    dependencies=[Depends(verify_csrf)],
)
def telegram_test(
    request: Request,
    operator_id: int = Depends(require_session),
) -> Response:
    """Send a smoke-test message straight to the operator's bound chat.

    Bypasses the outbox/dispatcher entirely so a bound operator can
    confirm the outbound path (bot token + chat id + network) end-to-
    end in one click. Failures redirect back with a flash error so the
    operator sees the reason instead of a stack trace.
    """
    bot_token = getattr(request.app.state, "telegram_bot_token", None)
    if not bot_token:
        return _redirect_flash(
            t("flash.telegram.no_token"),
            kind="error",
        )
    with get_session() as session:
        operator = _resolve_operator(session, operator_id)
    chat_id = operator.telegram_chat_id if operator else None
    if not chat_id:
        return _redirect_flash(
            t("flash.telegram.not_bound"),
            kind="warning",
        )
    try:
        telegram_sender.send_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text=(
                "🧪 Test message from WodBuster Booking Scheduler. "
                "If you see this, notifications are working."
            ),
        )
    except telegram_sender.PermanentTelegramError as exc:
        _log.warning("telegram.test.permanent_error", error=str(exc))
        return _redirect_flash(
            t("flash.telegram.permanent_error", reason=str(exc)),
            kind="error",
        )
    except telegram_sender.TransientTelegramError as exc:
        _log.warning("telegram.test.transient_error", error=str(exc))
        return _redirect_flash(
            t("flash.telegram.transient_error", reason=str(exc)),
            kind="warning",
        )
    return _redirect_flash(
        t("flash.telegram.test_sent"),
        kind="info",
    )


def _redirect_flash(message: str, *, kind: str) -> RedirectResponse:
    query = urlencode({"flash": message, "flash_kind": kind})
    return RedirectResponse(url=f"/telegram?{query}", status_code=303)


def _build_deep_link(bot_username: str | None, token: str) -> str | None:
    if not bot_username:
        return None
    return f"https://t.me/{quote(bot_username)}?start={quote(token)}"


# ---------------------------------------------------------------------------
# POST /telegram/webhook/{secret} — bot updates from Telegram
# ---------------------------------------------------------------------------


@router.post("/telegram/webhook/{secret}", name="telegram_webhook")
async def telegram_webhook(
    secret: str,
    request: Request,
) -> dict[str, Any]:
    """Bot API webhook endpoint.

    Returns a JSON envelope Telegram accepts; every reply is sent
    via a separate ``sendMessage`` call because the webhook return
    payload has a low size limit and mixing reply methods here made
    the handler brittle.

    Security: the ``{secret}`` path segment is compared against the
    Key Vault-sourced ``telegram-webhook-secret``. Mismatch → 404
    so a scanner cannot even confirm the URL exists.
    """
    expected = getattr(request.app.state, "telegram_webhook_secret", None)
    if not expected or secret != expected:
        raise HTTPException(status_code=404)

    payload = await request.json()
    message = payload.get("message") or payload.get("edited_message")
    if not message:
        # Not a message update (edited channel post, callback query,
        # etc.); acknowledge silently.
        return {"ok": True}

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    if not chat_id or not text:
        return {"ok": True}

    reply = _handle_command(request, chat_id=str(chat_id), text=text)
    if reply:
        _send_reply(request, chat_id=str(chat_id), text=reply)
    return {"ok": True}


def _handle_command(request: Request, *, chat_id: str, text: str) -> str | None:
    """Dispatch on ``text``. Returns the reply body or ``None``."""
    parts = text.split(maxsplit=1)
    command = parts[0].lower()
    argument = parts[1] if len(parts) > 1 else ""

    if command == "/start":
        return _handle_start(request, chat_id=chat_id, token=argument)
    if command in {"/help", "/status"}:
        return _render_status(request, chat_id=chat_id)
    # Any other command: helpful nudge, no state change.
    return (
        "Unknown command. Send /help for a status check, or /start "
        "<token> with the token from the web UI to bind this chat."
    )


def _handle_start(request: Request, *, chat_id: str, token: str) -> str:
    if not token:
        return (
            "Missing token. Open the web UI (Telegram page) and click "
            "'Generate link' to get a one-shot binding URL."
        )
    store = _bind_store(request)
    operator_id = store.consume(token)
    if operator_id is None:
        return (
            "Token invalid or expired. Open the web UI (Telegram page) "
            "and generate a fresh link — tokens live 10 minutes and "
            "can only be used once."
        )
    with get_session() as session:
        operator = session.get(OperatorProfile, operator_id)
        if operator is None:
            return "Operator profile not found. Contact the deployment owner."
        operator.telegram_chat_id = chat_id
        session.commit()
    _log.info(
        "telegram.bind.ok",
        operator_id=operator_id,
        chat_id=chat_id,
    )
    return (
        "Bound. This chat will now receive booking outcomes, "
        "cookie-expiring warnings, and anomaly alerts."
    )


def _render_status(request: Request, *, chat_id: str) -> str:
    with get_session() as session:
        row = session.execute(
            select(OperatorProfile).where(OperatorProfile.telegram_chat_id == chat_id)
        ).scalar_one_or_none()
    if row is None:
        return (
            "This chat is not bound. Open the web UI (Telegram page) "
            "and click 'Generate link' to bind."
        )
    return (
        f"Bound to operator {row.display_name or f'#{row.id}'}. "
        "You will receive booking outcomes and alerts here."
    )


def _send_reply(request: Request, *, chat_id: str, text: str) -> None:
    """Fire-and-forget ``sendMessage``. Failures are logged only."""
    bot_token = getattr(request.app.state, "telegram_bot_token", None)
    if not bot_token:
        _log.warning("telegram.reply.no_bot_token")
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        with httpx.Client(timeout=5.0) as client:
            client.post(url, json={"chat_id": chat_id, "text": text})
    except httpx.HTTPError as exc:
        _log.warning("telegram.reply.transport_error", error=str(exc))


__all__ = ["router"]
