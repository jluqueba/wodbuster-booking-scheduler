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
- ``/bookclass <YYYY-MM-DD> <HH:MM>``  one-off manual booking (US8.3).

Every stateful command (``/next``, ``/last``, ``/cancel``, ``/ack``,
``/bookclass``) requires the chat to be bound to an operator (FR-031);
unbound chats get a no-data-leak rejection, never another operator's
data.

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
)
from ..booking.manual import (
    BookingWindowClosedError,
    ClassNotVisibleError,
    ManualBookingService,
    ManualBookingUpstreamError,
    NoCookieError,
)
from ..booking.upcoming import list_upcoming_slots
from ..heartbeat.alerts import acknowledge_open_cookie_expiring
from ..heartbeat.next_window import compute_next_booking
from ..i18n import lang_url, t
from ..persistence.engine import get_session
from ..persistence.models import OperatorProfile
from ..scheduler.rule_jobs import operator_timezone
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
    if route == "bookclass":
        return _handle_bookclass(request, chat_id=chat_id, argument=argument)
    if route == "rule_mutation":
        # US5.6 / CC-009: rule create/update/delete is web-UI only.
        # Reject with an explanation and change no state.
        return (
            "Rules can't be changed from Telegram. Open the web UI "
            "(Rules page) to create, edit, or delete a scheduling rule. "
            "This chat is for status checks and one-off actions only."
        )
    # Any other command: helpful nudge, no state change.
    return (
        "Unknown command. Send /help to see what I can do, or /start "
        "<token> with the token from the web UI to bind this chat."
    )


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
        "/bookclass": "bookclass",
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
# chat's binding state beyond "not bound".
_UNBOUND_REJECTION = (
    "This chat is not bound. Open the web UI (Telegram page) and click "
    "'Generate link' to bind it before using this command."
)


def _operator_for_chat(session: Session, chat_id: str) -> OperatorProfile | None:
    """Resolve the operator bound to ``chat_id`` (or ``None``).

    Central bound-chat lookup shared by every stateful handler so the
    ``telegram_chat_id`` scoping (FR-031) lives in one place.
    """
    return session.execute(
        select(OperatorProfile).where(OperatorProfile.telegram_chat_id == chat_id)
    ).scalar_one_or_none()


def _handle_help() -> str:
    """TG.4: list the supported commands."""
    return (
        "Commands:\n"
        "/status — is this chat bound?\n"
        "/next — next scheduled booking and upcoming slots (with ids)\n"
        "/last — most recent booking outcome\n"
        "/cancel <booking-id> — cancel a booking\n"
        "/ack — acknowledge the cookie-expiring warning\n"
        "/bookclass <YYYY-MM-DD> <HH:MM> [class type] — one-off manual booking\n"
        "Rules are managed in the web UI, not here."
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
        row = _operator_for_chat(session, chat_id)
    if row is None:
        return (
            "This chat is not bound. Open the web UI (Telegram page) "
            "and click 'Generate link' to bind."
        )
    return (
        f"Bound to operator {row.display_name or f'#{row.id}'}. "
        "You will receive booking outcomes and alerts here."
    )


def _handle_next(request: Request, *, chat_id: str) -> str:
    """TG.3: report the next scheduled booking and upcoming slots."""
    now = datetime.now(tz=UTC)
    tz = operator_timezone()
    with get_session() as session:
        operator = _operator_for_chat(session, chat_id)
        if operator is None:
            return _UNBOUND_REJECTION
        next_booking = compute_next_booking(session, operator.id, now)
        upcoming = list_upcoming_slots(session, operator.id, now=now)

    if next_booking is None and not upcoming:
        return "Nothing scheduled. No active rules have a window on the horizon."

    lines: list[str] = []
    if next_booking is not None:
        slot_local = next_booking.target_slot.astimezone(tz)
        opens_local = next_booking.window_open.astimezone(tz)
        lines.append(
            "Next booking: "
            f"{slot_local:%a %d %b at %H:%M} "
            f"(window opens {opens_local:%a %d %b at %H:%M})."
        )
    if upcoming:
        lines.append("Upcoming slots:")
        for slot in upcoming[:5]:
            slot_local = slot.target_slot.astimezone(tz)
            if slot.kind == "granted":
                # Granted slots are cancellable; surface the id /cancel needs.
                lines.append(
                    f"- #{slot.booking_id} {slot_local:%a %d %b at %H:%M} "
                    f"{slot.target_class} (granted)"
                )
            else:
                lines.append(f"- {slot_local:%a %d %b at %H:%M} {slot.target_class} (scheduled)")
    return "\n".join(lines)


def _handle_last(request: Request, *, chat_id: str) -> str:
    """TG.3: report the most recent booking outcome."""
    tz = operator_timezone()
    with get_session() as session:
        operator = _operator_for_chat(session, chat_id)
        if operator is None:
            return _UNBOUND_REJECTION
        recent = list_recent_bookings(session, operator.id, limit=1)

    if not recent:
        return "No bookings yet. Nothing has been attempted for this operator."
    last = recent[0]
    slot_local = last.target_slot.astimezone(tz)
    attempted_local = last.attempted_at.astimezone(tz)
    return (
        f"Last booking #{last.id}: {last.target_class} on {slot_local:%a %d %b at %H:%M} "
        f"— {last.terminal_status} (attempted {attempted_local:%a %d %b at %H:%M})."
    )


def _handle_cancel(request: Request, *, chat_id: str, argument: str) -> str:
    """US6.3 / CC-015: idempotent cancel of a booking by id."""
    booking_id_text = argument.strip()
    if not booking_id_text:
        return "Usage: /cancel <booking-id>. Find the id in /next, /last, or the web UI."
    try:
        booking_id = int(booking_id_text)
    except ValueError:
        return "Booking id must be a number. Usage: /cancel <booking-id>."

    client = getattr(request.app.state, "wodbuster_client", None)
    cookie_store = getattr(request.app.state, "cookie_store", None)
    if client is None or cookie_store is None:
        return "Cancellation is temporarily unavailable. Try again shortly."

    tz = operator_timezone()
    with get_session() as session:
        operator = _operator_for_chat(session, chat_id)
        if operator is None:
            return _UNBOUND_REJECTION
        try:
            outcome = cancel_booking(
                session,
                operator_id=operator.id,
                booking_id=booking_id,
                client=client,
                cookie_store=cookie_store,
            )
        except BookingNotFoundError:
            return f"Booking #{booking_id} not found for this operator."
        except BookingAlreadyCancelledError:
            # CC-015: idempotent — no WodBuster call was issued.
            return f"Booking #{booking_id} is already cancelled. Nothing to do."
        except CancellationUpstreamError:
            return f"Couldn't reach WodBuster to cancel #{booking_id}. Try again in a moment."
        # Capture display values before commit expires the attributes.
        target_class = outcome.target_class
        slot_local = outcome.target_slot.astimezone(tz)
        session.commit()
    return f"Cancelled #{booking_id}: {target_class} on {slot_local:%a %d %b at %H:%M}."


def _handle_ack(request: Request, *, chat_id: str) -> str:
    """TG.5: acknowledge the open cookie-expiring alert."""
    now = datetime.now(tz=UTC)
    with get_session() as session:
        operator = _operator_for_chat(session, chat_id)
        if operator is None:
            return _UNBOUND_REJECTION
        alert_id = acknowledge_open_cookie_expiring(session, operator.id, now=now)
        if alert_id is None:
            return "No open cookie-expiring warning to acknowledge."
        session.commit()
    return "Acknowledged. I'll stop nagging about the cookie for this cycle."


def _handle_bookclass(request: Request, *, chat_id: str, argument: str) -> str:
    """US8.3: one-off manual booking ``/bookclass <YYYY-MM-DD> <HH:MM> [class type]``.

    Validates the argument shape, resolves the bound operator (FR-031),
    then delegates to :class:`ManualBookingService`. The service issues
    a single read-only ``LoadClass`` probe to check the reservation
    window (CC-010) and resolve the class type before firing exactly
    one booking attempt.

    The optional trailing ``class type`` disambiguates several classes
    sharing a start time (Cross Training vs Open Endurance at 08:30).
    When omitted, the service books whichever class runs at that time.
    """
    args = argument.split()
    if len(args) < 2 or not _valid_date(args[0]) or not _valid_time(args[1]):
        return (
            "Usage: /bookclass <YYYY-MM-DD> <HH:MM> [class type]. "
            "Example: /bookclass 2026-07-15 18:30 Cross Training."
        )
    class_type = " ".join(args[2:]).strip() or None

    client = getattr(request.app.state, "wodbuster_client", None)
    cookie_store = getattr(request.app.state, "cookie_store", None)
    if client is None or cookie_store is None:
        return "Manual booking is temporarily unavailable. Try again shortly."

    with get_session() as session:
        operator = _operator_for_chat(session, chat_id)
        if operator is None:
            return _UNBOUND_REJECTION
        operator_id = operator.id

    settings = getattr(request.app.state, "settings", None)
    operator_idu = getattr(settings, "wodbuster_idu", None) if settings is not None else None
    service = ManualBookingService(
        client=client,
        cookie_store=cookie_store,
        operator_idu=operator_idu,
    )

    target_date = datetime.strptime(args[0], "%Y-%m-%d").date()
    try:
        result = service.book(
            operator_id=operator_id,
            target_date=target_date,
            target_time=args[1],
            class_type=class_type,
        )
    except NoCookieError:
        return "No active WodBuster session on file. Refresh your cookie and retry."
    except BookingWindowClosedError:
        return (
            f"{args[1]} on {args[0]} isn't open for booking yet. "
            "Try again once its reservation window opens."
        )
    except ClassNotVisibleError:
        if class_type is not None:
            return f"No {class_type} class found at {args[1]} on {args[0]}."
        return f"No class found at {args[1]} on {args[0]}."
    except ManualBookingUpstreamError:
        return "Couldn't reach WodBuster to book. Try again in a moment."

    if result.terminal_status == "granted":
        return f"Booked {result.class_type} at {args[1]} on {args[0]}."
    return f"Booking for {args[1]} on {args[0]} wasn't granted ({result.terminal_status})."


def _valid_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _valid_time(value: str) -> bool:
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        return False
    return True


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
