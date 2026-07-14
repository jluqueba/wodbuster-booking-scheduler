"""Unit tests for :func:`run_heartbeat_tick` (US4.1 scheduler slice).

Drives the tick through a fake session factory + fake probe so no real
Postgres and no APScheduler thread are involved. Focus is the tick's
orchestration contract, not the probe internals (those are covered by
``tests/component/test_heartbeat_probe.py``).
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from wodbuster_worker.heartbeat.probe import HeartbeatOutcome, NoCookieOnFile
from wodbuster_worker.scheduler.heartbeat_tick import run_heartbeat_tick


class _FakeSession:
    """Minimal session shape — the tick treats it as opaque.

    Since slice 3 wires the alert evaluator into the tick, the fake
    also has to satisfy the two SQL entry points the evaluator uses:
    ``scalar`` (for ``compute_next_window`` and ``_open_alert``) and
    ``execute`` (for the SchedulerRule list). Returning ``None`` /
    empty for both keeps the alert flow at the ``NoOp`` branch, which
    is what a tick-orchestration unit test wants: no alert-side work
    should happen; the tick's job is only to route outcomes.
    """

    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...

    def scalar(self, _stmt: Any) -> Any:
        return None

    def execute(self, _stmt: Any) -> Any:
        # Duck-typed ``Result`` returning an empty scalars iterator so
        # the SchedulerRule query in ``compute_next_window`` yields no
        # rules and the evaluator drops to NoOp.
        class _EmptyResult:
            def scalars(self) -> Any:
                class _EmptyScalars:
                    def all(self) -> list[Any]:
                        return []

                return _EmptyScalars()

        return _EmptyResult()

    def add(self, _obj: Any) -> None:
        # Not exercised in this file (the NoOp path never adds rows),
        # but included so a future test that flips the fake into an
        # Emit path gets a clear ``AssertionError`` rather than a
        # confusing ``AttributeError``.
        raise AssertionError("_FakeSession.add called; extend the fake")

    def get(self, _cls: Any, _pk: Any) -> Any:
        return None

    def flush(self) -> None: ...


class _FakeSessionFactory:
    """Yields a fresh :class:`_FakeSession` per context enter.

    Tracks how many contexts were opened so tests can assert the tick
    used one session per operator (isolation invariant).
    """

    def __init__(self) -> None:
        self.opens: int = 0

    @contextmanager
    def __call__(self):
        self.opens += 1
        yield _FakeSession()


class _FakeProbe:
    """Scripted probe. Each ``run`` call consumes the next scripted item.

    Items are either an outcome (returned) or an exception (raised).
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[int] = []

    def run(self, session: Any, operator_id: int) -> HeartbeatOutcome:
        self.calls.append(operator_id)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _outcome(operator_id: int, result: str = "valid") -> HeartbeatOutcome:
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    return HeartbeatOutcome(
        operator_id=operator_id,
        reading_id=100 + operator_id,
        result=result,  # type: ignore[arg-type]
        probed_at=now,
        projected_ttl_at=now + timedelta(days=30),
    )


def _ids(fixed: Iterable[int]):
    """Return an operator-id source that yields ``fixed`` ignoring the session."""
    fixed_list = list(fixed)

    def source(_session: Any) -> Iterable[int]:
        yield from fixed_list

    return source


def test_tick_runs_probe_for_every_operator_in_order() -> None:
    probe = _FakeProbe([_outcome(1), _outcome(2), _outcome(3)])
    factory = _FakeSessionFactory()

    outcomes = run_heartbeat_tick(
        probe,
        factory,
        operator_ids=_ids([1, 2, 3]),  # type: ignore[arg-type]
    )

    assert probe.calls == [1, 2, 3]
    assert [o.operator_id for o in outcomes] == [1, 2, 3]
    # One session for the id-enumeration + one per probe = 4 opens.
    assert factory.opens == 4


def test_tick_skips_operators_without_a_cookie() -> None:
    probe = _FakeProbe([NoCookieOnFile(1), _outcome(2), NoCookieOnFile(3), _outcome(4)])
    factory = _FakeSessionFactory()

    outcomes = run_heartbeat_tick(
        probe,
        factory,
        operator_ids=_ids([1, 2, 3, 4]),  # type: ignore[arg-type]
    )

    # Skipped operators produce no outcome and no error.
    assert [o.operator_id for o in outcomes] == [2, 4]
    assert probe.calls == [1, 2, 3, 4]


def test_tick_isolates_failures_across_operators() -> None:
    # Operator 2 blows up but 1 and 3 still probe successfully.
    probe = _FakeProbe([_outcome(1), RuntimeError("simulated"), _outcome(3)])
    factory = _FakeSessionFactory()

    outcomes = run_heartbeat_tick(
        probe,
        factory,
        operator_ids=_ids([1, 2, 3]),  # type: ignore[arg-type]
    )

    # Failed operator absent; the others complete.
    assert [o.operator_id for o in outcomes] == [1, 3]
    assert probe.calls == [1, 2, 3]


def test_tick_with_no_operators_returns_empty_list() -> None:
    probe = _FakeProbe([])
    factory = _FakeSessionFactory()

    outcomes = run_heartbeat_tick(
        probe,
        factory,
        operator_ids=_ids([]),  # type: ignore[arg-type]
    )

    assert outcomes == []
    assert probe.calls == []
    # Only the id-enumeration session was opened.
    assert factory.opens == 1
