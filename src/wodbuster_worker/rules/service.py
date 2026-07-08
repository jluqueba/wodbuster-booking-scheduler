"""Scheduler rule persistence (US-005).

Handles the four CRUD operations against ``scheduler_rule`` and its
``class_preference`` children. Session and transaction management is
the caller's responsibility (route handlers open a session per
request).

Multi-day fan-out lives in :func:`create_rules_for_days`: when the
operator ticks Mon+Wed+Fri, that function inserts three sibling rows
that share the same time slot, preferences, and offset. This keeps
the schema unchanged (one row per day) while offering the operator a
"one form, many days" experience.

Ownership: a rule can only be seen or mutated by its owner. Callers
translate a ``None`` from :func:`get_rule_for_operator` into a 404 so
we do not confirm existence to an unauthorised caller (CC-012).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..persistence.models import ClassPreference, SchedulerRule
from .forms import PreferenceInput


def list_rules_for_operator(
    session: Session, operator_id: int
) -> Sequence[SchedulerRule]:
    """Return every rule the operator owns, ordered by day of week."""
    return (
        session.execute(
            select(SchedulerRule)
            .where(SchedulerRule.operator_id == operator_id)
            .options(selectinload(SchedulerRule.preferences))
            .order_by(SchedulerRule.day_of_week, SchedulerRule.created_at)
        )
        .scalars()
        .all()
    )


def get_rule_for_operator(
    session: Session, operator_id: int, rule_id: int
) -> SchedulerRule | None:
    """Return the rule if it exists AND belongs to the operator."""
    return session.scalar(
        select(SchedulerRule)
        .where(
            SchedulerRule.id == rule_id,
            SchedulerRule.operator_id == operator_id,
        )
        .options(selectinload(SchedulerRule.preferences))
    )


def create_rules_for_days(
    session: Session,
    *,
    operator_id: int,
    days_of_week: Sequence[int],
    time_slot: str,
    preferences: Sequence[PreferenceInput],
    window_offset_hours: int,
) -> list[SchedulerRule]:
    """Insert one rule per day-of-week; all share time / prefs / offset.

    ``preferences`` are class-type-only at the form layer; here we
    denormalise ``time_slot`` into every :class:`ClassPreference`'s
    ``target_time_slot`` so downstream readers (the alert evaluator's
    :func:`compute_next_window`) keep working unchanged.

    Caller commits.
    """
    rules: list[SchedulerRule] = []
    for day in days_of_week:
        rule = SchedulerRule(
            operator_id=operator_id,
            day_of_week=day,
            window_offset_hours=window_offset_hours,
            active=True,
            preferences=[
                ClassPreference(
                    order_index=pref.order_index,
                    class_type=pref.class_type,
                    target_time_slot=time_slot,
                )
                for pref in preferences
            ],
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
    time_slot: str,
    preferences: Sequence[PreferenceInput],
    window_offset_hours: int,
) -> SchedulerRule:
    """Replace one rule's fields and its full preference list.

    Edit is per-row on purpose: if a rule was originally created via
    fan-out (Mon+Wed+Fri) the operator edits each row individually.
    Group-edit UX can layer on top later without changing the
    persistence contract.
    """
    rule.day_of_week = day_of_week
    rule.window_offset_hours = window_offset_hours

    rule.preferences.clear()
    session.flush()

    for pref in preferences:
        rule.preferences.append(
            ClassPreference(
                order_index=pref.order_index,
                class_type=pref.class_type,
                target_time_slot=time_slot,
            )
        )
    return rule


def delete_rule(session: Session, rule: SchedulerRule) -> None:
    """Remove the rule and its preferences (cascade)."""
    session.delete(rule)


__all__ = [
    "create_rules_for_days",
    "delete_rule",
    "get_rule_for_operator",
    "list_rules_for_operator",
    "update_rule",
]
