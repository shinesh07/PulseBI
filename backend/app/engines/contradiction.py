"""Reconciliation of evidence that ought to agree.

The previous implementation hardcoded two specific comparisons. This one defines
contradiction structurally, in terms of relationships the contract already
declares, so it works for any KPI:

* **Slice additivity.** For a KPI the contract marks `additive`, the movements of
  its dimension slices must sum to the movement of the total. A residual beyond
  tolerance means two views of the same quantity disagree.
* **Cross-source agreement.** Two independent measurements of the same quantity
  should move together. A wide divergence means at least one of them is wrong.

Three outcomes, never two:

    CONSISTENT             evidence agrees within tolerance
    CONTRADICTORY          evidence disagrees beyond tolerance
    INSUFFICIENT_EVIDENCE  not enough signal to judge either way

The third is the important one. Absence of evidence is not agreement, and the
audit found the old `consistency_score()` returning a perfect 1.0 for an empty
signal list -- so a total measurement failure scored as flawless corroboration.
Every check here is window-scoped, because a contradiction detected across a
month says nothing about a particular week inside it.
"""

from __future__ import annotations

from datetime import date
from enum import Enum

import numpy as np
from pydantic import BaseModel

from app.contracts import ContractStore, Relationship, get_contract_store
from app.series import DuckDBSeriesProvider, SeriesKey

# A residual larger than this share of the observed movement means the parts do
# not explain the whole.
ADDITIVITY_TOLERANCE = 0.02

# Two measurements of the same quantity diverging by more than this many
# percentage points of growth are not measuring the same thing.
SOURCE_DIVERGENCE_TOLERANCE_PP = 20.0

# Below this absolute movement there is nothing meaningful to reconcile, and a
# ratio against it would be dominated by rounding.
MIN_MOVEMENT_TO_RECONCILE = 1e-9


class ReconciliationStatus(str, Enum):
    CONSISTENT = "CONSISTENT"
    CONTRADICTORY = "CONTRADICTORY"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ReconciliationCheck(BaseModel):
    name: str
    label: str
    status: ReconciliationStatus
    observed: float | None = None
    explained: float | None = None
    unexplained: float | None = None
    divergence: float | None = None
    tolerance: float
    detail: str
    # Which KPIs this check actually bears on. A contradiction in marketing
    # attribution says nothing about whether total revenue moved -- POS counts
    # the same orders either way -- so penalising every finding for it would
    # bury real signal under an unrelated data-quality problem.
    affected_kpis: list[str] = []

    model_config = {"frozen": True}

    @property
    def is_contradictory(self) -> bool:
        return self.status is ReconciliationStatus.CONTRADICTORY

    def affects(self, kpi: str) -> bool:
        """An empty affected list means the check is global."""
        return not self.affected_kpis or kpi in self.affected_kpis


class ContradictionReport(BaseModel):
    window_start: date
    window_end: date
    checks: list[ReconciliationCheck]

    @property
    def contradictions(self) -> list[ReconciliationCheck]:
        return [c for c in self.checks if c.is_contradictory]

    def contradictions_for(self, kpi: str) -> list[ReconciliationCheck]:
        """Contradictions that actually bear on the given KPI."""
        return [c for c in self.contradictions if c.affects(kpi)]

    @property
    def conclusive_checks(self) -> list[ReconciliationCheck]:
        """Checks that reached a verdict either way."""
        return [
            c for c in self.checks if c.status is not ReconciliationStatus.INSUFFICIENT_EVIDENCE
        ]

    @property
    def status(self) -> ReconciliationStatus:
        if self.contradictions:
            return ReconciliationStatus.CONTRADICTORY
        if not self.conclusive_checks:
            return ReconciliationStatus.INSUFFICIENT_EVIDENCE
        return ReconciliationStatus.CONSISTENT

    @property
    def evidence_coverage(self) -> float:
        """Share of attempted checks that reached a verdict.

        Consumed by the confidence model so that unreachable checks reduce
        confidence instead of being silently treated as agreement.
        """
        if not self.checks:
            return 0.0
        return len(self.conclusive_checks) / len(self.checks)

    def consistency_score(self) -> float:
        """Agreement strength in [0, 1], or 0 when nothing could be checked.

        Exponential decay in the worst divergence, so the score keeps
        discriminating at large gaps rather than saturating: a 40 pp and a 150 pp
        divergence are both bad, but not equally bad.

        Scored on the worst check rather than an average because these signals
        often share a root cause and are not independent evidence; averaging
        correlated failures understates the problem.
        """
        conclusive = self.conclusive_checks
        if not conclusive:
            return 0.0

        divergences = [c.divergence for c in conclusive if c.divergence is not None]
        if not divergences:
            return 1.0
        worst = max(abs(d) for d in divergences)
        return float(np.exp(-worst / SOURCE_DIVERGENCE_TOLERANCE_PP / 3.0))


def reconcile_additive(
    *,
    name: str,
    label: str,
    observed: float | None,
    parts: dict[str, float | None],
    tolerance: float = ADDITIVITY_TOLERANCE,
) -> ReconciliationCheck:
    """Do the parts explain the whole?

    Generic over any additive relationship: dimension slices against a total,
    or driver contributions against a decomposed movement.
    """
    missing = [k for k, v in parts.items() if v is None]
    if observed is None or missing:
        return ReconciliationCheck(
            name=name,
            label=label,
            status=ReconciliationStatus.INSUFFICIENT_EVIDENCE,
            tolerance=tolerance,
            detail=(
                "Cannot reconcile: "
                + ("the total is unavailable." if observed is None else f"missing parts {missing}.")
            ),
        )

    explained = float(sum(v for v in parts.values() if v is not None))
    unexplained = observed - explained

    if abs(observed) < MIN_MOVEMENT_TO_RECONCILE:
        if abs(unexplained) < MIN_MOVEMENT_TO_RECONCILE:
            return ReconciliationCheck(
                name=name,
                label=label,
                status=ReconciliationStatus.CONSISTENT,
                observed=observed,
                explained=explained,
                unexplained=unexplained,
                divergence=0.0,
                tolerance=tolerance,
                detail="No movement to reconcile; parts and total both flat.",
            )
        return ReconciliationCheck(
            name=name,
            label=label,
            status=ReconciliationStatus.CONTRADICTORY,
            observed=observed,
            explained=explained,
            unexplained=unexplained,
            divergence=100.0,
            tolerance=tolerance,
            detail="Parts move while the total does not.",
        )

    ratio = abs(unexplained) / abs(observed)
    contradictory = ratio > tolerance
    return ReconciliationCheck(
        name=name,
        label=label,
        status=(
            ReconciliationStatus.CONTRADICTORY if contradictory else ReconciliationStatus.CONSISTENT
        ),
        observed=observed,
        explained=explained,
        unexplained=unexplained,
        divergence=ratio * 100.0,
        tolerance=tolerance,
        detail=(
            f"Parts explain {explained:,.2f} of an observed {observed:,.2f} "
            f"({ratio:.2%} unexplained; tolerance {tolerance:.2%})."
        ),
    )


def reconcile_sources(
    *,
    name: str,
    label: str,
    affected_kpis: list[str] | None = None,
    growth_a: float | None,
    growth_b: float | None,
    source_a: str,
    source_b: str,
    likely_cause: str,
    tolerance_pp: float = SOURCE_DIVERGENCE_TOLERANCE_PP,
) -> ReconciliationCheck:
    """Do two independent measurements of the same quantity agree?"""
    if growth_a is None or growth_b is None:
        absent = source_a if growth_a is None else source_b
        return ReconciliationCheck(
            name=name,
            label=label,
            status=ReconciliationStatus.INSUFFICIENT_EVIDENCE,
            tolerance=tolerance_pp,
            affected_kpis=affected_kpis or [],
            detail=f"Cannot compare: {absent} produced no measurable movement in this window.",
        )

    divergence = growth_a - growth_b
    contradictory = abs(divergence) >= tolerance_pp
    return ReconciliationCheck(
        name=name,
        label=label,
        status=(
            ReconciliationStatus.CONTRADICTORY if contradictory else ReconciliationStatus.CONSISTENT
        ),
        observed=growth_a,
        explained=growth_b,
        divergence=divergence,
        tolerance=tolerance_pp,
        affected_kpis=affected_kpis or [],
        detail=(
            f"{source_a} moved {growth_a:+.1f}% while {source_b} moved {growth_b:+.1f}% "
            f"({divergence:+.1f} pp apart). "
            + (likely_cause if contradictory else "Within tolerance.")
        ),
    )


class ContradictionDetector:
    """Runs structural reconciliation checks over an explicit window pair."""

    def __init__(
        self,
        provider: DuckDBSeriesProvider,
        store: ContractStore | None = None,
    ) -> None:
        self.provider = provider
        self.store = store or get_contract_store()

    def _growth(self, key: SeriesKey, baseline: tuple[date, date], event: tuple[date, date]):
        before = self.provider.aggregate(key, *baseline)
        after = self.provider.aggregate(key, *event)
        if before is None or after is None or before == 0:
            return None
        return (after - before) / abs(before) * 100.0

    def _attributed_revenue(self, start: date, end: date) -> float | None:
        row = self.provider.conn.execute(
            """
            SELECT sum(revenue) FROM pos_orders
            WHERE CAST(date AS DATE) >= ? AND CAST(date AS DATE) < ?
              AND utm_source IS NOT NULL
            """,
            [start, end],
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def _slice_additivity(
        self,
        kpi: str,
        baseline: tuple[date, date],
        event: tuple[date, date],
    ) -> ReconciliationCheck:
        """Slices of an additive KPI must sum to its total."""
        total_key = SeriesKey(kpi, "total", "ALL")
        total_before = self.provider.aggregate(total_key, *baseline)
        total_after = self.provider.aggregate(total_key, *event)
        observed = (
            None if total_before is None or total_after is None else total_after - total_before
        )

        parts: dict[str, float | None] = {}
        for key in self.provider.available_keys():
            if key.kpi != kpi or key.dimension != "category":
                continue
            before = self.provider.aggregate(key, *baseline)
            after = self.provider.aggregate(key, *event)
            parts[key.entity] = None if before is None or after is None else after - before

        if not parts:
            return ReconciliationCheck(
                name=f"{kpi}_slice_additivity",
                label=f"{kpi} slices vs total",
                status=ReconciliationStatus.INSUFFICIENT_EVIDENCE,
                tolerance=ADDITIVITY_TOLERANCE,
                detail=f"No dimension slices available for {kpi}.",
            )

        check = reconcile_additive(
            name=f"{kpi}_slice_additivity",
            label=f"{kpi} category slices vs total",
            observed=observed,
            parts=parts,
        )
        return check.model_copy(update={"affected_kpis": [kpi]})

    def evaluate(
        self,
        baseline_window: tuple[date, date],
        event_window: tuple[date, date],
    ) -> ContradictionReport:
        """All reconciliation checks, scoped to the given windows."""
        checks: list[ReconciliationCheck] = []

        # Structural: for every additive KPI the contract declares, its slices
        # must reconstruct its total. Driven by the metric tree, not by name.
        for kpi_name in sorted(self.store.kpi_names):
            kpi = self.store.kpi(kpi_name)
            if kpi.metric_tree.relationship is Relationship.ADDITIVE:
                checks.append(self._slice_additivity(kpi_name, baseline_window, event_window))

        # Cross-source: total revenue against the subset still carrying
        # attribution tags. Both measure the same orders over the same window.
        revenue_key = SeriesKey("revenue", "total", "ALL")
        total_growth = self._growth(revenue_key, baseline_window, event_window)

        attributed_before = self._attributed_revenue(*baseline_window)
        attributed_after = self._attributed_revenue(*event_window)
        attributed_growth = (
            None
            if not attributed_before or attributed_after is None
            else (attributed_after - attributed_before) / abs(attributed_before) * 100.0
        )

        # This check bears only on KPIs that actually consume attribution data.
        # Total revenue is counted from POS regardless of tagging, so a tag
        # outage must not depress confidence in it.
        attribution_dependent = sorted(
            name
            for name in self.store.kpi_names
            if "marketing_spend" in self.store.kpi(name).sources
        )

        checks.append(
            reconcile_sources(
                affected_kpis=attribution_dependent,
                name="attribution_divergence",
                label="Total revenue vs attributed revenue",
                growth_a=total_growth,
                growth_b=attributed_growth,
                source_a="POS total revenue",
                source_b="attributed revenue",
                likely_cause=(
                    "Both series count the same orders and differ only in tagging, so a gap "
                    "this wide indicates attribution loss rather than a demand shift."
                ),
            )
        )

        return ContradictionReport(
            window_start=event_window[0],
            window_end=event_window[1],
            checks=checks,
        )
