"""Detection and prioritisation of material KPI movements.

Pipeline, in order, with each stage's output feeding only the next:

    candidate series (from the provider, never named in this module)
        -> window-scoped measurement        [baseline window vs event window]
        -> change classification            [is the ratio even defined?]
        -> materiality gate                 [is it large enough to act on?]
        -> statistical test                 [is it distinguishable from noise?]
        -> FDR pool                         [only validly tested hypotheses]
        -> multiplicity correction          [BH or BY, per the contract]
        -> decision                         [uses the ADJUSTED p-value only]

Three properties this module guarantees, each of which was violated before:

* **No window leakage.** Every value is read through an explicit half-open
  window. The baseline window and the event window are separate parameters and
  historical data reaches the result only through the baseline.
* **Only tested hypotheses are corrected.** A candidate with no valid p-value is
  excluded from the pool entirely rather than inflating m and weakening every
  real finding.
* **Decisions never consult a raw p-value.** `Decision` is derived from the
  adjusted p-value, so the correction is load-bearing rather than displayed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

import numpy as np
from pydantic import BaseModel
from scipy import stats

from app import fdr
from app.change import ChangeMeasurement, ChangeType, measure_change
from app.contracts import ContractStore, Relationship, get_contract_store
from app.materiality import MaterialityDecision, assess_materiality
from app.series import SeriesKey, SeriesProvider
from app.timeseries import TimeSeries


class Decision(str, Enum):
    """Terminal state for one hypothesis."""

    DETECTED = "DETECTED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    ABSTAIN = "ABSTAIN"
    NOT_MATERIAL = "NOT_MATERIAL"
    NOT_SIGNIFICANT = "NOT_SIGNIFICANT"

    @property
    def is_reportable(self) -> bool:
        return self in {Decision.DETECTED, Decision.LOW_CONFIDENCE}


class BaselineMode(str, Enum):
    """How to compare windows of different lengths.

    Both are legitimate; which is correct depends on the question being asked,
    so the caller declares it rather than the engine guessing from a threshold.

    MATCHED_LENGTH scales the baseline total to the event window's length, giving
    "actual versus what a window this long would normally produce". Required for
    any event window that is not the same size as its baseline -- comparing seven
    days against thirty-one otherwise reads as a collapse caused purely by the
    length difference.

    AS_REPORTED compares raw totals. Correct for like-for-like calendar periods,
    where month-over-month is reported as-is and nobody normalises February.
    """

    MATCHED_LENGTH = "MATCHED_LENGTH"
    AS_REPORTED = "AS_REPORTED"


class NotTestedReason(str, Enum):
    """Why no statistical test was run. The distinction drives the decision.

    GRAIN_TOO_COARSE is an expected structural limitation and permits reporting
    on materiality alone. The others indicate the evidence is inadequate, which
    forces an abstention rather than a quiet fallback.
    """

    GRAIN_TOO_COARSE = "GRAIN_TOO_COARSE"
    NO_BASELINE_OBSERVATIONS = "NO_BASELINE_OBSERVATIONS"
    NO_EVENT_OBSERVATIONS = "NO_EVENT_OBSERVATIONS"
    INSUFFICIENT_OBSERVATIONS = "INSUFFICIENT_OBSERVATIONS"
    DEGENERATE_SERIES = "DEGENERATE_SERIES"

    @property
    def permits_materiality_only(self) -> bool:
        return self is NotTestedReason.GRAIN_TOO_COARSE


class TestOutcome(BaseModel):
    tested: bool
    test_name: str | None = None
    p_value: float | None = None
    statistic: float | None = None
    effect_size: float | None = None
    baseline_n: int = 0
    event_n: int = 0
    not_tested_reason: NotTestedReason | None = None
    note: str | None = None

    model_config = {"frozen": True}


class WindowCoverage(BaseModel):
    """How completely the window was actually observed."""

    start: date
    end: date
    expected_days: int
    observed_days: int
    missing_days: int

    model_config = {"frozen": True}

    @property
    def coverage(self) -> float:
        return self.observed_days / self.expected_days if self.expected_days else 0.0


class HypothesisResult(BaseModel):
    key: str
    kpi: str
    dimension: str
    entity: str
    label: str
    unit: str

    measurement: ChangeMeasurement
    materiality: MaterialityDecision
    test: TestOutcome
    baseline_coverage: WindowCoverage
    event_coverage: WindowCoverage
    baseline_mode: BaselineMode = BaselineMode.MATCHED_LENGTH
    baseline_scale: float = 1.0

    adjusted_p_value: float | None = None
    significant_after_fdr: bool = False
    decision: Decision = Decision.ABSTAIN
    decision_reason: str = ""
    priority_score: float = 0.0
    confidence: float | None = None

    @property
    def is_reportable(self) -> bool:
        return self.decision.is_reportable

    @property
    def windows_equal_length(self) -> bool:
        """Sums over unequal windows are not directly comparable.

        Reported so a reader can see when a total-vs-total comparison is
        confounded by window length; the statistical test is unaffected because
        it operates on daily observations.
        """
        return self.baseline_coverage.expected_days == self.event_coverage.expected_days


class DetectionResult(BaseModel):
    baseline_window: tuple[date, date]
    event_window: tuple[date, date]
    baseline_mode: BaselineMode
    fdr: fdr.FDRResult
    hypotheses: list[HypothesisResult]

    @property
    def reportable(self) -> list[HypothesisResult]:
        return [h for h in self.hypotheses if h.is_reportable]

    @property
    def abstained(self) -> list[HypothesisResult]:
        return [h for h in self.hypotheses if h.decision is Decision.ABSTAIN]

    @property
    def suppressed(self) -> list[HypothesisResult]:
        return [
            h
            for h in self.hypotheses
            if h.decision in {Decision.NOT_MATERIAL, Decision.NOT_SIGNIFICANT}
        ]

    @property
    def candidates_evaluated(self) -> int:
        return len(self.hypotheses)

    @property
    def hypotheses_tested(self) -> int:
        return self.fdr.m_tested

    def by_decision(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for h in self.hypotheses:
            counts[h.decision.value] = counts.get(h.decision.value, 0) + 1
        return counts

    def get(self, key: str) -> HypothesisResult | None:
        return next((h for h in self.hypotheses if h.key == key), None)


@dataclass(frozen=True)
class _Candidate:
    key: SeriesKey
    baseline_scale: float
    measurement: ChangeMeasurement
    materiality: MaterialityDecision
    test: TestOutcome
    baseline_coverage: WindowCoverage
    event_coverage: WindowCoverage


class MovementDetector:
    def __init__(
        self,
        provider: SeriesProvider,
        store: ContractStore | None = None,
    ) -> None:
        self.provider = provider
        self.store = store or get_contract_store()

    # -- measurement -------------------------------------------------------

    @staticmethod
    def _coverage(series: TimeSeries | None, start: date, end: date) -> WindowCoverage:
        expected = max(0, (end - start).days)
        observed = series.n if series is not None else 0
        return WindowCoverage(
            start=start,
            end=end,
            expected_days=expected,
            observed_days=observed,
            missing_days=max(0, expected - observed),
        )

    def _run_test(
        self,
        baseline: TimeSeries | None,
        event: TimeSeries | None,
    ) -> TestOutcome:
        """Two-sample Mann-Whitney U on daily observations.

        Non-parametric by choice: daily revenue and order counts are right-skewed
        and bounded below at zero, so a t-test's normality assumption does not
        hold. The test compares distributions of daily values, which is why it is
        unaffected by the two windows having different lengths.

        Effect size is the rank-biserial correlation, r = 1 - 2U/(n1*n2), which
        is the natural companion to the U statistic and is bounded in [-1, 1].
        """
        if baseline is None or event is None:
            return TestOutcome(
                tested=False,
                not_tested_reason=NotTestedReason.GRAIN_TOO_COARSE,
                note="KPI has no daily representation at this grain.",
            )
        if baseline.n == 0:
            return TestOutcome(
                tested=False,
                baseline_n=0,
                event_n=event.n,
                not_tested_reason=NotTestedReason.NO_BASELINE_OBSERVATIONS,
                note="No observations in the baseline window.",
            )
        if event.n == 0:
            return TestOutcome(
                tested=False,
                baseline_n=baseline.n,
                event_n=0,
                not_tested_reason=NotTestedReason.NO_EVENT_OBSERVATIONS,
                note="No observations in the event window.",
            )

        minimum = self.store.detection.min_baseline_points
        if baseline.n < minimum or event.n < minimum:
            return TestOutcome(
                tested=False,
                baseline_n=baseline.n,
                event_n=event.n,
                not_tested_reason=NotTestedReason.INSUFFICIENT_OBSERVATIONS,
                note=(
                    f"Need at least {minimum} observations per window; "
                    f"have {baseline.n} baseline and {event.n} event."
                ),
            )

        baseline_values = baseline.values()
        event_values = event.values()
        if len(set(baseline_values)) == 1 and len(set(event_values)) == 1:
            if baseline_values[0] == event_values[0]:
                return TestOutcome(
                    tested=False,
                    baseline_n=baseline.n,
                    event_n=event.n,
                    not_tested_reason=NotTestedReason.DEGENERATE_SERIES,
                    note="Both windows are constant and identical; no variation to test.",
                )

        try:
            statistic, p_value = stats.mannwhitneyu(
                event_values, baseline_values, alternative="two-sided"
            )
        except ValueError as exc:
            return TestOutcome(
                tested=False,
                baseline_n=baseline.n,
                event_n=event.n,
                not_tested_reason=NotTestedReason.DEGENERATE_SERIES,
                note=f"Test could not be computed: {exc}",
            )

        if not np.isfinite(p_value):
            return TestOutcome(
                tested=False,
                baseline_n=baseline.n,
                event_n=event.n,
                not_tested_reason=NotTestedReason.DEGENERATE_SERIES,
                note="Test produced a non-finite p-value.",
            )

        effect = 1.0 - (2.0 * float(statistic)) / (baseline.n * event.n)
        return TestOutcome(
            tested=True,
            test_name="mann_whitney_u",
            p_value=float(p_value),
            statistic=float(statistic),
            effect_size=float(np.clip(-effect, -1.0, 1.0)),
            baseline_n=baseline.n,
            event_n=event.n,
        )

    def _baseline_scale(
        self,
        key: SeriesKey,
        baseline_window: tuple[date, date],
        event_window: tuple[date, date],
        mode: BaselineMode,
    ) -> float:
        """Factor converting a baseline total to the event window's length."""
        if mode is BaselineMode.AS_REPORTED:
            return 1.0
        if self.store.kpi(key.kpi).metric_tree.relationship is not Relationship.ADDITIVE:
            return 1.0
        baseline_days = (baseline_window[1] - baseline_window[0]).days
        event_days = (event_window[1] - event_window[0]).days
        if baseline_days <= 0 or baseline_days == event_days:
            return 1.0
        return event_days / baseline_days

    def _build_candidate(
        self,
        key: SeriesKey,
        baseline_window: tuple[date, date],
        event_window: tuple[date, date],
        mode: BaselineMode,
    ) -> _Candidate:
        contract_kpi = self.store.kpi(key.kpi)

        baseline_value = self.provider.aggregate(key, *baseline_window)
        event_value = self.provider.aggregate(key, *event_window)

        # Windows of unequal length make sums incomparable: a 7-day event against
        # a 31-day baseline would read as a catastrophic collapse purely from the
        # length difference. For an additive KPI the baseline is scaled to the
        # event window's length, so the comparison is "actual versus what a window
        # this long would normally produce". Rates and ratios are already
        # length-normalised and are left alone -- which of the two applies is
        # decided by the metric tree, not by the KPI's name.
        scale = self._baseline_scale(key, baseline_window, event_window, mode)
        if baseline_value is not None and scale != 1.0:
            baseline_value = baseline_value * scale

        measurement = measure_change(
            baseline_value,
            event_value,
            baseline_floor=contract_kpi.materiality.baseline_floor,
        )
        materiality = assess_materiality(measurement, contract_kpi.materiality)

        baseline_series = self.provider.daily_series(key, *baseline_window)
        event_series = self.provider.daily_series(key, *event_window)

        return _Candidate(
            key=key,
            baseline_scale=scale,
            measurement=measurement,
            materiality=materiality,
            test=self._run_test(baseline_series, event_series),
            baseline_coverage=self._coverage(baseline_series, *baseline_window),
            event_coverage=self._coverage(event_series, *event_window),
        )

    # -- decision ----------------------------------------------------------

    def _decide(
        self,
        candidate: _Candidate,
        adjusted_p: float | None,
        significant: bool,
        alpha: float,
    ) -> tuple[Decision, str]:
        """Terminal state for one hypothesis.

        Order matters: a movement that cannot be measured is an abstention, not
        an immaterial finding, because "we could not tell" and "we checked and it
        was small" are different answers.
        """
        measurement = candidate.measurement
        test = candidate.test

        if not measurement.change_type.is_measurable:
            return Decision.ABSTAIN, measurement.reason

        if measurement.change_type is ChangeType.NO_ACTIVITY:
            return Decision.NOT_MATERIAL, "No activity in either window."

        if not candidate.materiality.is_material:
            if not candidate.materiality.was_gated:
                return Decision.ABSTAIN, candidate.materiality.reason
            return Decision.NOT_MATERIAL, candidate.materiality.reason

        if test.tested:
            if significant:
                return (
                    Decision.DETECTED,
                    (
                        f"Material ({candidate.materiality.exceedance:.1f}x the threshold) and "
                        f"significant after {self.store.detection.fdr_method.value} correction "
                        f"(adjusted p = {adjusted_p:.2e} <= {alpha})."
                    ),
                )
            return (
                Decision.NOT_SIGNIFICANT,
                (
                    f"Material but not distinguishable from noise after correction "
                    f"(adjusted p = {adjusted_p:.3f} > {alpha})."
                ),
            )

        reason = test.not_tested_reason
        if reason is not None and reason.permits_materiality_only:
            return (
                Decision.LOW_CONFIDENCE,
                (
                    "Material on absolute impact, but the KPI's grain does not support a "
                    "distributional test against this window, so no significance claim is made."
                ),
            )

        if measurement.change_type is ChangeType.NEW_ACTIVITY:
            return (
                Decision.LOW_CONFIDENCE,
                (
                    "New activity from a zero baseline: a material event, but with no prior "
                    "period there is nothing to test it against."
                ),
            )

        return (
            Decision.ABSTAIN,
            f"No valid statistical test was possible: {test.note or reason}",
        )

    # -- entry point -------------------------------------------------------

    def detect(
        self,
        baseline_window: tuple[date, date],
        event_window: tuple[date, date],
        keys: list[SeriesKey] | None = None,
        baseline_mode: BaselineMode = BaselineMode.MATCHED_LENGTH,
    ) -> DetectionResult:
        """Evaluate every candidate series over an explicit pair of windows.

        `baseline_window` is the only route by which historical data enters the
        result. `event_window` bounds everything said about what happened.
        """
        for label, (start, end) in (("baseline", baseline_window), ("event", event_window)):
            if end <= start:
                raise ValueError(f"{label} window must be non-empty: {start} to {end}")

        candidates = [
            self._build_candidate(key, baseline_window, event_window, baseline_mode)
            for key in (keys if keys is not None else self.provider.available_keys())
        ]

        # Only hypotheses that were actually tested enter the pool. Passing a
        # None p-value here would be silently dropped by fdr.correct, but the
        # filter is explicit so the intent survives future edits.
        pool = {
            str(c.key): c.test.p_value for c in candidates if c.test.tested
        }
        correction = fdr.correct(
            pool,
            alpha=self.store.detection.fdr_alpha,
            method=self.store.detection.fdr_method,
        )

        results: list[HypothesisResult] = []
        for candidate in candidates:
            key_str = str(candidate.key)
            adjusted = correction.adjusted_p(key_str)
            significant = correction.is_significant(key_str)
            decision, reason = self._decide(
                candidate, adjusted, significant, self.store.detection.fdr_alpha
            )

            contract_kpi = self.store.kpi(candidate.key.kpi)
            results.append(
                HypothesisResult(
                    key=key_str,
                    kpi=candidate.key.kpi,
                    dimension=candidate.key.dimension,
                    entity=candidate.key.entity,
                    label=contract_kpi.label,
                    unit=contract_kpi.unit,
                    measurement=candidate.measurement,
                    materiality=candidate.materiality,
                    test=candidate.test,
                    baseline_coverage=candidate.baseline_coverage,
                    event_coverage=candidate.event_coverage,
                    baseline_mode=baseline_mode,
                    baseline_scale=candidate.baseline_scale,
                    adjusted_p_value=adjusted,
                    significant_after_fdr=significant,
                    decision=decision,
                    decision_reason=reason,
                    priority_score=self._priority(candidate, decision, adjusted),
                )
            )

        results.sort(key=lambda h: h.priority_score, reverse=True)
        return DetectionResult(
            baseline_window=baseline_window,
            event_window=event_window,
            baseline_mode=baseline_mode,
            fdr=correction,
            hypotheses=results,
        )

    def _priority(
        self,
        candidate: _Candidate,
        decision: Decision,
        adjusted_p: float | None,
    ) -> float:
        """Rank by business impact weighted by evidential strength.

        Exceedance is already capped, so no degenerate baseline can dominate the
        queue. A finding with no statistical backing is discounted rather than
        excluded, so it can surface without displacing tested evidence.
        """
        if not decision.is_reportable:
            return 0.0

        exceedance = candidate.materiality.exceedance
        if candidate.test.tested and adjusted_p is not None:
            alpha = self.store.detection.fdr_alpha
            evidence_weight = max(0.0, 1.0 - adjusted_p / alpha) if alpha > 0 else 0.0
        else:
            evidence_weight = 0.4

        return float(exceedance * evidence_weight)
