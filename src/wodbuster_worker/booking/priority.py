"""Admin booking priority (spec ``admin-booking-priority``).

Computes, for one regular user's run, the classes an admin also targets
at the same gym on the same day. The executor holds a request for one of
those classes back by a short fixed delay after publication, so the
admin's request reaches WodBuster first.

Protection is read from stored rules, overrides and vacation windows
only: no booking thread ever waits for, or signals, another one.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.models import GymAccount, OperatorProfile, SchedulerRule
from .executor import NO_PROTECTION, ProtectedClasses, build_attempt_chain, protection_key
from .overrides import OverridePlan, effective_slot_for, load_override
from .vacation import find_covering_window


def is_admin(user: OperatorProfile) -> bool:
    """FR-001: an admin is an active user with ``is_admin`` set."""
    return bool(user.is_admin) and user.status == "active"


def resolve_protected_classes(
    session: Session,
    *,
    gym_account: GymAccount,
    target_date: date,
) -> ProtectedClasses:
    """Return the classes admins contest at ``gym_account``'s gym on ``target_date``.

    Empty when the run belongs to an admin (FR-008). Otherwise each active
    admin rule for that weekday, on an active gym account of the same gym,
    contributes the classes of its attempt chain for the date: override
    target, primary and second shot, exactly as the admin's own run walks
    them. An admin rule protects nothing when the admin skips the day or
    is on vacation (FR-003).
    """
    owner = session.get(OperatorProfile, gym_account.user_id)
    if owner is None or is_admin(owner):
        return NO_PROTECTION

    admin_rules = (
        session.execute(
            select(SchedulerRule)
            .join(GymAccount, GymAccount.id == SchedulerRule.gym_account_id)
            .join(OperatorProfile, OperatorProfile.id == GymAccount.user_id)
            .where(
                SchedulerRule.active.is_(True),
                SchedulerRule.day_of_week == target_date.weekday(),
                GymAccount.active.is_(True),
                GymAccount.gym_slug == gym_account.gym_slug,
                OperatorProfile.is_admin.is_(True),
                OperatorProfile.status == "active",
            )
        )
        .scalars()
        .all()
    )

    protected: set[tuple[str, str]] = set()
    for admin_rule in admin_rules:
        protected.update(_contested_by(session, admin_rule, target_date))
    return frozenset(protected)


def _contested_by(
    session: Session, admin_rule: SchedulerRule, target_date: date
) -> list[tuple[str, str]]:
    """Classes ``admin_rule``'s run contests on ``target_date``.

    Mirrors the scheduler and executor: the override is applied first, and
    the vacation guard is checked against the resulting slot.
    """
    row = load_override(session, rule_id=int(admin_rule.id), target_date=target_date)
    override = OverridePlan.from_row(row) if row is not None else None
    if override is not None and override.skip_day:
        return []

    class_time = (
        override.class_time
        if override is not None and override.class_time is not None
        else str(admin_rule.class_time)
    )
    slot = effective_slot_for(admin_rule, target_date, class_time)
    if (
        find_covering_window(
            session, gym_account_id=int(admin_rule.gym_account_id), target_slot=slot
        )
        is not None
    ):
        return []

    chain = build_attempt_chain(rule=admin_rule, target_slot=slot, override=override)
    return [protection_key(c.class_type, c.class_time) for c in chain]


__all__ = ["is_admin", "resolve_protected_classes"]
