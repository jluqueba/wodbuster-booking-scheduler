"""Scheduler rule persistence (US5.2-5.5).

Handles the four CRUD operations against ``scheduler_rule`` and its
``class_preference`` children. Session and transaction management is
the caller's responsibility (route handlers open a session per
request).

Ownership check policy: an operator can only see or mutate their own
rules. Routes call :func:`get_rule_for_operator` which returns ``None``
when the rule id does not belong to the operator; the route surfaces
that as a 404 rather than a 403 so we do not confirm the row's
existence to an unauthorized caller (CC-012).
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
    """Return every rule the operator owns, newest first."""
    return (
        session.execute(
            select(SchedulerRule)
            .where(SchedulerRule.operator_id == operator_id)
            .options(selectinload(SchedulerRule.preferences))
            .order_by(SchedulerRule.created_at.desc())
        )
        .scalars()
        .all()
    )


def get_rule_for_operator(
    session: Session, operator_id: int, rule_id: int
) -> SchedulerRule | None:
    """Return the rule if it exists AND belongs to the operator.

    Returns ``None`` on either miss. Routes translate the ``None`` case
    into a 404, which is what an unauthorized caller sees.
    """
    return session.scalar(
        select(SchedulerRule)
        .where(
            SchedulerRule.id == rule_id,
            SchedulerRule.operator_id == operator_id,
        )
        .options(selectinload(SchedulerRule.preferences))
    )


def create_rule(
    session: Session,
    *,
    operator_id: int,
    day_of_week: int,
    window_offset_hours: int,
    preferences: Sequence[PreferenceInput],
) -> SchedulerRule:
    """Insert a rule and its preferences in one transactional unit.

    Caller commits. Enforces the ``uq_class_preference_rule_order``
    invariant implicitly because :func:`parse_rule_form` assigns
    ``order_index`` positionally, without gaps.
    """
    rule = SchedulerRule(
        operator_id=operator_id,
        day_of_week=day_of_week,
        window_offset_hours=window_offset_hours,
        active=True,
        preferences=[
            ClassPreference(
                order_index=pref.order_index,
                class_type=pref.class_type,
                target_time_slot=pref.target_time_slot,
            )
            for pref in preferences
        ],
    )
    session.add(rule)
    session.flush()  # populate rule.id for the caller (redirect target)
    return rule


def update_rule(
    session: Session,
    rule: SchedulerRule,
    *,
    day_of_week: int,
    window_offset_hours: int,
    preferences: Sequence[PreferenceInput],
) -> SchedulerRule:
    """Replace a rule's fields and its full preference list.

    The preference list is replaced wholesale rather than diffed: the
    order is meaningful (primary + fallbacks) and a diff-style update
    would need to handle deletion, reinsertion, and reordering
    together anyway. Wholesale replace is easier to reason about and
    the row counts here are small (single-digit).
    """
    rule.day_of_week = day_of_week
    rule.window_offset_hours = window_offset_hours

    # Clear existing preferences via the collection so the
    # cascade="all, delete-orphan" relationship deletes the old rows.
    rule.preferences.clear()
    session.flush()

    for pref in preferences:
        rule.preferences.append(
            ClassPreference(
                order_index=pref.order_index,
                class_type=pref.class_type,
                target_time_slot=pref.target_time_slot,
            )
        )
    return rule


def delete_rule(session: Session, rule: SchedulerRule) -> None:
    """Remove the rule and its preferences.

    Hard-delete: the ``cascade="all, delete-orphan"`` relationship
    cleans up ``class_preference`` rows automatically. The plan
    allows soft-delete via ``active=False`` but for now we hard-delete
    so the operator's rule list stays uncluttered; when audit history
    matters we can switch to soft-delete without changing the route.
    """
    session.delete(rule)


__all__ = [
    "create_rule",
    "delete_rule",
    "get_rule_for_operator",
    "list_rules_for_operator",
    "update_rule",
]
