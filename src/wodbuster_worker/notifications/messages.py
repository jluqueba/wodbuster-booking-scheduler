"""Send-time rendering of Telegram message bodies (T-UP-009).

ADR-0008: a Telegram message is rendered in the *recipient's* stored
``communication_language`` at the moment the dispatcher sends it, not
in the producer's language when the outbox row is enqueued.

Every message is uniform (user requirement 2026-07-30):

- One date format everywhere (:func:`format_slot`, rendered in the
  gym timezone because Telegram cannot run the web's client-side
  formatter).
- The gym name (``display_name``) so a multi-gym operator can tell
  which gym a message is about.
- The referenced booking's ``#id`` whenever a message is about a
  specific booking.

The templates live in the i18n catalog under the ``tg.*`` namespace
(English + Spanish); this module only maps a structured outbox payload
to a template key and formats the placeholders.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..i18n import normalize_language, t_lang

# Locale-neutral, self-contained weekday/month abbreviations. Kept
# here rather than in the catalog because they are data, not UI copy,
# and Babel is intentionally not a dependency (see i18n docstring).
_WEEKDAYS: dict[str, tuple[str, ...]] = {
    "en": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
    "es": ("lun", "mar", "mié", "jue", "vie", "sáb", "dom"),
}
_MONTHS: dict[str, tuple[str, ...]] = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "es": ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"),
}

# terminal_status -> catalog key for booking_result messages.
_BOOKING_KEYS: dict[str, str] = {
    "granted": "tg.booking.granted",
    "full": "tg.booking.full",
    "class_not_visible": "tg.booking.class_not_visible",
    "cookie_invalid": "tg.booking.cookie_invalid",
    "upstream_unavailable": "tg.booking.upstream_unavailable",
    "skipped": "tg.booking.skipped",
    "cancelled": "tg.booking.cancelled",
}


def format_slot(when: datetime, lang: str) -> str:
    """One canonical, localised slot label in the gym timezone.

    Example: ``Mon 04 Aug 18:30`` (en) / ``lun 04 ago 18:30`` (es).
    """
    from ..scheduler.rule_jobs import operator_timezone

    lang = normalize_language(lang)
    local = when.astimezone(operator_timezone())
    weekday = _WEEKDAYS[lang][local.weekday()]
    month = _MONTHS[lang][local.month - 1]
    return f"{weekday} {local.day:02d} {month} {local:%H:%M}"


def render(payload: dict[str, Any] | None, *, lang: str, gym_name: str) -> str | None:
    """Render an outbox payload into a Telegram body, or ``None``.

    Returns ``None`` when the payload kind is unknown so the caller
    can fall back to any pre-rendered ``text`` on the row.
    """
    payload = payload or {}
    kind = payload.get("kind")
    if kind == "booking_result":
        return _render_booking(payload, lang, gym_name)
    if kind == "cookie_expiring":
        return t_lang(
            lang,
            "tg.alert.cookie_expiring",
            gym=gym_name,
            when=_when(payload.get("next_window_at"), lang),
        )
    if kind == "cookie_invalid":
        return t_lang(lang, "tg.alert.cookie_invalid", gym=gym_name)
    if kind == "heartbeat_anomaly":
        return _render_anomaly(payload, lang, gym_name)
    return None


def _render_booking(payload: dict[str, Any], lang: str, gym_name: str) -> str:
    status = str(payload.get("terminal_status") or "")
    key = _BOOKING_KEYS.get(status, "tg.booking.unknown")
    klass = str(payload.get("class_type") or payload.get("target_class") or "?")
    booking_id = payload.get("outcome_id", "?")
    when = _when(payload.get("target_slot"), lang)
    if key == "tg.booking.unknown":
        return t_lang(
            lang,
            key,
            gym=gym_name,
            id=booking_id,
            klass=klass,
            when=when,
            status=status or "?",
        )
    return t_lang(lang, key, gym=gym_name, id=booking_id, klass=klass, when=when)


def _render_anomaly(payload: dict[str, Any], lang: str, gym_name: str) -> str:
    missed = payload.get("missed") or []
    if len(missed) == 1:
        entry = missed[0]
        return t_lang(
            lang,
            "tg.alert.anomaly.one",
            gym=gym_name,
            klass=str(entry.get("target_class") or "?"),
            when=_when(entry.get("target_slot"), lang),
        )
    return t_lang(lang, "tg.alert.anomaly.many", gym=gym_name, count=len(missed))


def _when(iso: Any, lang: str) -> str:
    if not iso:
        return "?"
    try:
        parsed = datetime.fromisoformat(str(iso))
    except ValueError:
        return "?"
    return format_slot(parsed, lang)


__all__ = ["format_slot", "render"]
