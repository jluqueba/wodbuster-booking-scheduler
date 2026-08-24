"""Scheduler rule persistence (rule model v2).

Handles the four CRUD operations against ``scheduler_rule``. Session
and transaction management is the caller's responsibility (route
handlers open a session per request).

Multi-day fan-out lives in :func:`create_rules_for_days`: when the
operator ticks Mon+Wed+Fri, that function inserts three sibling rows
that share every field except ``day_of_week``. This keeps the schema
unchanged (one row per day) while offering the operator a "one form,
many days" experience.

Ownership: a rule can only be seen or mutated by its owner. Callers
translate a ``None`` from :func:`get_rule_for_operator` into a 404 so
we do not confirm existence to an unauthorised caller (CC-012).

Latent hook (ADR-0012, plan AMB-004): :func:`deactivate_rule` has no
HTTP surface. This feature deliberately ships no rule-deactivation UI,
because the operator's single-day need is served by the skip action on
the history screen. The function is kept real, tested and used as the
*only* place in ``src/`` that writes ``SchedulerRule.active = False``,
so a future deactivation feature has exactly one place to plug into and
the override cleanup cannot be forgotten. It is not dead code; do not
delete it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..booking.overrides import discard_future_overrides_for_rule
from ..persistence.models import SchedulerRule


def _utcnow() -> datetime:
    """Clock seam so tests can place overrides in the past and future."""
    return datetime.now(tz=UTC)


@dataclass(frozen=True)
class RuleUpdate:
    """Outcome of :func:`update_rule`.

    ``discarded_override_dates`` carries the dates rather than a bare
    count because the route has to name them in the flash (FR-023).
    """

    rule: SchedulerRule
    discarded_override_dates: tuple[date, ...]


def list_rules_for_operator(session: Session, gym_account_id: int) -> Sequence[SchedulerRule]:
    """Return every rule the gym account owns, ordered by day of week."""
    return (
        session.execute(
            select(SchedulerRule)
            .where(SchedulerRule.gym_account_id == gym_account_id)
            .order_by(SchedulerRule.day_of_week, SchedulerRule.created_at)
        )
        .scalars()
        .all()
    )


def get_rule_for_operator(
    session: Session, gym_account_id: int, rule_id: int
) -> SchedulerRule | None:
    """Return the rule if it exists AND belongs to the gym account."""
    return session.scalar(
        select(SchedulerRule).where(
            SchedulerRule.id == rule_id,
            SchedulerRule.gym_account_id == gym_account_id,
        )
    )


def create_rules_for_days(
    session: Session,
    *,
    gym_account_id: int,
    days_of_week: Sequence[int],
    class_type: str,
    class_time: str,
    booking_opens_days_before: int,
    booking_opens_at: str,
    second_shot_class_type: str | None = None,
    second_shot_class_time: str | None = None,
) -> list[SchedulerRule]:
    """Insert one rule per day-of-week; all share every other field.

    Caller commits.
    """
    rules: list[SchedulerRule] = []
    for day in days_of_week:
        rule = SchedulerRule(
            gym_account_id=gym_account_id,
            day_of_week=day,
            class_type=class_type,
            class_time=class_time,
            booking_opens_days_before=booking_opens_days_before,
            booking_opens_at=booking_opens_at,
            second_shot_class_type=second_shot_class_type,
            second_shot_class_time=second_shot_class_time,
            active=True,
        )
        session.add(rule)
        rules.append(rule)
    session.flush()
    return rules


def update_rule(
    session: Session,
    rule: SchedulerRule,
    *,
    day_of_week: int,
    class_type: str,
    class_time: str,
    booking_opens_days_before: int,
    booking_opens_at: str,
    second_shot_class_type: str | None,
    second_shot_class_time: str | None,
    now: datetime | None = None,
) -> RuleUpdate:
    """Replace one rule's fields.

    Edit is per-row on purpose: if a rule was originally created via
    fan-out (Mon+Wed+Fri) the operator edits each row individually.

    Moving ``day_of_week`` moves every date the rule projects, so the
    single-day overrides hanging off the old dates are discarded
    (FR-023). Any other edit leaves the projected dates intact and the
    overrides untouched, ``validated`` included (FR-024).
    """
    day_moved = int(rule.day_of_week) != day_of_week
    rule.day_of_week = day_of_week
    rule.class_type = class_type
    rule.class_time = class_time
    rule.booking_opens_days_before = booking_opens_days_before
    rule.booking_opens_at = booking_opens_at
    rule.second_shot_class_type = second_shot_class_type
    rule.second_shot_class_time = second_shot_class_time

    discarded: list[date] = []
    if day_moved:
        discarded = discard_future_overrides_for_rule(
            session,
            rule=rule,
            reason="rule_day_of_week_changed",
            now=now if now is not None else _utcnow(),
        )
    return RuleUpdate(rule=rule, discarded_override_dates=tuple(discarded))


def deactivate_rule(
    session: Session,
    rule: SchedulerRule,
    *,
    now: datetime | None = None,
) -> list[date]:
    """Turn the rule off and discard its future overrides (FR-026).

    Latent hook: nothing routes here yet, by the decision recorded in
    the module docstring. It is the single writer of ``active = False``
    in ``src/``, so deactivation and override cleanup can never drift
    apart.

    No user notice is emitted. There is no request context to flash
    into, and the resolved channel for the discard notice is the web
    flash only, so the discard is recorded in the structured log alone.
    """
    rule.active = False
    return discard_future_overrides_for_rule(
        session,
        rule=rule,
        reason="rule_deactivated",
        now=now if now is not None else _utcnow(),
    )


def delete_rule(session: Session, rule: SchedulerRule) -> None:
    """Remove the rule."""
    session.delete(rule)


__all__ = [
    "RuleUpdate",
    "create_rules_for_days",
    "deactivate_rule",
    "delete_rule",
    "get_rule_for_operator",
    "list_rules_for_operator",
    "update_rule",
]
