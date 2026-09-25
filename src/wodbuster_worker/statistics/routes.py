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
from .periods import PERIOD_KEYS, Period, resolve_period
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


def _build_context(
    request: Request, operator_id: int, month: str | None, period_key: str | None
) -> dict[str, object]:
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
    points = _read_points_summary(request, gym_account_id)

    with get_session() as session:
        gym = session.get(GymAccount, gym_account_id)
        if gym is None or gym.user_id != operator_id:
            # Not reachable through the switcher, which only offers the
            # acting user's own accounts. Treated as "no gym" rather
            # than as an error, so nothing confirms the account exists.
            return _empty_context(request, has_gym=False)
        gym_name = str(gym.display_name)
        # Read inside the session: the estimate is computed after it
        # closes, and a detached instance would raise on attribute
        # access. NULL means "use the application defaults".
        points_override = gym.points_model

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
        oldest_captured = session.scalar(
            select(func.min(AttendanceDay.local_date)).where(
                AttendanceDay.gym_account_id == gym_account_id
            )
        )
        # The whole ledger and the whole record set, for the streaks.
        # A run is not bounded by the window on screen.
        all_captured: dict[date, int] = {
            row.local_date: row.class_count
            for row in session.execute(
                select(AttendanceDay.local_date, AttendanceDay.class_count).where(
                    AttendanceDay.gym_account_id == gym_account_id
                )
            ).all()
        }
        all_rows = list(
            session.scalars(
                select(AttendanceRecord).where(AttendanceRecord.gym_account_id == gym_account_id)
            ).all()
        )
        excluded_weekdays = list(
            session.scalar(
                select(OperatorProfile.statistics_excluded_weekdays).where(
                    OperatorProfile.id == operator_id
                )
            )
            or []
        )
        period = resolve_period(
            period_key,
            today=today,
            horizon_days=settings.statistics_backfill_days,
            oldest_captured=oldest_captured,
            billing=points.period,
        )
        # A second read rather than a filter over the first: the charts
        # cover the selected period, which is usually wider than the
        # month on screen and never the same set of rows.
        period_rows = list(
            session.scalars(
                select(AttendanceRecord)
                .where(
                    AttendanceRecord.gym_account_id == gym_account_id,
                    AttendanceRecord.local_date >= period.start,
                    AttendanceRecord.local_date <= period.end,
                )
                .order_by(AttendanceRecord.start_at.asc())
            ).all()
        )

    counted = counted_records(
        records,
        now=now,
        settle_window_hours=settings.statistics_settle_window_hours,
    )
    over_period = counted_records(
        period_rows,
        now=now,
        settle_window_hours=settings.statistics_settle_window_hours,
    )
    all_counted = counted_records(
        all_rows,
        now=now,
        settle_window_hours=settings.statistics_settle_window_hours,
    )
    calendar = day_calendar(counted, captured=captured, start=start, end=end, today=today)
    dropouts = abandonment(counted)
    # One model governs every figure that depends on the gym's tiers.
    # Resolved once, before anything reads a threshold: charging a
    # cancellation under one boundary while labelling it under another
    # is the kind of disagreement a reader cannot diagnose.
    model = PointsModel.resolve(
        base_cost=settings.statistics_base_point_cost,
        late_penalty=settings.statistics_late_cancel_penalty,
        very_late_penalty=settings.statistics_very_late_cancel_penalty,
        absence_penalty=settings.statistics_absence_penalty,
        late_hours=settings.statistics_late_cancel_hours,
        very_late_hours=settings.statistics_very_late_cancel_hours,
        override=points_override,
    )
    bands = cancellation_bands(
        counted,
        late_hours=model.late_hours,
        very_late_hours=model.very_late_hours,
    )
    # Streaks read the whole captured history, not the month or the
    # chart period: a run that started before the window on screen is
    # still the run the reader is on.
    runs = streaks(
        all_counted,
        captured=all_captured,
        today=today,
        excluded_weekdays=excluded_weekdays,
    )

    # Days of this month that are over and still unread. The cells say
    # so one by one; this tells the reader it is worth coming back
    # rather than concluding the month was empty.
    unread = sum(
        1
        for cell in (c for week in calendar.weeks for c in week if c is not None)
        if cell.status == "uncaptured"
    )

    # Points and the weekly comparison describe the selected period, not
    # the month: they answer "how am I doing lately", and the month on
    # screen is chosen for a different reason.
    estimate = points_estimate(over_period, model=model)
    pace = weekly_average(
        all_counted,
        captured=all_captured.keys(),
        start=period.start,
        end=period.end,
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
        "abandonment": dropouts,
        "streaks": runs,
        "streak_cards": _streak_cards(runs),
        "excluded_weekday_labels": [
            t(f"day.{_WEEKDAY_KEYS[day]}") for day in sorted(excluded_weekdays)
        ],
        "bands": bands,
        "band_labels": _band_labels(bands),
        "weekday_labels": _weekday_labels(),
        "cell_lines": _cell_lines(calendar),
        "legend": _legend(),
        "points": estimate,
        "points_balance": points.balance,
        "points_assumptions": [
            t(
                f"statistics.points.assumption.{key}",
                hours=_hours_label(model.late_hours),
            )
            for key in estimate.assumptions
        ],
        "billing_period": points.period,
        "pace": pace,
        **_chart_context(over_period, period, model, billing=points.period),
    }


# An hour with one or two bookings produces a rate that swings between
# 0 and 100 percent on a single decision. The floor is what keeps the
# chart from inviting conclusions the data cannot carry.
_MIN_BOOKINGS_PER_HOUR = 5


def _chart_context(
    records: list[CountedRecord],
    period: Period,
    model: PointsModel,
    *,
    billing: tuple[date, date] | None = None,
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
    # The gym's own period is offered only when the gym stated it.
    # Listing it greyed out would advertise a capability the deployment
    # cannot deliver for this account.
    options = [key for key in PERIOD_KEYS if key != "billing" or billing is not None]

    return {
        "period": period,
        "period_options": [(key, t(f"statistics.period.{key}")) for key in options],
        "period_label": t(f"statistics.period.{period.key}"),
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
        "unread_days": 0,
        "capture_notice": None,
        "cookie_url": lang_url("/cookie"),
        "no_activity_ever": False,
        "abandonment": None,
        "streaks": None,
        "streak_cards": [],
        "excluded_weekday_labels": [],
        "points": None,
        "points_balance": None,
        "points_assumptions": [],
        "billing_period": None,
        "pace": None,
        "bands": None,
        "band_labels": [],
        "charts": [],
        "period": None,
        "period_options": [],
        "period_label": None,
        "occupancy": None,
        "month": None,
        "month_label": None,
    }


@router.get("/statistics", name="statistics")
def statistics(
    request: Request,
    operator_id: int = Depends(require_session),
    month: str | None = None,
    period: str | None = None,
) -> Response:
    """Render the statistics page for the active gym account.

    ``month`` selects the calendar month as ``YYYY-MM``; ``period``
    selects the window the charts describe. Neither carries any
    authority: the gym account still comes from the session, so both
    can only move the reader within their own history.
    """
    return _templates(request).TemplateResponse(
        request=request,
        name="statistics/page.html",
        context=_build_context(request, operator_id, month, period),
    )


__all__ = ["router"]
