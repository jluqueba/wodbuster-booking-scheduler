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

from ..persistence.models import Alert


@dataclass(frozen=True)
class BannerItem:
    """Everything the ``_banners.html`` partial needs about one alert."""

    kind: str
    severity: str
    heading: str
    body: str
    first_emitted_at: datetime
    last_emitted_at: datetime


def load_banners_for_operator(
    session: Session, operator_id: int
) -> list[BannerItem]:
    """Return every open alert for ``operator_id`` as a banner item.

    Rows are ordered by ``first_emitted_at`` descending so the newest
    condition sits at the top of the banner stack — matches how the
    operator's attention actually flows.
    """
    rows = session.execute(
        select(Alert)
        .where(
            Alert.operator_id == operator_id,
            Alert.closed_at.is_(None),
        )
        .order_by(Alert.first_emitted_at.desc())
    ).scalars().all()
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

    Severity vocabulary: ``warning`` (something to act on) or
    ``error`` (worker paused / degraded). The design-system CSS in
    ``brand.css`` styles both.
    """
    if kind == "cookie_expiring":
        window = payload.get("next_window_at", "the next window")
        return (
            "Cookie expiring soon",
            (
                "Your WodBuster cookie is projected to expire before "
                f"{window}. Paste a fresh cookie on the Cookie page to "
                "keep bookings running."
            ),
            "warning",
        )
    if kind == "cookie_invalid":
        return (
            "Cookie rejected",
            (
                "WodBuster rejected the stored cookie. Bookings are "
                "paused until you paste a fresh one."
            ),
            "error",
        )
    if kind == "heartbeat_anomaly":
        window = payload.get("window_close_expected", "the last window")
        return (
            "Silent-run detected",
            (
                "No booking outcome was recorded for the window that "
                f"should have closed by {window}. Check the worker."
            ),
            "error",
        )
    # Unknown kind — surface as a generic warning so the operator at
    # least sees that something happened.
    return (
        f"Alert: {kind}",
        "See logs for details.",
        "warning",
    )


__all__ = ["BannerItem", "load_banners_for_operator"]
