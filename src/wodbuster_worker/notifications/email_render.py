"""Render an outbox payload into an email (ADR-0011).

Reuses :func:`notifications.messages.render` for the message body (so
email and Telegram stay word-for-word consistent and localised at send
time) and wraps it in the branded HTML shell
(``templates/email/notification.html.jinja``). Produces the subject, an
HTML part and a plain-text part.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from ..i18n import t_lang
from . import messages

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "email"
# ``autoescape=True`` (not name-based ``select_autoescape``) because the
# template file is ``*.html.jinja`` (a ``.jinja`` extension so the web HTML
# linter leaves it alone); its name does not end in ``.html`` so name-based
# autoescape would silently switch off and let user data through unescaped.
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=True,
)

# Gym-photo hero backdrop; the same permissive Unsplash CDN shot the web
# landing uses (brand.css .wb-hero__bg). Absolute URL so mail clients can
# fetch it. Outlook desktop ignores it and falls back to the dark header.
_HERO_IMAGE_URL = (
    "https://images.unsplash.com/photo-1517836357463-d25dfeac3438?w=1200&auto=format&fit=crop&q=80"
)

# outbox payload kind -> subject catalog key. All subjects take ``gym``.
_SUBJECT_KEYS: dict[str, str] = {
    "booking_result": "email.subject.booking",
    "cookie_expiring": "email.subject.cookie_expiring",
    "cookie_invalid": "email.subject.cookie_invalid",
    "heartbeat_anomaly": "email.subject.anomaly",
}

# Account (signup lifecycle) mail is email-only (no Telegram template) and has
# no gym context: kind -> (subject key, body key). Transactional; always sent.
_ACCOUNT_KINDS: dict[str, tuple[str, str]] = {
    "account_received": ("email.account.received.subject", "email.account.received.body"),
    "account_approved": ("email.account.approved.subject", "email.account.approved.body"),
    "account_rejected": ("email.account.rejected.subject", "email.account.rejected.body"),
}


@dataclass(frozen=True)
class EmailContent:
    """The three parts an ACS email message needs."""

    subject: str
    html: str
    text: str


def render_email(
    payload: dict[str, Any] | None,
    *,
    lang: str,
    gym_name: str,
    unsubscribe_url: str | None = None,
) -> EmailContent | None:
    """Render an email, or ``None`` when the payload kind is unknown."""
    kind = str((payload or {}).get("kind"))
    account = _ACCOUNT_KINDS.get(kind)
    if account is not None:
        subject_key, body_key = account
        subject = t_lang(lang, subject_key)
        text = t_lang(lang, body_key)
        title = subject
        html_body = text
        chip = ""  # no gym on account mail
    else:
        rendered = messages.render(payload, lang=lang, gym_name=gym_name)
        if rendered is None:
            return None
        text = rendered
        subject = t_lang(lang, _SUBJECT_KEYS.get(kind, "email.subject.booking"), gym=gym_name)
        # In-body heading: the subject without the trailing " · {gym}" (the gym
        # already shows in the chip). Every subject uses that separator.
        title = subject.rsplit(" · ", 1)[0]
        # Option (a): the HTML body drops the bracketed "[gym]" that the tg.*
        # copy carries, because the chip states the gym. The plain-text part
        # keeps it (no chip there).
        html_body = text.replace(f"[{gym_name}] ", "") if gym_name else text
        chip = gym_name

    html = _env.get_template("notification.html.jinja").render(
        lang=lang,
        subject=subject,
        title=title,
        gym_name=chip,
        hero_image_url=_HERO_IMAGE_URL,
        body_lines=html_body.split("\n"),
        unsubscribe_url=unsubscribe_url,
        footer_tagline=t_lang(lang, "email.footer.tagline"),
        footer_preferences=t_lang(lang, "email.footer.preferences"),
        footer_unsubscribe=t_lang(lang, "email.footer.unsubscribe"),
    )
    return EmailContent(subject=subject, html=html, text=text)


__all__ = ["EmailContent", "render_email"]
