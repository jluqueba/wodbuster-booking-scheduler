"""Statistics routes (Attendance Statistics, slice 1).

Two properties hold for every handler here and are worth stating once:

- the gym account comes from the session through
  :func:`active_gym_account_id`, never from a request parameter, so a
  record belonging to another account is unreachable rather than merely
  hidden (FR-039, INV-006);
- the handler is a read. Capture issues upstream GET requests and
  writes the operator's own rows; nothing here books, cancels, or
  mutates anything the user owns, so no CSRF token is required.

Rendering degrades rather than fails. A rejected cookie, an unreachable
gym or a missing client stack all produce a page built from whatever is
already stored, because a statistics screen with slightly old numbers is
useful and a 500 is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from ..auth.csrf import get_csrf_token
from ..auth.deps import require_session
from ..config import Settings
from ..gyms.context import active_gym_account_id
from ..gyms.service import gym_client_factory, resolve_gym_client
from ..i18n import lang_url, t
from ..persistence.cookie_store import CookieStore
from ..persistence.engine import get_session
from ..persistence.models import AttendanceDay, AttendanceRecord, GymAccount
from ..scheduler.clock import local_date_for_slot
from .capture import CaptureStatus, catch_up
from .metrics import Calendar, counted_records, day_calendar

_log = structlog.get_logger(__name__)

router = APIRouter(tags=["statistics"])


@dataclass(frozen=True)
class MonthWindow:
    """The calendar month on screen, and where it can move to.

    A month rather than a rolling window of days: a calendar whose first
    row starts mid-month is hard to read, and "the 3rd" means something
    to a reader only inside a month.
    """

    year: int
    month: int
    start: date
    end: date
    previous: str | None
    next: str | None
    # Bounds for the date picker, so it cannot offer a month the
    # backfill will never populate or one that has not happened.
    oldest: date
    newest: date

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


def _month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _first_of_month(day: date) -> date:
    return day.replace(day=1)


def _shift_month(first: date, delta: int) -> date:
    index = (first.year * 12 + first.month - 1) + delta
    return date(index // 12, index % 12 + 1, 1)


def resolve_month(requested: str | None, *, today: date, horizon_days: int) -> MonthWindow:
    """Return the month to render and its navigation targets.

    Accepts ``YYYY-MM`` from the arrows and ``YYYY-MM-DD`` from the date
    picker, which posts a whole day. The day is discarded: the calendar
    is a month, so two values that name the same month must land on the
    same page.

    An unparseable or out-of-range value falls back to the current
    month rather than raising: a crafted query string is not worth a
    500, and there is nothing here to protect beyond rendering.

    Navigation is bounded by the backfill horizon on one side and by
    today on the other, so the arrows never offer a month that can
    hold nothing.
    """
    current_first = _first_of_month(today)
    oldest_first = _first_of_month(today - timedelta(days=horizon_days))

    first = current_first
    if requested:
        parts = requested.split("-")
        try:
            candidate = date(int(parts[0]), int(parts[1]), 1)
        except (ValueError, TypeError, IndexError):
            candidate = current_first
        first = min(max(candidate, oldest_first), current_first)

    last = _shift_month(first, 1) - timedelta(days=1)
    previous_first = _shift_month(first, -1)
    next_first = _shift_month(first, 1)

    return MonthWindow(
        year=first.year,
        month=first.month,
        start=first,
        end=last,
        previous=_month_key(previous_first) if previous_first >= oldest_first else None,
        next=_month_key(next_first) if next_first <= current_first else None,
        oldest=oldest_first,
        newest=today,
    )


def _templates(request: Request) -> Jinja2Templates:
    templates = request.app.state.templates
    if not isinstance(templates, Jinja2Templates):  # pragma: no cover - wiring guard
        raise RuntimeError("templates not configured")
    return templates


def _settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if isinstance(settings, Settings):
        return settings  # pragma: no cover - always wired in the real app
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _run_catch_up(
    request: Request,
    gym_account_id: int,
    now: datetime,
    priority: tuple[date, date] | None,
) -> CaptureStatus:
    """Capture the days worth reading, within the request budget.

    ``priority`` is the month on screen. Without it a visit to an
    unread month would spend its whole budget on recent days the reader
    is not looking at, and the month would stay blank however many
    times they opened it.

    Every failure mode ends the same way: fewer captured days and a page
    rendered from what is already stored. Nothing raised here reaches
    the user, because the data the page needs may already be in the
    database. What is returned is why it stopped, so the page can say
    something true instead of silently showing old numbers.
    """
    settings = _settings(request)
    store = getattr(request.app.state, "cookie_store", None)
    factory = gym_client_factory(request.app.state)
    if not isinstance(store, CookieStore) or factory is None:
        _log.info("statistics.capture.no_client_stack", gym_account_id=gym_account_id)
        return "gym_unreachable"

    with get_session() as session:
        resolved = resolve_gym_client(factory, session, gym_account_id)
        cookie_value = store.load(session, gym_account_id)
    if resolved is None or cookie_value is None:
        _log.info("statistics.capture.no_cookie", gym_account_id=gym_account_id)
        return "cookie_rejected"

    client, idu = resolved
    result = catch_up(
        gym_account_id=gym_account_id,
        client=client,
        cookie_value=cookie_value,
        operator_idu=idu,
        cap=settings.statistics_capture_cap_per_request,
        horizon_days=settings.statistics_backfill_days,
        budget_seconds=settings.statistics_capture_budget_seconds,
        priority=priority,
        now=now,
    )
    _log.info(
        "statistics.capture.catch_up",
        gym_account_id=gym_account_id,
        captured=len(result.captured),
        remaining=result.remaining,
        status=result.status,
    )
    return result.status


def _build_context(request: Request, operator_id: int, month: str | None) -> dict[str, object]:
    now = datetime.now(tz=UTC)
    gym_account_id = active_gym_account_id(request)
    if gym_account_id is None:
        return _empty_context(request, has_gym=False)

    settings = _settings(request)
    today = local_date_for_slot(now)
    window = resolve_month(
        month,
        today=today,
        horizon_days=settings.statistics_backfill_days,
    )
    start, end = window.start, window.end

    # The month is resolved first so capture can prioritise it. Opening
    # January must read January, not the fortnight the reader already
    # has on the current month's page.
    capture_status = _run_catch_up(request, gym_account_id, now, (start, min(end, today)))

    with get_session() as session:
        gym = session.get(GymAccount, gym_account_id)
        if gym is None or gym.user_id != operator_id:
            # Not reachable through the switcher, which only offers the
            # acting user's own accounts. Treated as "no gym" rather
            # than as an error, so nothing confirms the account exists.
            return _empty_context(request, has_gym=False)
        gym_name = str(gym.display_name)

        data_through = session.scalar(
            select(func.max(AttendanceDay.local_date)).where(
                AttendanceDay.gym_account_id == gym_account_id
            )
        )
        # Whether any activity has ever been read for this account.
        # Deliberately not used to diagnose why. A gym that hides its
        # athlete lists and a user who has not booked anything yet
        # produce the identical signal, and the data minimisation
        # decision means we store nothing that would tell them apart.
        # The page therefore states the fact and stops there.
        any_record_ever = (
            session.scalar(
                select(func.count())
                .select_from(AttendanceRecord)
                .where(AttendanceRecord.gym_account_id == gym_account_id)
            )
            or 0
        )
        captured: dict[date, int] = {
            row.local_date: row.class_count
            for row in session.execute(
                select(AttendanceDay.local_date, AttendanceDay.class_count).where(
                    AttendanceDay.gym_account_id == gym_account_id,
                    AttendanceDay.local_date >= start,
                    AttendanceDay.local_date <= end,
                )
            ).all()
        }
        records = list(
            session.scalars(
                select(AttendanceRecord)
                .where(
                    AttendanceRecord.gym_account_id == gym_account_id,
                    AttendanceRecord.local_date >= start,
                    AttendanceRecord.local_date <= end,
                )
                .order_by(AttendanceRecord.start_at.asc())
            ).all()
        )

    counted = counted_records(
        records,
        now=now,
        settle_window_hours=settings.statistics_settle_window_hours,
    )
    calendar = day_calendar(counted, captured=captured, start=start, end=end, today=today)

    # Days of this month that are over and still unread. The cells say
    # so one by one; this tells the reader it is worth coming back
    # rather than concluding the month was empty.
    unread = sum(
        1
        for cell in (c for week in calendar.weeks for c in week if c is not None)
        if cell.status == "uncaptured"
    )

    return {
        "csrf_token": get_csrf_token(request) or "",
        "has_gym": True,
        "gym_name": gym_name,
        "never_captured": data_through is None,
        "unread_days": unread,
        "capture_notice": _capture_notice(capture_status),
        "cookie_url": lang_url("/cookie"),
        "no_activity_ever": data_through is not None and any_record_ever == 0,
        "month": window,
        "month_label": _month_label(window),
        "calendar": calendar,
        "weekday_labels": _weekday_labels(),
        "cell_lines": _cell_lines(calendar),
        "legend": _legend(),
    }


_WEEKDAY_KEYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

_MONTH_KEYS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)

# Every state a cell can legitimately hold, so the reader never meets a
# colour the legend does not explain. "Future" is omitted: an empty
# cell for a day that has not happened needs no explanation.
_LEGEND_KEYS = (
    "attended",
    "cancelled",
    "swapped",
    "no_activity",
    "closed",
    "uncaptured",
)


def _weekday_labels() -> list[dict[str, str]]:
    return [{"short": t(f"day.short.{key}"), "long": t(f"day.{key}")} for key in _WEEKDAY_KEYS]


def _month_label(window: MonthWindow) -> str:
    return t(
        "statistics.month.label",
        month=t(f"month.{_MONTH_KEYS[window.month - 1]}"),
        year=window.year,
    )


def _legend() -> list[tuple[str, str]]:
    return [(key, t(f"statistics.legend.{key}")) for key in _LEGEND_KEYS]


def _capture_notice(status: CaptureStatus) -> str | None:
    """Return what to tell the reader about the last capture attempt.

    A completed pass says nothing: a page that announces its own
    success on every visit trains the reader to ignore the line where
    the failure will eventually appear. A budget that ran out is
    covered by the per-day unread count, which is more precise than a
    sentence.
    """
    if status == "cookie_rejected":
        return t("statistics.capture.cookie_rejected")
    if status == "gym_unreachable":
        return t("statistics.capture.gym_unreachable")
    return None


def _cell_lines(calendar: Calendar) -> dict[str, list[dict[str, str]]]:
    """Resolve what each cell says, one line per thing that happened.

    A day holds classes; a cell holds one colour. Listing every outcome
    is what lets a reader add up the grid and reach the same figures as
    the tiles, which is the difference between a number they check and
    a number they distrust.

    Resolved here rather than in the template so the catalog lookups
    are not spread across a nested loop, and so a missing key surfaces
    as a plain string rather than as a template error.
    """
    lines: dict[str, list[dict[str, str]]] = {}
    for week in calendar.weeks:
        for cell in week:
            if cell is None:
                continue
            entries: list[dict[str, str]] = [
                {"kind": "attended", "text": name} for name in cell.attended_names
            ]
            for kind, count in (
                ("swapped", cell.swapped),
                ("cancelled", cell.cancelled),
                ("removed_after_start", cell.removed_after_start),
                ("no_show", cell.no_show),
            ):
                if not count:
                    continue
                label = t(f"statistics.calendar.{kind}")
                entries.append({"kind": kind, "text": f"{label} x{count}" if count > 1 else label})
            if not entries:
                text = t(f"statistics.calendar.{cell.status}")
                if text:
                    entries.append({"kind": cell.status, "text": text})
            lines[cell.day.isoformat()] = entries
    return lines


def _empty_context(request: Request, *, has_gym: bool) -> dict[str, object]:
    return {
        "csrf_token": get_csrf_token(request) or "",
        "has_gym": has_gym,
        "gym_name": None,
        "never_captured": False,
        "unread_days": 0,
        "capture_notice": None,
        "cookie_url": lang_url("/cookie"),
        "no_activity_ever": False,
        "month": None,
        "month_label": None,
    }


@router.get("/statistics", name="statistics")
def statistics(
    request: Request,
    operator_id: int = Depends(require_session),
    month: str | None = None,
) -> Response:
    """Render the statistics page for the active gym account.

    ``month`` selects the calendar month as ``YYYY-MM``. It carries no
    authority: the gym account still comes from the session, so the
    parameter can only move the reader within their own history.
    """
    return _templates(request).TemplateResponse(
        request=request,
        name="statistics/page.html",
        context=_build_context(request, operator_id, month),
    )


__all__ = ["router"]
