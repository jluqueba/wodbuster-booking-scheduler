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
from sqlalchemy import select

from ..auth.csrf import get_csrf_token
from ..auth.deps import require_session
from ..config import Settings
from ..gyms.context import active_gym_account_id
from ..gyms.service import gym_client_factory, resolve_gym_client
from ..i18n import lang_url, t
from ..persistence.cookie_store import CookieStore
from ..persistence.engine import get_session
from ..persistence.models import AttendanceDay, AttendanceRecord, GymAccount, OperatorProfile
from ..scheduler.clock import local_date_for_slot
from ..wodbuster_client.client import (
    WodBusterAuthError,
    WodBusterProtocolError,
    WodBusterTransportError,
)
from ..wodbuster_client.parsers import PointsSummary, read_points_summary
from .capture import CaptureStatus, catch_up
from .charts import drop_rate_payload, grid_payload, lead_payload, trend_payload
from .metrics import (
    Calendar,
    CancellationBands,
    CountedRecord,
    Streaks,
    abandonment,
    booking_lead_bands,
    cancellation_bands,
    counted_records,
    day_calendar,
    drop_rate_by_slot,
    monthly_trend,
    occupancy,
    streaks,
    weekday_hour_grid,
    weekly_average,
)
from .periods import (
    PERIOD_KEYS,
    POINTS_PERIOD_KEYS,
    Period,
    PeriodKey,
    resolve_period,
)
from .points import PointsModel, points_estimate

_log = structlog.get_logger(__name__)

router = APIRouter(tags=["statistics"])


@dataclass(frozen=True)
class MonthWindow:
    """The calendar month on screen, and how far it may travel.

    A month rather than a rolling window of days: a calendar whose first
    row starts mid-month is hard to read, and "the 3rd" means something
    to a reader only inside a month.

    There are no step-by-step targets. The date picker carries its own
    month and year navigation, so a pair of arrows beside it was two
    mechanisms for one job.
    """

    year: int
    month: int
    start: date
    end: date
    # Bounds for the date picker, so it cannot offer a month the
    # backfill will never populate or one that has not happened.
    oldest: date
    newest: date

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


def _first_of_month(day: date) -> date:
    return day.replace(day=1)


def _shift_month(first: date, delta: int) -> date:
    index = (first.year * 12 + first.month - 1) + delta
    return date(index // 12, index % 12 + 1, 1)


def resolve_month(requested: str | None, *, today: date, horizon_days: int) -> MonthWindow:
    """Return the month to render, clamped to what can hold data.

    Accepts ``YYYY-MM-DD`` from the date picker, which posts a whole
    day, and bare ``YYYY-MM``. The day is discarded: the calendar is a
    month, so two values naming the same month land on the same page,
    and a bookmarked link keeps working.

    An unparseable or out-of-range value falls back to the current
    month rather than raising: a crafted query string is not worth a
    500, and there is nothing here to protect beyond rendering.

    Clamped by the backfill horizon on one side and today on the other,
    so no request can render a month that will never hold anything.
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

    return MonthWindow(
        year=first.year,
        month=first.month,
        start=first,
        end=last,
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


def _read_points_summary(request: Request, gym_account_id: int) -> PointsSummary:
    """Read the gym's own points page, or return nothing at all.

    The balance and the billing period are conveniences, not the point
    of the screen. Every failure here therefore degrades to absence:
    the page renders without the points block and without the billing
    range, which is FR-031 and the same contract the capture path
    already honours.
    """
    store = getattr(request.app.state, "cookie_store", None)
    factory = gym_client_factory(request.app.state)
    if not isinstance(store, CookieStore) or factory is None:
        return PointsSummary(balance=None, period=None)

    with get_session() as session:
        resolved = resolve_gym_client(factory, session, gym_account_id)
        cookie_value = store.load(session, gym_account_id)
    if resolved is None or cookie_value is None:
        return PointsSummary(balance=None, period=None)

    client, _ = resolved
    reader = getattr(client, "load_points_page", None)
    if reader is None:
        return PointsSummary(balance=None, period=None)
    try:
        html = reader(cookie_value)
    except (WodBusterAuthError, WodBusterTransportError, WodBusterProtocolError) as exc:
        _log.info(
            "statistics.points.unavailable",
            gym_account_id=gym_account_id,
            reason=type(exc).__name__,
        )
        return PointsSummary(balance=None, period=None)
    return read_points_summary(html)


@dataclass(frozen=True)
class History:
    """One read of everything the page derives from.

    Every section slices this rather than issuing its own query. Three
    windows over the same year are three filters in Python, not three
    round trips, and the settle window and the class-change rule are
    applied once over whole days instead of once per window, so a day
    on a window boundary is classified the same way in every section.
    """

    gym_name: str
    points_override: object | None
    data_through: date | None
    any_record_ever: int
    oldest_captured: date | None
    captured: dict[date, int]
    counted: list[CountedRecord]
    excluded_weekdays: list[int]

    def window(self, period: Period) -> list[CountedRecord]:
        return [r for r in self.counted if period.start <= r.local_date <= period.end]

    def ledger(self, start: date, end: date) -> dict[date, int]:
        return {day: count for day, count in self.captured.items() if start <= day <= end}


def _read_history(
    gym_account_id: int,
    operator_id: int,
    *,
    now: datetime,
    settle_window_hours: float,
) -> History | None:
    """Load the account's whole captured history, or nothing.

    ``None`` means the account is not the acting user's. Treated by the
    caller as "no gym" rather than as an error, so nothing confirms the
    account exists (INV-006).
    """
    with get_session() as session:
        gym = session.get(GymAccount, gym_account_id)
        if gym is None or gym.user_id != operator_id:
            return None

        captured: dict[date, int] = {
            row.local_date: row.class_count
            for row in session.execute(
                select(AttendanceDay.local_date, AttendanceDay.class_count).where(
                    AttendanceDay.gym_account_id == gym_account_id
                )
            ).all()
        }
        rows = list(
            session.scalars(
                select(AttendanceRecord)
                .where(AttendanceRecord.gym_account_id == gym_account_id)
                .order_by(AttendanceRecord.start_at.asc())
            ).all()
        )
        # Whether any activity has ever been read for this account.
        # Deliberately not used to diagnose why. A gym that hides its
        # athlete lists and a user who has not booked anything yet
        # produce the identical signal, and the data minimisation
        # decision means we store nothing that would tell them apart.
        # The page therefore states the fact and stops there.
        excluded_weekdays = list(
            session.scalar(
                select(OperatorProfile.statistics_excluded_weekdays).where(
                    OperatorProfile.id == operator_id
                )
            )
            or []
        )
        return History(
            gym_name=str(gym.display_name),
            points_override=gym.points_model,
            data_through=max(captured, default=None),
            any_record_ever=len(rows),
            oldest_captured=min(captured, default=None),
            captured=captured,
            counted=counted_records(rows, now=now, settle_window_hours=settle_window_hours),
            excluded_weekdays=excluded_weekdays,
        )


def _known(value: str | None, allowed: tuple[PeriodKey, ...]) -> str | None:
    """Return ``value`` only when the section actually offers it."""
    return value if value in allowed else None


def _period_options(keys: tuple[PeriodKey, ...], selected: Period) -> list[dict[str, object]]:
    return [
        {"key": key, "label": t(f"statistics.period.{key}"), "current": key == selected.key}
        for key in keys
    ]


def _attendance_context(
    history: History,
    *,
    requested: str | None,
    today: date,
    horizon_days: int,
    model: PointsModel,
) -> dict[str, object]:
    """What the reader did in the selected window.

    Streaks are bounded by the window like everything else here. A run
    that reaches its edge is reported as a lower bound rather than
    truncated, which is the same rule that already covers the edge of
    captured history: the reading is missing, not the training.
    """
    period = resolve_period(
        requested,
        today=today,
        horizon_days=horizon_days,
        oldest_captured=history.oldest_captured,
        allowed=PERIOD_KEYS,
    )
    records = history.window(period)
    dropouts = abandonment(records)
    bands = cancellation_bands(
        records,
        late_hours=model.late_hours,
        very_late_hours=model.very_late_hours,
    )
    runs = streaks(
        records,
        captured=history.ledger(period.start, period.end),
        today=today,
        excluded_weekdays=history.excluded_weekdays,
    )
    pace = weekly_average(
        history.counted,
        captured=history.captured.keys(),
        start=period.start,
        end=period.end,
    )
    return {
        "period": period,
        "period_label": t(f"statistics.period.{period.key}"),
        "period_options": _period_options(PERIOD_KEYS, period),
        "abandonment": dropouts,
        "bands": bands,
        "band_labels": _band_labels(bands),
        "streak_cards": _streak_cards(runs),
        "pace": pace,
        "excluded_weekday_labels": [
            t(f"day.{_WEEKDAY_KEYS[day]}") for day in sorted(history.excluded_weekdays)
        ],
    }


def _points_context(
    history: History,
    *,
    requested: str | None,
    today: date,
    horizon_days: int,
    model: PointsModel,
    summary: PointsSummary,
) -> dict[str, object]:
    """What the reader's behaviour cost, in the gym's own economy."""
    period = resolve_period(
        requested,
        today=today,
        horizon_days=horizon_days,
        oldest_captured=history.oldest_captured,
        billing=summary.period,
        allowed=POINTS_PERIOD_KEYS,
    )
    estimate = points_estimate(history.window(period), model=model)
    return {
        "period": period,
        "period_label": t(f"statistics.period.{period.key}"),
        "period_options": _period_options(
            POINTS_PERIOD_KEYS if summary.period else PERIOD_KEYS, period
        ),
        "estimate": estimate,
        "balance": summary.balance,
        "billing_period": summary.period,
        "billing_label": (
            t(
                "statistics.period.billing.range",
                start=_date_label(summary.period[0]),
                end=_date_label(summary.period[1]),
            )
            if summary.period
            else None
        ),
        "assumptions": [
            t(
                f"statistics.points.assumption.{key}",
                hours=_hours_label(model.late_hours),
                count=estimate.filed_as_absent,
            )
            for key in estimate.assumptions
        ],
    }


def _patterns_context(
    history: History,
    *,
    requested: str | None,
    today: date,
    horizon_days: int,
    model: PointsModel,
) -> dict[str, object]:
    """When the reader trains and which bookings they keep."""
    period = resolve_period(
        requested,
        today=today,
        horizon_days=horizon_days,
        oldest_captured=history.oldest_captured,
        allowed=PERIOD_KEYS,
    )
    records = history.window(period)
    return {
        "period": period,
        "period_label": t(f"statistics.period.{period.key}"),
        "period_options": _period_options(PERIOD_KEYS, period),
        **_chart_context(records, period, model),
    }


def _calendar_context(
    history: History,
    *,
    month: str | None,
    today: date,
    horizon_days: int,
) -> dict[str, object]:
    """The month on screen, governed by its own picker and nothing else.

    Deliberately outside every period filter. A calendar is a month by
    nature, so a control offering it thirty days or a year would be
    offering it something it cannot draw.
    """
    window = resolve_month(month, today=today, horizon_days=horizon_days)
    calendar = day_calendar(
        [r for r in history.counted if window.start <= r.local_date <= window.end],
        captured=history.ledger(window.start, window.end),
        start=window.start,
        end=window.end,
        today=today,
    )
    # Days of this month that are over and still unread. The cells say
    # so one by one; this tells the reader it is worth coming back
    # rather than concluding the month was empty.
    unread = sum(
        1
        for cell in (c for week in calendar.weeks for c in week if c is not None)
        if cell.status == "uncaptured"
    )
    return {
        "month": window,
        "month_label": _month_label(window),
        "calendar": calendar,
        "cell_lines": _cell_lines(calendar),
        "weekday_labels": _weekday_labels(),
        "legend": _legend(),
        "unread_days": unread,
    }


def _resolve_model(settings: Settings, override: object | None) -> PointsModel:
    """One model governs every figure that depends on the gym's tiers.

    Resolved before anything reads a threshold: charging a cancellation
    under one boundary while labelling it under another is the kind of
    disagreement a reader cannot diagnose.
    """
    return PointsModel.resolve(
        base_cost=settings.statistics_base_point_cost,
        late_penalty=settings.statistics_late_cancel_penalty,
        very_late_penalty=settings.statistics_very_late_cancel_penalty,
        absence_penalty=settings.statistics_absence_penalty,
        late_hours=settings.statistics_late_cancel_hours,
        very_late_hours=settings.statistics_very_late_cancel_hours,
        override=override,
    )


def _build_context(
    request: Request,
    operator_id: int,
    *,
    month: str | None,
    attendance: str | None,
    points: str | None,
    patterns: str | None,
    section: str | None = None,
) -> dict[str, object]:
    """Assemble the page, or one section of it.

    ``section`` names the fragment a filter click asked for. Capture is
    skipped for it and so is the points page unless that is the
    section: a click on "3 months" must not spend a reader's request on
    thirty five upstream calls to re-read days already in the ledger.
    """
    now = datetime.now(tz=UTC)
    gym_account_id = active_gym_account_id(request)
    if gym_account_id is None:
        return _empty_context(request, has_gym=False)

    settings = _settings(request)
    today = local_date_for_slot(now)
    horizon = settings.statistics_backfill_days
    window = resolve_month(month, today=today, horizon_days=horizon)

    capture_status: CaptureStatus = "complete"
    if section is None:
        # The month is resolved first so capture can prioritise it.
        # Opening January must read January, not the fortnight the
        # reader already has on the current month's page.
        capture_status = _run_catch_up(
            request, gym_account_id, now, (window.start, min(window.end, today))
        )

    history = _read_history(
        gym_account_id,
        operator_id,
        now=now,
        settle_window_hours=settings.statistics_settle_window_hours,
    )
    if history is None:
        return _empty_context(request, has_gym=False)

    summary = PointsSummary(balance=None, period=None)
    if section in (None, "points"):
        summary = _read_points_summary(request, gym_account_id)

    model = _resolve_model(settings, history.points_override)
    context: dict[str, object] = {
        "csrf_token": get_csrf_token(request) or "",
        "has_gym": True,
        "gym_name": history.gym_name,
        "never_captured": history.data_through is None,
        "capture_notice": _capture_notice(capture_status),
        "cookie_url": lang_url("/cookie"),
        "no_activity_ever": history.data_through is not None and history.any_record_ever == 0,
        "section": section,
        # Echoed into every filter form so submitting one window never
        # resets the other two. Sanitised rather than passed through:
        # an unknown value is dropped and the server falls back, which
        # keeps a crafted query string out of the rendered markup.
        "filters": {
            "month": window.key,
            "attendance": _known(attendance, PERIOD_KEYS),
            "points": _known(points, POINTS_PERIOD_KEYS),
            "patterns": _known(patterns, PERIOD_KEYS),
        },
    }
    if section in (None, "attendance"):
        context["attendance"] = _attendance_context(
            history,
            requested=attendance,
            today=today,
            horizon_days=horizon,
            model=model,
        )
    if section in (None, "points"):
        context["points_block"] = _points_context(
            history,
            requested=points,
            today=today,
            horizon_days=horizon,
            model=model,
            summary=summary,
        )
    if section in (None, "patterns"):
        context["patterns"] = _patterns_context(
            history,
            requested=patterns,
            today=today,
            horizon_days=horizon,
            model=model,
        )
    if section is None:
        context["calendar_block"] = _calendar_context(
            history, month=month, today=today, horizon_days=horizon
        )
    return context


# An hour with one or two bookings produces a rate that swings between
# 0 and 100 percent on a single decision. The floor is what keeps the
# chart from inviting conclusions the data cannot carry.
_MIN_BOOKINGS_PER_HOUR = 5


def _chart_context(
    records: list[CountedRecord],
    period: Period,
    model: PointsModel,
) -> dict[str, object]:
    """Everything the chart block needs, for the selected period.

    Takes the resolved :class:`PointsModel` rather than the settings,
    so the boundary a chart draws is the same one the estimate charged.
    """
    short_days = [t(f"day.short.{key}") for key in _WEEKDAY_KEYS]
    grid = weekday_hour_grid(records)
    hours = drop_rate_by_slot(records, min_bookings=_MIN_BOOKINGS_PER_HOUR)
    trend = monthly_trend(records)
    lead = booking_lead_bands(records, free_hours=model.late_hours)
    free = _hours_label(model.late_hours)

    return {
        "occupancy": occupancy(records),
        "charts": [
            {
                "id": "wb-chart-grid",
                "title": t("statistics.chart.grid.title"),
                "hint": t("statistics.chart.grid.hint"),
                "size": "tall",
                "payload": grid_payload(
                    grid,
                    weekday_labels=short_days,
                    strings={"cell": t("statistics.chart.grid.cell")},
                ),
                "columns": (t("statistics.chart.grid.slot"), t("statistics.table.count")),
                "rows": [
                    (f"{short_days[cell.weekday]} {cell.slot}", str(cell.attended)) for cell in grid
                ],
            },
            {
                "id": "wb-chart-drop",
                "title": t("statistics.chart.drop.title"),
                "hint": t("statistics.chart.drop.hint", min=_MIN_BOOKINGS_PER_HOUR),
                "size": "medium",
                "payload": drop_rate_payload(hours, strings={"of": t("statistics.chart.drop.of")}),
                "columns": (
                    t("statistics.chart.drop.hour"),
                    t("statistics.chart.drop.rate"),
                ),
                "rows": [
                    (
                        stat.slot,
                        "{}% {}".format(
                            round(100 * (stat.drop_rate or 0)),
                            t("statistics.chart.drop.of")
                            .replace("{dropped}", str(stat.cancelled))
                            .replace("{booked}", str(stat.booked)),
                        ),
                    )
                    for stat in hours
                ],
            },
            {
                "id": "wb-chart-trend",
                "title": t("statistics.chart.trend.title"),
                "hint": t("statistics.chart.trend.hint"),
                "size": "medium",
                "payload": trend_payload(
                    trend,
                    strings={
                        "attended": t("statistics.attended.tile"),
                        "cancelled": t("statistics.cancelled.tile"),
                    },
                ),
                "columns": (
                    t("statistics.chart.trend.month"),
                    t("statistics.chart.trend.split"),
                ),
                "rows": [(point.key, f"{point.attended} / {point.cancelled}") for point in trend],
            },
            {
                "id": "wb-chart-lead",
                "title": t("statistics.chart.lead.title"),
                "hint": t("statistics.chart.lead.hint", hours=free),
                "size": "short",
                "payload": lead_payload(
                    lead,
                    labels=[
                        t("statistics.chart.lead.same_day", hours=free),
                        t("statistics.chart.lead.within_day", hours=free),
                        t("statistics.chart.lead.early"),
                    ],
                    strings={"unit": t("statistics.chart.lead.unit")},
                ),
                "columns": (t("statistics.chart.lead.band"), t("statistics.table.count")),
                "rows": [
                    (t("statistics.chart.lead.same_day", hours=free), str(lead.same_day)),
                    (
                        t("statistics.chart.lead.within_day", hours=free),
                        str(lead.within_day),
                    ),
                    (t("statistics.chart.lead.early"), str(lead.early)),
                ],
            },
        ],
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


def _date_label(day: date) -> str:
    return t(
        "statistics.date",
        day=day.day,
        month=t(f"month.{_MONTH_KEYS[day.month - 1]}"),
        year=day.year,
    )


def _streak_cards(runs: Streaks) -> list[dict[str, object]]:
    """Describe both runs in words the reader can check on the calendar.

    A run that reached the oldest captured day is reported as "at
    least", because the history behind it was never read and claiming
    a exact number there would be claiming a fact nobody measured.
    """
    cards: list[dict[str, object]] = []
    for key, run, provisional in (
        ("current", runs.current, False),
        ("longest", runs.longest, runs.longest_is_provisional),
    ):
        if run.days == 0:
            value = t("statistics.streak.none")
            hint = ""
        else:
            count = (
                t("statistics.streak.at_least", days=run.days)
                if run.is_lower_bound
                else str(run.days)
            )
            value = (
                t("statistics.streak.one_day")
                if run.days == 1 and not run.is_lower_bound
                else t("statistics.streak.days", days=count)
            )
            hint = (
                t(
                    "statistics.streak.range",
                    start=_date_label(run.start),
                    end=_date_label(run.end),
                )
                if run.start and run.end
                else ""
            )
        cards.append(
            {
                "label": t(f"statistics.streak.{key}.tile"),
                "value": value,
                "hint": hint,
                "provisional": provisional and run.days > 0,
            }
        )
    return cards


def _legend() -> list[tuple[str, str]]:
    return [(key, t(f"statistics.legend.{key}")) for key in _LEGEND_KEYS]


def _band_labels(bands: CancellationBands) -> list[dict[str, object]]:
    """Describe each notice band in the gym's own terms.

    The thresholds are interpolated rather than written into the
    catalog, so a gym with different penalty tiers gets sentences that
    match its own rules instead of Antwork's.
    """
    late = _hours_label(bands.late_hours)
    very_late = _hours_label(bands.very_late_hours)
    return [
        {
            "kind": "early",
            "label": t("statistics.bands.early", hours=late),
            "count": bands.early,
        },
        {
            "kind": "late",
            "label": t("statistics.bands.late", upper=late, lower=very_late),
            "count": bands.late,
        },
        {
            "kind": "very_late",
            "label": t("statistics.bands.very_late", hours=very_late),
            "count": bands.very_late,
        },
    ]


def _hours_label(hours: float) -> str:
    """Render a threshold without a trailing zero on whole hours."""
    return str(int(hours)) if hours == int(hours) else str(hours)


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
        "capture_notice": None,
        "cookie_url": lang_url("/cookie"),
        "no_activity_ever": False,
        "section": None,
        "filters": {},
    }


# Which fragment a filter click may ask for. A value outside this set
# renders the whole page, so a crafted query string gets a valid
# response rather than a 500 or a partial nobody asked for.
_SECTION_TEMPLATES = {
    "attendance": "statistics/_attendance.html",
    "points": "statistics/_points.html",
    "patterns": "statistics/_patterns.html",
}


@router.get("/statistics", name="statistics")
def statistics(
    request: Request,
    operator_id: int = Depends(require_session),
    month: str | None = None,
    attendance: str | None = None,
    points: str | None = None,
    patterns: str | None = None,
    section: str | None = None,
) -> Response:
    """Render the statistics page, or one section of it.

    ``month`` selects the calendar month as ``YYYY-MM``. The other
    three select the window their own section describes, independently:
    a reader comparing this month's drop-out rate against a year of
    training patterns is asking two questions, and one control for both
    would force them to choose.

    ``section`` asks for a single block, which is how a filter click
    replaces its own numbers without rebuilding the page around them.
    Nothing here carries authority: the gym account still comes from
    the session, so every parameter can only move the reader within
    their own history.
    """
    template = _SECTION_TEMPLATES.get(section or "")
    context = _build_context(
        request,
        operator_id,
        month=month,
        attendance=attendance,
        points=points,
        patterns=patterns,
        section=section if template else None,
    )
    return _templates(request).TemplateResponse(
        request=request,
        name=template or "statistics/page.html",
        context=context,
    )


__all__ = ["router"]
