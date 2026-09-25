"""Shape metric results into Chart.js payloads (ADR-0014).

The server sends finished numbers and finished text; the browser draws
them. Nothing here emits markup, and nothing here knows a colour: the
palette lives in ``brand.css`` and is read by ``static/wb-charts.js``
at chart creation, so a theme change recolours every chart without a
JavaScript edit.

Keeping the shaping in Python is what makes the conditions that break
charts in practice, an empty series, a single point, every value zero,
ordinary pytest cases rather than something noticed in a browser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .metrics import GridCell, LeadBands, MonthPoint, SlotStats


@dataclass(frozen=True)
class ChartPayload:
    """One chart, ready to be serialised into the page."""

    kind: str
    labels: tuple[str, ...]
    values: tuple[float, ...]
    strings: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    points: tuple[dict[str, Any], ...] = ()
    series: tuple[dict[str, Any], ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to draw.

        An all-zero series is not empty: the zeros are the finding.
        """
        return not (self.values or self.points or self.series)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "labels": list(self.labels),
            "values": list(self.values),
            "strings": dict(self.strings),
            "meta": dict(self.meta),
            "points": [dict(p) for p in self.points],
            "series": [dict(s) for s in self.series],
        }


def grid_payload(
    cells: tuple[GridCell, ...],
    *,
    weekday_labels: list[str],
    strings: dict[str, str],
) -> ChartPayload:
    """Weekday by class-time matrix of attended sessions.

    Both axes are categories. A linear hour axis was tried first and
    produced ticks like ``08.5:00``: Chart.js interpolates between
    integers, and a gym's slots are not evenly spaced anyway. Naming
    the real start times removes both problems at once.
    """
    if not cells:
        return ChartPayload(kind="matrix", labels=(), values=(), strings=strings)

    slots = sorted({cell.slot for cell in cells})
    peak = max(cell.attended for cell in cells)

    return ChartPayload(
        kind="matrix",
        labels=tuple(weekday_labels),
        values=(),
        strings=strings,
        meta={"slots": slots, "peak": peak},
        points=tuple(
            {"x": cell.slot, "y": weekday_labels[cell.weekday], "v": cell.attended}
            for cell in cells
        ),
    )


def drop_rate_payload(stats: tuple[SlotStats, ...], strings: dict[str, str]) -> ChartPayload:
    """Drop rate per class start time, as whole percent."""
    return ChartPayload(
        kind="bar",
        labels=tuple(stat.slot for stat in stats),
        values=tuple(round(100 * (stat.drop_rate or 0)) for stat in stats),
        strings=strings,
        meta={
            # Carried so the tooltip can say "4 of 14" rather than a
            # bare percentage the reader cannot check.
            "booked": [stat.booked for stat in stats],
            "dropped": [stat.cancelled for stat in stats],
            "max": 100,
        },
    )


def trend_payload(points: tuple[MonthPoint, ...], strings: dict[str, str]) -> ChartPayload:
    """Attended and dropped classes per calendar month.

    With no months to draw, the payload is empty rather than two series
    of nothing. Two empty arrays still read as present to ``is_empty``,
    which would put a blank canvas on screen where the page has an
    empty state that says so in words.
    """
    if not points:
        return ChartPayload(kind="bar-stacked", labels=(), values=(), strings=strings, meta={})
    return ChartPayload(
        kind="bar-stacked",
        labels=tuple(point.key for point in points),
        values=(),
        strings=strings,
        meta={"max": max((p.attended + p.cancelled for p in points), default=0)},
        series=(
            {
                "key": "attended",
                "label": strings.get("attended", "attended"),
                "values": [point.attended for point in points],
            },
            {
                "key": "cancelled",
                "label": strings.get("cancelled", "dropped"),
                "values": [point.cancelled for point in points],
            },
        ),
    )


def lead_payload(bands: LeadBands, labels: list[str], strings: dict[str, str]) -> ChartPayload:
    """How far ahead attended classes were booked."""
    values = (bands.same_day, bands.within_day, bands.early)
    return ChartPayload(
        kind="bar-horizontal",
        labels=tuple(labels),
        values=tuple(float(v) for v in values),
        strings=strings,
        meta={"max": max(values, default=0), "unknown": bands.unknown},
    )


__all__ = [
    "ChartPayload",
    "drop_rate_payload",
    "grid_payload",
    "lead_payload",
    "trend_payload",
]
