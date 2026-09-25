"""The gym's points economy, applied to captured behaviour (ADR-0015).

Two kinds of number live here and they are never mixed.

A **fact** is something the gym states or something derived from an
instant the gym recorded. The balance is read from the gym's own points
page. The penalties are derived from the removal instant, which
WodBuster does record, so the tier each removal fell into is known.

An **estimate** is everything else, and here it is a single thing: the
base cost of a booking that was later dropped. The gym charges a point
only when the booking was made more than four hours ahead, and the
instant of that booking is overwritten the moment the athlete removes
themselves. The data cannot say whether the point was ever spent, so
the base cost is reported as a range and never folded into one number.

The gym's wording, verbatim, for reference:

    Reservar con más de 4 horas de antelación cuesta 1 punto (aunque
    algunas clases pueden costar más). Reservar con menos de 4 horas es
    gratis. Si la clase nunca llegó a llenarse, recuperas los puntos
    gastados. Si te borras con menos de 4 h pierdes 1 punto extra; con
    menos de 1 h, 2 puntos extra; si no asistes, 6 puntos extra.

No threshold, cost or penalty appears here as a literal. All of them
arrive as a :class:`PointsModel` built from configuration, because a
gym can change them with a notice pinned to a wall and no signal to
this system.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from .metrics import CountedRecord

__all__ = ["PointsEstimate", "PointsModel", "points_estimate"]

# Why a figure is not exact. The template resolves these through the
# catalog; keeping them as keys stops the metric layer from deciding how
# an assumption is worded, and stops the template from rendering a
# points figure that carries none.
Assumption = str

ASSUMPTION_BASE_COST_UNKNOWN = "base_cost_unknown"
ASSUMPTION_RECOVERY_BASE_ONLY = "recovery_base_only"
ASSUMPTION_NON_STANDARD_COST = "non_standard_cost"
ASSUMPTION_CLASS_CHANGES_CHARGED = "class_changes_charged"


@dataclass(frozen=True)
class PointsModel:
    """The gym's published prices, as configured for this deployment."""

    base_cost: int
    late_penalty: int
    very_late_penalty: int
    absence_penalty: int
    late_hours: float
    very_late_hours: float


@dataclass(frozen=True)
class PointsEstimate:
    """What the captured behaviour cost, separated by how well it is known.

    ``penalties`` is a fact. ``base_cost_low`` and ``base_cost_high``
    bound the part that cannot be known, so the page can say "at least
    N" without pretending the upper end is measured.

    ``assumptions`` is never empty when anything was priced, which is
    the structural half of INV-004: a template cannot reach the figure
    without also reaching the reasons it is not exact.
    """

    penalties: int
    base_cost_low: int
    base_cost_high: int
    late_cancellations: int
    very_late_cancellations: int
    early_cancellations: int
    class_changes: int
    no_shows: int
    removed_after_start: int
    recovered_bookings: int
    unknown_lead: int
    assumptions: tuple[Assumption, ...] = field(default=())

    @property
    def total_low(self) -> int:
        """The floor: penalties, which are known, plus nothing assumed."""
        return self.penalties + self.base_cost_low

    @property
    def total_high(self) -> int:
        return self.penalties + self.base_cost_high

    @property
    def is_range(self) -> bool:
        """True when the two ends differ and the page must show both."""
        return self.total_high > self.total_low

    @property
    def absences(self) -> int:
        """Both paths by which the gym sees an absence.

        Kept as a sum with its two parts still visible, because at a gym
        with the attendance control disabled every absence arrives as a
        post-start removal, and a figure that hid that would be
        untraceable when it looks wrong.
        """
        return self.no_shows + self.removed_after_start


def points_estimate(
    records: Sequence[CountedRecord],
    *,
    model: PointsModel,
) -> PointsEstimate:
    """Price the removals and absences in ``records``.

    A class change is priced. The gym does not know the athlete moved to
    another hour; it sees a removal and charges for it. Excluding them
    would make the figure disagree with the balance, which is the one
    number on this page the user can check against the gym. They are
    counted separately so the reader can see the part that came from a
    change rather than from giving up on a class.

    A booking on a class that never filled up has its base cost
    recovered, per the gym's own first rule for removals. The recovery
    is applied to the base cost only and never to a penalty, which is
    the reading recorded in ADR-0015 and stated on screen.
    """
    late_cut = timedelta(hours=model.late_hours)
    very_late_cut = timedelta(hours=model.very_late_hours)

    penalties = 0
    early = late = very_late = unknown = 0
    changes = no_shows = post_start = recovered = 0
    chargeable = 0

    for record in records:
        if record.state in ("cancelled", "swapped"):
            if record.state == "swapped":
                changes += 1
            lead = record.removal_lead
            if lead is None:
                unknown += 1
            elif lead >= late_cut:
                early += 1
            elif lead >= very_late_cut:
                late += 1
                penalties += model.late_penalty
            else:
                very_late += 1
                penalties += model.very_late_penalty
        elif record.state == "no_show":
            no_shows += 1
            penalties += model.absence_penalty
        elif record.state == "removed_after_start":
            post_start += 1
            penalties += model.absence_penalty
        else:
            continue

        # Whatever the tier, the booking itself may or may not have cost
        # a point. The class that never filled gives it back.
        if record.ever_full:
            chargeable += 1
        else:
            recovered += 1

    assumptions: list[Assumption] = []
    if chargeable:
        assumptions.append(ASSUMPTION_BASE_COST_UNKNOWN)
        assumptions.append(ASSUMPTION_NON_STANDARD_COST)
    if recovered:
        assumptions.append(ASSUMPTION_RECOVERY_BASE_ONLY)
    if changes:
        assumptions.append(ASSUMPTION_CLASS_CHANGES_CHARGED)

    return PointsEstimate(
        penalties=penalties,
        base_cost_low=0,
        base_cost_high=chargeable * model.base_cost,
        late_cancellations=late,
        very_late_cancellations=very_late,
        early_cancellations=early,
        class_changes=changes,
        no_shows=no_shows,
        removed_after_start=post_start,
        recovered_bookings=recovered,
        unknown_lead=unknown,
        assumptions=tuple(assumptions),
    )
