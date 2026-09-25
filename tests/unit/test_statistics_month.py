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


def test_the_picker_cannot_reach_beyond_today() -> None:
    """A month that has not happened can hold nothing."""
    assert _resolve(None).newest == TODAY
    assert _resolve("2027-06").key == "2026-09"


def test_the_picker_cannot_reach_past_the_backfill_horizon() -> None:
    """CC-033."""
    # 365 days before 2026-09-24 is 2025-09-24, so September 2025 is
    # the oldest month the backfill can ever populate.
    assert _resolve(None).oldest == date(2025, 9, 1)
    assert _resolve("2020-01").key == "2025-09"


def test_a_past_month_spans_its_own_calendar_length() -> None:
    window = _resolve("2026-05")

    assert window.start == date(2026, 5, 1)
    assert window.end == date(2026, 5, 31)


def test_a_malformed_parameter_falls_back_to_the_current_month() -> None:
    """A crafted query string is not worth a 500."""
    for value in ("", "nonsense", "2026", "2026-13-01", "abcd-ef", "2026-xx"):
        assert _resolve(value).key == "2026-09", value


def test_a_whole_day_from_the_date_picker_selects_its_month() -> None:
    """The picker posts YYYY-MM-DD; the day is not part of the question."""
    assert _resolve("2026-07-14").key == "2026-07"
    assert _resolve("2026-07-01").key == _resolve("2026-07-31").key


def test_a_bare_month_still_resolves() -> None:
    """Bookmarks and hand-typed links predate the picker."""
    assert _resolve("2026-07").key == "2026-07"


def test_february_length_follows_the_calendar() -> None:
    assert _resolve("2026-02", today=date(2026, 9, 24)).end == date(2026, 2, 28)
    assert _resolve("2024-02", today=date(2024, 9, 24)).end == date(2024, 2, 29)


def test_december_ends_on_the_last_day_of_the_year() -> None:
    window = resolve_month("2025-12", today=date(2026, 1, 15), horizon_days=HORIZON)

    assert window.end == date(2025, 12, 31)
