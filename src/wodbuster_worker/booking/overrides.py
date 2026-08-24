"""Single-day override arithmetic and persistence (ADR-0012).

This module is the one place that converts between a rule's UTC booking
slots and the operator-local calendar day an override is keyed on. Every
other component (the date-scoped class probe, the override routes, the
upcoming projection, the executor) calls in here rather than
reimplementing ``astimezone(...).date()``, so an off-by-one across
midnight or a DST transition can only exist in one place.

The service half is deliberately ownership-blind: none of the load,
save or delete functions take a ``gym_account_id`` filter. The route
resolves the rule once through ``get_rule_for_operator`` and passes the
resulting object in, so there is one ownership check on one code path
(CC-013).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.models import BookingDayOverride, BookingOutcome, SchedulerRule
from ..scheduler.clock import DEFAULT_PREWARM_LEAD_S, operator_timezone

_log = structlog.get_logger(__name__)

# Safety margin on top of the pre-warm lead (FR-006). An edit accepted
# any later than this races the job that is about to fire.
DEFAULT_EDIT_MARGIN_S = 60.0


class OverrideWindowClosedError(Exception):
    """Raised when the edit cutoff has passed or the day already ran."""


class OverrideCombinationUnavailableError(Exception):
    """Raised when the combination is absent from a published schedule."""


class OverrideSkipConflictError(Exception):
    """Raised when a skip is submitted alongside an alternative target.

    ``ck_booking_day_override_skip_exclusive`` enforces the same rule in
    the database, but an integrity error surfacing to the user is not an
    error message. This is the first line of defence (FR-003, INV-002).
    """


@dataclass(frozen=True)
class OverridePlan:
    """Immutable view of an override, as handed to the executor.

    ``class_type`` and ``class_time`` are null when the override leaves
    that dimension on the rule's value, so both are resolved through the
    ``effective_*`` accessors rather than read directly.
    """

    override_id: int
    rule_id: int
    target_date: date
    class_type: str | None
    class_time: str | None
    skip_day: bool
    validated: bool
    suppress_second_shot: bool

    @classmethod
    def from_row(cls, row: BookingDayOverride) -> OverridePlan:
        """Snapshot ``row`` while its session is still open."""
        return cls(
            override_id=int(row.id),
            rule_id=int(row.rule_id),
            target_date=row.target_date,
            class_type=row.class_type,
            class_time=row.class_time,
            skip_day=bool(row.skip_day),
            validated=bool(row.validated),
            suppress_second_shot=bool(row.suppress_second_shot),
        )

    @property
    def changes_target(self) -> bool:
        """Whether the override moves the class type or the class time."""
        return self.class_type is not None or self.class_time is not None

    def effective_class_type(self, rule: SchedulerRule) -> str:
        return self.class_type if self.class_type is not None else str(rule.class_type)

    def effective_class_time(self, rule: SchedulerRule) -> str:
        return self.class_time if self.class_time is not None else str(rule.class_time)


def local_date_for_slot(target_slot: datetime) -> date:
    """Return the operator-local calendar day a UTC slot belongs to."""
    if target_slot.tzinfo is None:
        raise ValueError("target_slot must be timezone-aware")
    return target_slot.astimezone(operator_timezone()).date()


def effective_slot_for(rule: SchedulerRule, target_date: date, class_time: str) -> datetime:
    """Return the UTC class-start instant for ``target_date`` at ``class_time``.

    Mirrors :func:`scheduler.rule_jobs.target_slot_for_window`: the
    ``HH:MM`` is operator-local wall time, and the result is UTC. ``rule``
    is accepted for signature symmetry with the rest of the module; the
    arithmetic depends only on the date and the clock time.
    """
    return _local_wall_time(target_date, class_time).astimezone(UTC)


def window_open_for(rule: SchedulerRule, target_date: date) -> datetime:
    """Return the UTC instant the reservation window opens for ``target_date``.

    The inverse of :func:`scheduler.rule_jobs.target_slot_for_window`'s
    day arithmetic: the window opens ``booking_opens_days_before`` days
    before the class, at ``booking_opens_at`` in operator-local time.
    """
    open_day = target_date - timedelta(days=int(rule.booking_opens_days_before))
    return _local_wall_time(open_day, str(rule.booking_opens_at)).astimezone(UTC)


def edit_cutoff_for(
    rule: SchedulerRule,
    target_date: date,
    *,
    prewarm_lead_s: float = DEFAULT_PREWARM_LEAD_S,
    margin_s: float = DEFAULT_EDIT_MARGIN_S,
) -> datetime:
    """Return the last instant an override for ``target_date`` may be written.

    Derived from the pre-warm constant the scheduler actually registers
    the job with, so the two cannot drift apart (FR-006).
    """
    return window_open_for(rule, target_date) - timedelta(seconds=prewarm_lead_s + margin_s)


def is_editable(rule: SchedulerRule, target_date: date, now: datetime) -> bool:
    """Whether an override for ``target_date`` may still be written at ``now``."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now < edit_cutoff_for(rule, target_date)


def _local_wall_time(day: date, hhmm: str) -> datetime:
    hh, mm = hhmm.split(":")
    return datetime(
        day.year,
        day.month,
        day.day,
        int(hh),
        int(mm),
        tzinfo=operator_timezone(),
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load_override(
    session: Session, *, rule_id: int, target_date: date
) -> BookingDayOverride | None:
    """Single lookup for the trigger path."""
    return session.scalar(
        select(BookingDayOverride).where(
            BookingDayOverride.rule_id == rule_id,
            BookingDayOverride.target_date == target_date,
        )
    )


def load_overrides_in_range(
    session: Session,
    *,
    gym_account_id: int,
    start: date,
    end: date,
) -> dict[tuple[int, date], BookingDayOverride]:
    """Return every override in ``[start, end]`` keyed by rule and date.

    One query for the whole projection horizon, served by
    ``ix_booking_day_override_gym_date``.
    """
    rows = (
        session.execute(
            select(BookingDayOverride).where(
                BookingDayOverride.gym_account_id == gym_account_id,
                BookingDayOverride.target_date >= start,
                BookingDayOverride.target_date <= end,
            )
        )
        .scalars()
        .all()
    )
    return {(int(row.rule_id), row.target_date): row for row in rows}


def save_override(
    session: Session,
    *,
    rule: SchedulerRule,
    target_date: date,
    class_type: str | None = None,
    class_time: str | None = None,
    skip_day: bool = False,
    suppress_second_shot: bool = False,
    validated: bool = False,
    now: datetime,
) -> BookingDayOverride:
    """Upsert the override for ``(rule, target_date)``.

    Idempotent by construction: the unique constraint on
    ``(rule_id, target_date)`` is honoured by updating the existing row,
    so a duplicate submission is a no-op update rather than a second row
    (INV-001).

    Raises :class:`OverrideWindowClosedError` past the edit cutoff or
    once the executor has already recorded an outcome for that day
    (FR-007). Raises :class:`OverrideSkipConflictError` when a skip
    carries an alternative target (FR-003). No branch writes anything.
    The rule row is never written by this function.
    """
    if skip_day and (class_type is not None or class_time is not None):
        raise OverrideSkipConflictError(
            "a skipped day carries no alternative class type or class time"
        )
    _assert_writable(session, rule=rule, target_date=target_date, now=now)

    existing = load_override(session, rule_id=int(rule.id), target_date=target_date)
    if existing is None:
        existing = BookingDayOverride(
            rule_id=int(rule.id),
            # Denormalized from the resolved rule, never from the caller.
            gym_account_id=int(rule.gym_account_id),
            target_date=target_date,
        )
        session.add(existing)
    existing.class_type = class_type
    existing.class_time = class_time
    existing.skip_day = skip_day
    existing.suppress_second_shot = suppress_second_shot
    existing.validated = validated
    session.flush()
    return existing


def delete_override(
    session: Session,
    *,
    rule: SchedulerRule,
    target_date: date,
    now: datetime,
) -> bool:
    """Revert the override for ``target_date`` (FR-022).

    Returns ``False`` when there was nothing to revert, so a double
    submission is not an error. Shares the cutoff and already-executed
    guards with :func:`save_override`.
    """
    _assert_writable(session, rule=rule, target_date=target_date, now=now)

    existing = load_override(session, rule_id=int(rule.id), target_date=target_date)
    if existing is None:
        return False
    session.delete(existing)
    session.flush()
    return True


def discard_future_overrides_for_rule(
    session: Session,
    *,
    rule: SchedulerRule,
    reason: str,
    now: datetime,
) -> list[date]:
    """Drop the rule's overrides from today onwards (FR-023, FR-026).

    An override is a single-day amendment to a projected date. Once the
    rule stops projecting that date, the amendment has nothing to amend,
    so it is deleted rather than silently re-pointed at a day the user
    never chose.

    Overrides for dates already past are inert history and survive: they
    describe what was asked for on a day that has already run.

    Returns the discarded dates in ascending order, so ``len(...)`` is
    the count and the caller can name them back to the user, which
    FR-023 requires. The edit cutoff is deliberately not consulted: the
    day is gone regardless of how close its window is.
    """
    today = local_date_for_slot(now)
    rows = (
        session.execute(
            select(BookingDayOverride)
            .where(
                BookingDayOverride.rule_id == rule.id,
                BookingDayOverride.target_date >= today,
            )
            .order_by(BookingDayOverride.target_date)
        )
        .scalars()
        .all()
    )
    discarded = [row.target_date for row in rows]
    if not discarded:
        return discarded

    for row in rows:
        session.delete(row)
    session.flush()
    _log.info(
        "override.discarded",
        rule_id=int(rule.id),
        gym_account_id=int(rule.gym_account_id),
        reason=reason,
        discarded=len(discarded),
        dates=[day.isoformat() for day in discarded],
    )
    return discarded


def _assert_writable(
    session: Session, *, rule: SchedulerRule, target_date: date, now: datetime
) -> None:
    if not is_editable(rule, target_date, now):
        raise OverrideWindowClosedError(
            f"edit window for {target_date.isoformat()} closed at "
            f"{edit_cutoff_for(rule, target_date).isoformat()}"
        )
    if _outcome_exists(session, rule=rule, target_date=target_date):
        raise OverrideWindowClosedError(
            f"rule {rule.id} already has a booking outcome for {target_date.isoformat()}"
        )


def _outcome_exists(session: Session, *, rule: SchedulerRule, target_date: date) -> bool:
    """Whether the executor already ran for ``target_date``'s local day.

    ``booking_outcome.target_slot`` is an instant, so the operator-local
    day is matched as a half-open UTC range rather than by date equality.
    """
    day_start = _local_wall_time(target_date, "00:00").astimezone(UTC)
    day_end = _local_wall_time(target_date + timedelta(days=1), "00:00").astimezone(UTC)
    return (
        session.scalar(
            select(BookingOutcome.id)
            .where(
                BookingOutcome.rule_id == rule.id,
                BookingOutcome.target_slot >= day_start,
                BookingOutcome.target_slot < day_end,
            )
            .limit(1)
        )
        is not None
    )


__all__ = [
    "DEFAULT_EDIT_MARGIN_S",
    "OverrideCombinationUnavailableError",
    "OverridePlan",
    "OverrideSkipConflictError",
    "OverrideWindowClosedError",
    "delete_override",
    "discard_future_overrides_for_rule",
    "edit_cutoff_for",
    "effective_slot_for",
    "is_editable",
    "load_override",
    "load_overrides_in_range",
    "local_date_for_slot",
    "save_override",
    "window_open_for",
]
