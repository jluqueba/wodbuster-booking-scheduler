"""Per-run anomaly detector (US2.4, FR-026, CC-008).

Every 60 seconds the scheduler ticks :func:`run_anomaly_tick`. The
tick asks :func:`detect_missed_windows` which active rules had their
booking window open in the recent past *without* a paired
``booking_outcome`` row landing in the database. Each missed
``(rule, window)`` pair is a "silent run": the executor never
touched WodBuster, or it did but the outcome writer failed to
commit, or the scheduler itself stalled and the tick never
happened.

The alert is aggregated per operator via the "one open alert per
(operator, kind)" pattern shared with :mod:`heartbeat.alerts`: if
an open ``heartbeat_anomaly`` already exists we refresh
``last_emitted_at`` instead of inserting a duplicate. Callers own
the transaction; both the alert row and the paired outbox rows are
written inside the same session so the plan's cross-cutting rule
holds (an operator never sees a Telegram burst for an alert that
failed to persist and vice versa).

Grace period bounds "recent": a window that opened less than the
grace ago is still considered "in flight" — the retry loop inside
:class:`BookingExecutor.book` can take up to a couple of minutes,
so we do not want to raise a false anomaly just because the tick
fired between the window open and the outcome commit. Default 5
minutes; expose as a knob so tests can shrink it.

Lookback bounds the historical scan window: we only inspect
occurrences in ``(now - lookback, now - grace_period]``. Anything
older than ``lookback`` is water under the bridge; alerting on
week-old missed runs would just add noise. Default 60 minutes,
which is a comfortable buffer for a 60-second tick + a scheduler
that briefly missed its beat.

Re-fire interval bounds how often an already-open alert pushes
notifications again. The tick runs every 60 seconds and a missed
window stays detectable for the whole lookback, so without a gate
one silent run would mail the operator once a minute for an hour.
Same suppression contract as :mod:`heartbeat.alerts`:
``last_emitted_at`` records the last *notification*, not the last
detection, and a window the alert has not reported yet always
notifies regardless of the interval.

Closing is automatic (:func:`close_resolved_anomalies`). Each window an
alert tracks leaves the payload once it produces an outcome (a late
commit, a re-run) or once it is older than the retention horizon: at
that point there is no action left to take on that run. The alert
closes when nothing is left to track, and a scheduler that is still
stalled re-opens it on the next window it misses.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..booking.overrides import local_date_for_slot
from ..notifications.fanout import enqueue_email_row
from ..persistence.models import (
    Alert,
    BookingOutcome,
    GymAccount,
    NotificationOutbox,
    OperatorProfile,
    SchedulerRule,
)
from ..scheduler.clock import operator_timezone
from ..scheduler.rule_jobs import (
    next_window_open_for_rule,
    target_slot_for_window,
)

_log = structlog.get_logger(__name__)

_ALERT_KIND = "heartbeat_anomaly"

DEFAULT_GRACE_PERIOD = timedelta(minutes=5)
DEFAULT_LOOKBACK = timedelta(minutes=60)
DEFAULT_REFIRE_INTERVAL = timedelta(hours=6)
DEFAULT_RETENTION = timedelta(hours=24)


@dataclass(frozen=True)
class MissedWindow:
    """One rule/window pair that fired without producing an outcome."""

    rule_id: int
    gym_account_id: int
    target_class: str
    window_open: datetime
    target_slot: datetime


def detect_missed_windows(
    session: Session,
    *,
    now: datetime,
    grace_period: timedelta = DEFAULT_GRACE_PERIOD,
    lookback: timedelta = DEFAULT_LOOKBACK,
) -> list[MissedWindow]:
    """Return the missed ``(rule, window)`` pairs across all active rules.

    A pair is "missed" when:

    - the rule is active,
    - the rule's most-recent past window opened at least
      ``grace_period`` ago (so a currently-running attempt is not
      flagged prematurely),
    - the window opened no more than ``lookback`` ago (older gaps
      are considered water under the bridge),
    - the rule already existed when the window opened
      (``rule.created_at <= window_open``), and
    - no ``booking_outcome`` row references that rule anywhere on
      the target's operator-local calendar day.

    The last check is deliberately day-scoped rather than an exact
    ``(rule_id, target_slot)`` match. The slot the executor writes
    does not always equal the slot this function derives from the
    rule: a single-day override that moves ``class_time`` (ADR-0012)
    makes the executor book, and record, a different instant on the
    same day. Matching on the instant flagged those runs as silent
    even though the booking succeeded. Any outcome on the day is
    evidence that the executor ran, which is the only thing this
    detector is meant to assert.

    Outcome status is not read either: ``granted``, ``full``,
    ``skipped`` (vacation or override), ``cookie_invalid`` and the
    rest all count as evidence that the tick happened. Missing means
    "no row at all".
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    cutoff_recent = now - grace_period
    cutoff_ancient = now - lookback

    rules = (
        session.execute(select(SchedulerRule).where(SchedulerRule.active.is_(True))).scalars().all()
    )

    missed: list[MissedWindow] = []
    for rule in rules:
        try:
            next_open = next_window_open_for_rule(rule, now=now)
        except ValueError:
            # Malformed HH:MM is an operator-data bug; skip and let
            # someone fix the rule.
            continue

        # Every rule runs weekly, so the previous instance is exactly
        # 7 days before the next one.
        last_open = next_open - timedelta(days=7)

        if last_open >= cutoff_recent:
            # Still inside the grace window (or in the future).
            continue
        if last_open <= cutoff_ancient:
            # Older than the lookback horizon.
            continue
        if rule.created_at is not None and rule.created_at > last_open:
            # Rule did not exist at the time the window would have
            # opened — nothing to have missed.
            continue

        try:
            target_slot = target_slot_for_window(rule, last_open)
        except ValueError:
            continue

        if _outcome_exists(session, rule_id=int(rule.id), target_slot=target_slot):
            continue

        missed.append(
            MissedWindow(
                rule_id=int(rule.id),
                gym_account_id=int(rule.gym_account_id),
                target_class=str(rule.class_type),
                window_open=last_open,
                target_slot=target_slot,
            )
        )

    return missed


def emit_anomaly_alerts(
    session: Session,
    missed: Iterable[MissedWindow],
    *,
    now: datetime,
    refire_interval: timedelta = DEFAULT_REFIRE_INTERVAL,
) -> list[int]:
    """Get-or-create one ``heartbeat_anomaly`` alert per gym account.

    Groups ``missed`` by ``gym_account_id``. For each group, upserts the
    open alert row and decides whether this detection deserves a fresh
    round of notifications. It does when the alert is newly opened, when
    the group contains a window the open alert has not reported yet, or
    when ``refire_interval`` has elapsed since the last notification.
    Otherwise the tick is silent: no outbox rows, no ``last_emitted_at``
    bump, no payload rewrite, so the row keeps describing what the
    operator was actually told.

    Without that gate the 60-second tick mailed one row per minute for
    as long as the missed window stayed inside the lookback.

    A notified payload is the union of what the alert already tracked
    and what this tick detected (:func:`_merge_windows`), never a
    replacement: windows leave only once resolved or expired.

    Returns the alert ids that were created or re-notified; a suppressed
    refresh is not included, so the empty list means "nothing new was
    pushed to the operator".
    """
    grouped: dict[int, list[MissedWindow]] = {}
    for m in missed:
        grouped.setdefault(m.gym_account_id, []).append(m)

    touched: list[int] = []
    for gym_account_id, windows in grouped.items():
        alert = _open_alert(session, gym_account_id)
        if alert is None:
            payload = _build_payload(_merge_windows([], windows))
            alert = Alert(
                gym_account_id=gym_account_id,
                kind=_ALERT_KIND,
                payload=payload,
                first_emitted_at=now,
                last_emitted_at=now,
            )
            session.add(alert)
            session.flush()
        else:
            recorded = _recorded_missed(alert.payload, gym_account_id=gym_account_id)
            merged = _merge_windows(recorded, windows)
            has_unreported = len(merged) > len(recorded)
            if not has_unreported and now - alert.last_emitted_at < refire_interval:
                _log.debug(
                    "anomaly.emit.suppressed",
                    gym_account_id=gym_account_id,
                    alert_id=int(alert.id),
                )
                continue
            payload = _build_payload(merged)
            alert.payload = payload
            alert.last_emitted_at = now

        _enqueue_outbox_rows(
            session,
            gym_account_id=gym_account_id,
            alert_id=int(alert.id),
            payload=payload,
            now=now,
        )
        touched.append(int(alert.id))

    return touched


def close_resolved_anomalies(
    session: Session,
    *,
    now: datetime,
    retention: timedelta = DEFAULT_RETENTION,
) -> list[int]:
    """Close or prune open ``heartbeat_anomaly`` alerts window by window.

    Each window an alert tracks leaves its payload when either condition
    holds:

    - it has since produced an outcome (a late commit, a manual re-run,
      or a false positive the detector no longer raises), or
    - it is older than ``retention``. Nothing can be done about a
      booking window that closed a day ago, and a scheduler that is
      still stalled re-opens the alert on the next window it misses.
      Retention is well past the detector's lookback, so a window still
      being detected as missed can never trip this branch and the alert
      cannot flap.

    The alert closes when nothing is left to track. Windows that are
    neither resolved nor expired keep it open, which is why the payload
    is pruned rather than emptied: one silent run resolving must not
    clear the banner for another that did not.

    An alert whose payload carries no usable window (an older schema, a
    truncated write) is closed once the alert itself is older than
    ``retention``.

    Returns the closed alert ids.
    """
    open_alerts = (
        session.execute(
            select(Alert).where(
                Alert.kind == _ALERT_KIND,
                Alert.closed_at.is_(None),
            )
        )
        .scalars()
        .all()
    )

    closed: list[int] = []
    for alert in open_alerts:
        recorded = _recorded_missed(alert.payload, gym_account_id=int(alert.gym_account_id))
        if not recorded:
            if alert.first_emitted_at < now - retention:
                alert.closed_at = now
                closed.append(int(alert.id))
                _log.info(
                    "anomaly.alert.closed",
                    alert_id=int(alert.id),
                    gym_account_id=int(alert.gym_account_id),
                    reason="unreadable_payload",
                )
            continue

        remaining: list[MissedWindow] = []
        resolved = 0
        expired = 0
        for window in recorded:
            if _outcome_exists(session, rule_id=window.rule_id, target_slot=window.target_slot):
                resolved += 1
            elif window.window_open < now - retention:
                expired += 1
            else:
                remaining.append(window)

        if not remaining:
            alert.closed_at = now
            closed.append(int(alert.id))
            _log.info(
                "anomaly.alert.closed",
                alert_id=int(alert.id),
                gym_account_id=int(alert.gym_account_id),
                resolved=resolved,
                expired=expired,
            )
        elif len(remaining) != len(recorded):
            alert.payload = _build_payload(remaining)
            _log.info(
                "anomaly.alert.pruned",
                alert_id=int(alert.id),
                gym_account_id=int(alert.gym_account_id),
                resolved=resolved,
                expired=expired,
                remaining=len(remaining),
            )

    return closed


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _outcome_exists(session: Session, *, rule_id: int, target_slot: datetime) -> bool:
    """Return True when ``rule_id`` produced any outcome on ``target_slot``'s day.

    Day-scoped on purpose: see :func:`detect_missed_windows` for why
    an exact instant match is the wrong question to ask.
    """
    day_start, day_end = _local_day_bounds(target_slot)
    hit = session.execute(
        select(BookingOutcome.id)
        .where(
            BookingOutcome.rule_id == rule_id,
            BookingOutcome.target_slot >= day_start,
            BookingOutcome.target_slot < day_end,
        )
        .limit(1)
    ).scalar_one_or_none()
    return hit is not None


def _local_day_bounds(target_slot: datetime) -> tuple[datetime, datetime]:
    """Return the UTC half-open bounds of ``target_slot``'s local day.

    Arithmetic runs on local wall time before converting back, so a
    DST day is 23 or 25 hours wide rather than a naive 24.
    """
    tz = operator_timezone()
    local_midnight = datetime.combine(local_date_for_slot(target_slot), time.min, tzinfo=tz)
    return (
        local_midnight.astimezone(UTC),
        (local_midnight + timedelta(days=1)).astimezone(UTC),
    )


def _open_alert(session: Session, gym_account_id: int) -> Alert | None:
    return session.scalar(
        select(Alert)
        .where(
            Alert.gym_account_id == gym_account_id,
            Alert.kind == _ALERT_KIND,
            Alert.closed_at.is_(None),
        )
        .limit(1)
    )


def _build_payload(windows: list[MissedWindow]) -> dict[str, object]:
    return {
        "kind": _ALERT_KIND,
        "text": _render_text(windows),
        "missed": [
            {
                "rule_id": w.rule_id,
                "target_class": w.target_class,
                "window_open": w.window_open.astimezone(UTC).isoformat(),
                "target_slot": w.target_slot.astimezone(UTC).isoformat(),
            }
            for w in windows
        ],
    }


def _recorded_missed(payload: object, *, gym_account_id: int) -> list[MissedWindow]:
    """Return the missed windows a payload records.

    ``gym_account_id`` comes from the alert row; the payload never
    carried it. Entries that cannot be parsed back are dropped, and
    :func:`close_resolved_anomalies` treats an alert left with no usable
    entry as stale rather than permanently unresolvable.
    """
    if not isinstance(payload, dict):
        return []
    entries = payload.get("missed")
    if not isinstance(entries, list):
        return []
    parsed: list[MissedWindow] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rule_id = entry.get("rule_id")
        window_open = _parse_instant(entry.get("window_open"))
        target_slot = _parse_instant(entry.get("target_slot"))
        if not (isinstance(rule_id, int) and window_open is not None and target_slot is not None):
            continue
        parsed.append(
            MissedWindow(
                rule_id=rule_id,
                gym_account_id=gym_account_id,
                target_class=str(entry.get("target_class") or "?"),
                window_open=window_open,
                target_slot=target_slot,
            )
        )
    return parsed


def _merge_windows(
    recorded: list[MissedWindow], detected: list[MissedWindow]
) -> list[MissedWindow]:
    """Union ``recorded`` and ``detected`` by ``(rule_id, window_open)``.

    A notification round reports everything the alert is still tracking,
    not only what this tick detected. The detector's lookback is an hour,
    so a window it stops returning has not necessarily been resolved;
    replacing the payload with the current detection would drop it, and a
    later resolution of some *other* window would then close the alert on
    a silent run the operator never heard about.

    Entries leave the payload in exactly two places: resolved and expired
    ones, both pruned by :func:`close_resolved_anomalies`.
    """
    merged = list(recorded)
    seen = {(w.rule_id, w.window_open) for w in recorded}
    for window in detected:
        key = (window.rule_id, window.window_open)
        if key in seen:
            continue
        seen.add(key)
        merged.append(window)
    return sorted(merged, key=lambda w: (w.window_open, w.rule_id))


def _parse_instant(value: object) -> datetime | None:
    """Parse an ISO instant from a payload, or ``None`` when unusable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _render_text(windows: list[MissedWindow]) -> str:
    """Operator-facing summary of the missed windows."""
    if len(windows) == 1:
        w = windows[0]
        return (
            "Heartbeat anomaly: no booking outcome recorded for "
            f"{w.target_class} at {w.target_slot.astimezone(UTC):%a %d %b %H:%M UTC}. "
            "Check the worker logs."
        )
    return (
        "Heartbeat anomaly: "
        f"{len(windows)} scheduled bookings did not produce an "
        "outcome. Check the worker logs."
    )


def _enqueue_outbox_rows(
    session: Session,
    *,
    gym_account_id: int,
    alert_id: int,
    payload: dict[str, object],
    now: datetime,
) -> None:
    outbox_payload = {**payload, "alert_id": alert_id}

    gym_account = session.get(GymAccount, gym_account_id)
    if gym_account is None:  # pragma: no cover - FK guarantees presence
        return
    user_id = gym_account.user_id

    session.add(
        NotificationOutbox(
            user_id=user_id,
            gym_account_id=gym_account_id,
            kind="banner",
            target=str(user_id),
            payload=outbox_payload,
            enqueued_at=now,
        )
    )

    operator = session.get(OperatorProfile, user_id)
    enqueue_email_row(
        session, operator=operator, gym_account_id=gym_account_id, payload=outbox_payload, now=now
    )
    if operator is None or not operator.telegram_chat_id:
        return
    session.add(
        NotificationOutbox(
            user_id=user_id,
            gym_account_id=gym_account_id,
            kind="telegram",
            target=operator.telegram_chat_id,
            payload=outbox_payload,
            enqueued_at=now,
        )
    )


__all__ = [
    "DEFAULT_GRACE_PERIOD",
    "DEFAULT_LOOKBACK",
    "DEFAULT_REFIRE_INTERVAL",
    "DEFAULT_RETENTION",
    "MissedWindow",
    "close_resolved_anomalies",
    "detect_missed_windows",
    "emit_anomaly_alerts",
]
