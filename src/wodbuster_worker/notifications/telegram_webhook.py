"""Telegram webhook + operator-facing binding page (US9.8, US9.9).

Two routes live here:

- ``GET  /telegram``                    operator-facing page: shows
  current bind status and a "Generate link" button that mints a
  one-shot token and renders the ``t.me/<bot>?start=<token>``
  deep-link the operator clicks to DM the bot.
- ``POST /telegram/webhook/{secret}``   Telegram Bot API webhook.
  ``secret`` is a Key Vault-sourced path component (US9.9); a
  mismatch returns 404 so Telegram is the only party that can
  reach the handler.

Command dispatcher (TG.2):

The webhook routes on an explicit allow-list of commands. Anything
outside the list is either an explanatory rejection (rule-mutation
verbs, which are web-UI-only per US5.6 / CC-009) or a polite unknown-
command nudge. Recognised commands:

- ``/start <token>``  bind this chat to the operator (US9.8).
- ``/help``           list the supported commands (TG.4).
- ``/status``         report bind status.
- ``/next``           next scheduled booking + upcoming slots, with
  the booking id for already-granted slots (TG.3).
- ``/last``           most recent booking outcome (TG.3).
- ``/cancel <id>``    idempotent cancel of a booking (US6.3, CC-015).
- ``/ack``            acknowledge the open cookie-expiring alert (TG.5).

Every stateful command (``/next``, ``/last``, ``/cancel``, ``/ack``)
requires the chat to be bound to an operator (FR-031); unbound chats
get a no-data-leak rejection, never another operator's data.

Auth model:

- The ``GET /telegram`` route is session-gated like every other
  UI page (``require_session``).
- The webhook route is intentionally NOT session-gated (Telegram
  never carries the session cookie); the path-secret gates it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth.csrf import get_csrf_token, verify_csrf
from ..auth.deps import require_session
from ..booking.cancellation import (
    BookingAlreadyCancelledError,
    BookingNotFoundError,
    CancellationUpstreamError,
    cancel_booking,
    list_recent_bookings,
    resolve_owner_gym_account,
)
from ..booking.upcoming import list_upcoming_slots
from ..gyms.service import gym_client_factory, resolve_gym_client
from ..heartbeat.alerts import acknowledge_open_cookie_expiring
from ..heartbeat.next_window import compute_next_booking
from ..i18n import get_language, lang_url, set_language, t
from ..persistence.engine import get_session
from ..persistence.gym_accounts import list_user_gym_accounts
from ..persistence.models import OperatorProfile
from . import telegram as telegram_sender
from .messages import format_slot
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
        url=f"{lang_url('/telegram')}?"
        + urlencode({"flash": t("flash.telegram.unbound"), "flash_kind": "info"}),
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
    return RedirectResponse(url=f"{lang_url('/telegram')}?{query}", status_code=303)


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
    # Replies render in the bound operator's language (ADR-0008).
    set_language(_reply_language(chat_id))
    parts = text.split(maxsplit=1)
    command = parts[0].lower()
    argument = parts[1] if len(parts) > 1 else ""

    route = _route(command)
    if route == "start":
        return _handle_start(request, chat_id=chat_id, token=argument)
    if route == "help":
        return _handle_help()
    if route == "status":
        return _render_status(request, chat_id=chat_id)
    if route == "next":
        return _handle_next(request, chat_id=chat_id)
    if route == "last":
        return _handle_last(request, chat_id=chat_id)
    if route == "cancel":
        return _handle_cancel(request, chat_id=chat_id, argument=argument)
    if route == "ack":
        return _handle_ack(request, chat_id=chat_id)
    if route == "rule_mutation":
        # US5.6 / CC-009: rule create/update/delete is web-UI only.
        # Reject with an explanation and change no state.
        return t("tg.cmd.rule_mutation")
    # Any other command: helpful nudge, no state change.
    return t("tg.cmd.unknown")


def _route(command: str) -> str:
    """Classify ``command`` into a dispatch label (TG.2 allow-list).

    Pure and dependency-free so the routing table can be unit-tested
    without a request or a database (TG.T1). Every supported verb maps
    to its own label; rule-mutation verbs map to ``rule_mutation`` so
    the dispatcher can reject them with an explanation (CC-009); every
    other token is ``unknown``.
    """
    supported = {
        "/start": "start",
        "/help": "help",
        "/status": "status",
        "/next": "next",
        "/last": "last",
        "/cancel": "cancel",
        "/ack": "ack",
    }
    if command in supported:
        return supported[command]
    if command in _RULE_MUTATION_COMMANDS:
        return "rule_mutation"
    return "unknown"


# Rule-mutation verbs are web-UI only (US5.6 / CC-009). Recognising
# them explicitly lets the dispatcher explain *why* they are refused
# instead of falling through to the generic unknown-command nudge.
_RULE_MUTATION_COMMANDS = frozenset(
    {
        "/newrule",
        "/addrule",
        "/createrule",
        "/editrule",
        "/updaterule",
        "/setrule",
        "/deleterule",
        "/delrule",
        "/removerule",
        "/rmrule",
        "/rule",
        "/rules",
    }
)

# Shared no-data-leak rejection for stateful commands on an unbound
# chat (FR-031): never surface another operator's data or confirm a
# chat's binding state beyond "not bound". Rendered via ``t("tg.cmd.unbound")``
# in the recipient's language at call time.


def _operator_for_chat(session: Session, chat_id: str) -> OperatorProfile | None:
    """Resolve the operator bound to ``chat_id`` (or ``None``).

    Central bound-chat lookup shared by every stateful handler so the
    ``telegram_chat_id`` scoping (FR-031) lives in one place.
    """
    return session.execute(
        select(OperatorProfile).where(OperatorProfile.telegram_chat_id == chat_id)
    ).scalar_one_or_none()


def _reply_language(chat_id: str) -> str:
    """Language for replies to ``chat_id``: the bound operator's, else ``en``."""
    with get_session() as session:
        operator = _operator_for_chat(session, chat_id)
        return (operator.communication_language if operator else None) or "en"


def _handle_help() -> str:
    """TG.4: list the supported commands."""
    return t("tg.cmd.help")


def _handle_start(request: Request, *, chat_id: str, token: str) -> str:
    if not token:
        return t("tg.cmd.start.missing_token")
    store = _bind_store(request)
    operator_id = store.consume(token)
    if operator_id is None:
        return t("tg.cmd.start.invalid_token")
    with get_session() as session:
        operator = session.get(OperatorProfile, operator_id)
        if operator is None:
            return t("tg.cmd.start.no_operator")
        operator.telegram_chat_id = chat_id
        # Now that the chat is bound, answer in the operator's language.
        set_language(operator.communication_language or "en")
        session.commit()
    _log.info(
        "telegram.bind.ok",
        operator_id=operator_id,
        chat_id=chat_id,
    )
    return t("tg.cmd.start.bound")


def _render_status(request: Request, *, chat_id: str) -> str:
    with get_session() as session:
        row = _operator_for_chat(session, chat_id)
    if row is None:
        return t("tg.cmd.status.unbound")
    return t("tg.cmd.status.bound", operator=row.display_name or f"#{row.id}")


def _next_section_lines(next_booking: Any, upcoming: Any) -> list[str]:
    """Render one gym's next-booking + upcoming-slots block (may be empty)."""
    lang = get_language()
    lines: list[str] = []
    if next_booking is not None:
        lines.append(
            t(
                "tg.cmd.next.line",
                slot=format_slot(next_booking.target_slot, lang),
                opens=format_slot(next_booking.window_open, lang),
            )
        )
    if upcoming:
        lines.append(t("tg.cmd.next.upcoming_header"))
        for slot in upcoming[:5]:
            when = format_slot(slot.target_slot, lang)
            if slot.kind == "granted":
                # Granted slots are cancellable; surface the id /cancel needs.
                lines.append(
                    t(
                        "tg.cmd.next.slot_granted",
                        id=slot.booking_id,
                        when=when,
                        klass=slot.target_class,
                    )
                )
            else:
                lines.append(t("tg.cmd.next.slot_scheduled", when=when, klass=slot.target_class))
    return lines


def _handle_next(request: Request, *, chat_id: str) -> str:
    """TG.3: report the next scheduled booking and upcoming slots.

    Aggregates across every gym the operator owns; a multi-gym operator
    sees each gym labelled, since Telegram has no gym switcher.
    """
    now = datetime.now(tz=UTC)
    empty = t("tg.cmd.next.empty")
    with get_session() as session:
        operator = _operator_for_chat(session, chat_id)
        if operator is None:
            return t("tg.cmd.unbound")
        gyms = list_user_gym_accounts(session, operator.id)
        if not gyms:
            return empty
        multi = len(gyms) > 1
        blocks: list[str] = []
        any_content = False
        for gym in gyms:
            next_booking = compute_next_booking(session, gym.id, now)
            upcoming = list_upcoming_slots(session, gym.id, now=now)
            lines = _next_section_lines(next_booking, upcoming)
            if lines:
                any_content = True
                body = "\n".join(lines)
            else:
                body = empty
            blocks.append(f"[{gym.display_name}]\n{body}" if multi else body)
    if not any_content:
        return empty
    return "\n\n".join(blocks)


def _handle_last(request: Request, *, chat_id: str) -> str:
    """TG.3: report the most recent booking outcome, per gym."""
    empty = t("tg.cmd.last.empty")
    with get_session() as session:
        operator = _operator_for_chat(session, chat_id)
        if operator is None:
            return t("tg.cmd.unbound")
        gyms = list_user_gym_accounts(session, operator.id)
        if not gyms:
            return empty
        multi = len(gyms) > 1
        blocks: list[str] = []
        any_content = False
        for gym in gyms:
            recent = list_recent_bookings(session, gym.id, limit=1)
            if recent:
                any_content = True
                last = recent[0]
                lang = get_language()
                body = t(
                    "tg.cmd.last.line",
                    id=last.id,
                    klass=last.target_class,
                    when=format_slot(last.target_slot, lang),
                    status=last.terminal_status,
                    attempted=format_slot(last.attempted_at, lang),
                )
            else:
                body = t("tg.cmd.last.none")
            blocks.append(f"[{gym.display_name}]\n{body}" if multi else body)
    if not any_content:
        return empty
    return "\n\n".join(blocks)


def _handle_cancel(request: Request, *, chat_id: str, argument: str) -> str:
    """US6.3 / CC-015: idempotent cancel of a booking by id."""
    booking_id_text = argument.strip()
    if not booking_id_text:
        return t("tg.cmd.cancel.usage")
    try:
        booking_id = int(booking_id_text)
    except ValueError:
        return t("tg.cmd.cancel.nan")

    factory = gym_client_factory(request.app.state)
    cookie_store = getattr(request.app.state, "cookie_store", None)
    if factory is None or cookie_store is None:
        return t("tg.cmd.cancel.unavailable")

    with get_session() as session:
        operator = _operator_for_chat(session, chat_id)
        if operator is None:
            return t("tg.cmd.unbound")
        gym_account_id = resolve_owner_gym_account(
            session, user_id=operator.id, booking_id=booking_id
        )
        if gym_account_id is None:
            return t("tg.cmd.cancel.not_found", id=booking_id)
        resolved = resolve_gym_client(factory, session, gym_account_id)
        if resolved is None:
            return t("tg.cmd.cancel.not_found", id=booking_id)
        client, _idu = resolved
        try:
            outcome = cancel_booking(
                session,
                gym_account_id=gym_account_id,
                booking_id=booking_id,
                client=client,
                cookie_store=cookie_store,
            )
        except BookingNotFoundError:
            return t("tg.cmd.cancel.not_found", id=booking_id)
        except BookingAlreadyCancelledError:
            # CC-015: idempotent — no WodBuster call was issued.
            return t("tg.cmd.cancel.already", id=booking_id)
        except CancellationUpstreamError:
            return t("tg.cmd.cancel.upstream", id=booking_id)
        # Capture display values before commit expires the attributes.
        target_class = outcome.target_class
        when = format_slot(outcome.target_slot, get_language())
        session.commit()
    return t("tg.cmd.cancel.ok", id=booking_id, klass=target_class, when=when)


def _handle_ack(request: Request, *, chat_id: str) -> str:
    """TG.5: acknowledge the open cookie-expiring alert."""
    now = datetime.now(tz=UTC)
    with get_session() as session:
        operator = _operator_for_chat(session, chat_id)
        if operator is None:
            return t("tg.cmd.unbound")
        alert_id = acknowledge_open_cookie_expiring(session, operator.id, now=now)
        if alert_id is None:
            return t("tg.cmd.ack.none")
        session.commit()
    return t("tg.cmd.ack.ok")


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
