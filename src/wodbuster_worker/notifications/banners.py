"""Banner data source for the dashboard (US2.3, US2.7).

Reads open alert rows (``closed_at IS NULL``) for one operator and
turns them into a small view-model list the template renders as a
banner stack. The alert row payload is already the source of truth —
producers (heartbeat evaluator, later booking evaluator) write the
payload in the same transaction as the state change that motivated
the alert, so the banner is always consistent with the DB.

Not a service in the ORM-service sense — just a query + a small
mapping layer. Keeping it out of the route module means the dashboard
view stays focused on presentation while the alert-kind vocabulary
lives next to the notification code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..i18n import get_language, t
from ..persistence.models import Alert, GymAccount
from .messages import format_slot


@dataclass(frozen=True)
class BannerItem:
    """Everything the ``_banners.html`` partial needs about one alert."""

    kind: str
    severity: str
    heading: str
    body: str
    first_emitted_at: datetime
    last_emitted_at: datetime


def load_banners_for_operator(session: Session, operator_id: int) -> list[BannerItem]:
    """Return every open alert owned by ``operator_id`` as a banner item.

    Alerts are gym-account-scoped (ADR-0007); this joins through
    ``gym_account`` so a user sees the open alerts across all their
    gym accounts (today, the single seeded account). Rows are ordered
    by ``first_emitted_at`` descending so the newest condition sits at
    the top of the banner stack — matches how the operator's attention
    actually flows.
    """
    rows = (
        session.execute(
            select(Alert)
            .join(GymAccount, Alert.gym_account_id == GymAccount.id)
            .where(
                GymAccount.user_id == operator_id,
                Alert.closed_at.is_(None),
            )
            .order_by(Alert.first_emitted_at.desc())
        )
        .scalars()
        .all()
    )
    return [_to_banner_item(alert) for alert in rows]


def _to_banner_item(alert: Alert) -> BannerItem:
    kind = alert.kind
    payload: dict[str, Any] = alert.payload or {}
    heading, body, severity = _render(kind, payload)
    return BannerItem(
        kind=kind,
        severity=severity,
        heading=heading,
        body=body,
        first_emitted_at=alert.first_emitted_at,
        last_emitted_at=alert.last_emitted_at,
    )


def _render(kind: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    """Return ``(heading, body, severity)`` for one alert kind.

    Rendered in the operator's web language (``t``) with one date
    format (``format_slot``, gym timezone). Severity vocabulary:
    ``warning`` (something to act on) or ``error`` (worker paused /
    degraded). The design-system CSS in ``brand.css`` styles both.
    """
    if kind == "cookie_expiring":
        return (
            t("banner.cookie_expiring.heading"),
            t("banner.cookie_expiring.body", when=_format_window(payload.get("next_window_at"))),
            "warning",
        )
    if kind == "cookie_invalid":
        return (
            t("banner.cookie_invalid.heading"),
            t("banner.cookie_invalid.body"),
            "error",
        )
    if kind == "heartbeat_anomaly":
        return (
            t("banner.anomaly.heading"),
            t("banner.anomaly.body"),
            "error",
        )
    # Unknown kind — surface as a generic warning so the operator at
    # least sees that something happened.
    return (
        t("banner.unknown.heading", kind=kind),
        t("banner.unknown.body"),
        "warning",
    )


def _format_window(value: Any) -> str:
    """Localised gym-tz label for an ISO window instant, or a fallback phrase."""
    if not value:
        return t("banner.window_fallback")
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return t("banner.window_fallback")
    return format_slot(parsed, get_language())


__all__ = ["BannerItem", "load_banners_for_operator"]
