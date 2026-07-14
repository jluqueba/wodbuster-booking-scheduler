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
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.models import SchedulerRule


def list_rules_for_operator(session: Session, operator_id: int) -> Sequence[SchedulerRule]:
    """Return every rule the operator owns, ordered by day of week."""
    return (
        session.execute(
            select(SchedulerRule)
            .where(SchedulerRule.operator_id == operator_id)
            .order_by(SchedulerRule.day_of_week, SchedulerRule.created_at)
        )
        .scalars()
        .all()
    )


def get_rule_for_operator(session: Session, operator_id: int, rule_id: int) -> SchedulerRule | None:
    """Return the rule if it exists AND belongs to the operator."""
    return session.scalar(
        select(SchedulerRule).where(
            SchedulerRule.id == rule_id,
            SchedulerRule.operator_id == operator_id,
        )
    )


def create_rules_for_days(
    session: Session,
    *,
    operator_id: int,
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
            operator_id=operator_id,
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
) -> SchedulerRule:
    """Replace one rule's fields.

    Edit is per-row on purpose: if a rule was originally created via
    fan-out (Mon+Wed+Fri) the operator edits each row individually.
    """
    rule.day_of_week = day_of_week
    rule.class_type = class_type
    rule.class_time = class_time
    rule.booking_opens_days_before = booking_opens_days_before
    rule.booking_opens_at = booking_opens_at
    rule.second_shot_class_type = second_shot_class_type
    rule.second_shot_class_time = second_shot_class_time
    return rule


def delete_rule(session: Session, rule: SchedulerRule) -> None:
    """Remove the rule."""
    session.delete(rule)


__all__ = [
    "create_rules_for_days",
    "delete_rule",
    "get_rule_for_operator",
    "list_rules_for_operator",
    "update_rule",
]
