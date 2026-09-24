"""Unit tests for the calendar month window (statistics navigation).

Pure arithmetic over dates, so the boundaries that matter, the horizon
and today, are asserted directly rather than through a rendered page.
"""

from __future__ import annotations

from datetime import date

from wodbuster_worker.statistics.routes import resolve_month

TODAY = date(2026, 9, 24)
HORIZON = 365


def _resolve(requested: str | None, *, today: date = TODAY):
    return resolve_month(requested, today=today, horizon_days=HORIZON)


def test_no_parameter_selects_the_current_month() -> None:
    window = _resolve(None)

    assert (window.year, window.month) == (2026, 9)
    assert window.start == date(2026, 9, 1)
    assert window.end == date(2026, 9, 30)


def test_the_current_month_cannot_step_forward() -> None:
    """The next arrow never offers a month that can hold nothing."""
    assert _resolve(None).next is None
    assert _resolve(None).previous == "2026-08"


def test_a_past_month_can_step_both_ways() -> None:
    window = _resolve("2026-05")

    assert window.start == date(2026, 5, 1)
    assert window.end == date(2026, 5, 31)
    assert window.previous == "2026-04"
    assert window.next == "2026-06"


def test_stepping_back_crosses_a_year_boundary() -> None:
    window = _resolve("2026-01")

    assert window.previous == "2025-12"


def test_the_oldest_month_cannot_step_back_past_the_horizon() -> None:
    # 365 days before 2026-09-24 is 2025-09-24, so September 2025 is
    # the oldest month the backfill can ever populate.
    window = _resolve("2025-09")

    assert window.previous is None


def test_a_month_beyond_the_horizon_is_clamped_rather_than_rejected() -> None:
    assert _resolve("2020-01").key == "2025-09"


def test_a_future_month_is_clamped_to_the_current_one() -> None:
    assert _resolve("2027-06").key == "2026-09"


def test_a_malformed_parameter_falls_back_to_the_current_month() -> None:
    """A crafted query string is not worth a 500."""
    for value in ("", "nonsense", "2026", "2026-13-01", "abcd-ef", "2026-xx"):
        assert _resolve(value).key == "2026-09", value


def test_a_whole_day_from_the_date_picker_selects_its_month() -> None:
    """The picker posts YYYY-MM-DD; the day is not part of the question."""
    assert _resolve("2026-07-14").key == "2026-07"
    assert _resolve("2026-07-01").key == _resolve("2026-07-31").key


def test_february_length_follows_the_calendar() -> None:
    assert _resolve("2026-02", today=date(2026, 9, 24)).end == date(2026, 2, 28)
    assert _resolve("2024-02", today=date(2024, 9, 24)).end == date(2024, 2, 29)


def test_december_steps_into_the_next_year() -> None:
    window = resolve_month("2025-12", today=date(2026, 1, 15), horizon_days=HORIZON)

    assert window.end == date(2025, 12, 31)
    assert window.next == "2026-01"
