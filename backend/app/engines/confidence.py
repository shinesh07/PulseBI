"""Confidence scoring: how good is the evidence behind one finding?

What this score is, stated plainly because the distinction matters: it is a
**governance rubric on a 0-1 scale, not a calibrated probability**. It does not
estimate P(the finding is correct). The weights are a business policy choice
declared in the contract, and every fact derived from them is tagged
`business_rule` rather than `statistical_estimate`. Calibrating it properly would
mean labelling outcomes and fitting the mapping -- until that is done, calling it
a probability would be exactly the false precision this engine exists to avoid.

Signals, all measured rather than asserted:

    sample_size          observations backing the statistical test
    statistical_evidence how far below alpha the ADJUSTED p-value sits
    effect_magnitude     rank-biserial effect size of the movement
    data_completeness    share of the window's days actually observed
    source_freshness     age of each source against its declared SLA
    cross_source_consistency  agreement between independent measurements
    contradiction_status whether reconciliation found a conflict
    cold_start_status    whether the entity has enough history to speak for itself

Two rules the audit found violated, now enforced by construction:

* **Missing evidence lowers confidence.** A signal that could not be measured
  contributes its weight at zero, and the assessment records the coverage gap.
  Previously an empty contradiction report scored a perfect 1.0.
* **Contradiction cannot raise confidence.** It enters only as a penalty.
"""

from __future__ import annotations

from datetime import date
from enum import Enum

import numpy as np
from pydantic import BaseModel, field_validator

from app.contracts import ContractStore, get_contract_store
from app.engines.contradiction import ContradictionReport, ReconciliationStatus
from app.change import ChangeType
from app.engines.detector import Decision, HypothesisResult, NotTestedReason
from app.series import DuckDBSeriesProvider


class SignalAvailability(str, Enum):
    """Three states, because two conflate very different situations.

    UNAVAILABLE means the signal *should* bear on this hypothesis but could not
    be measured -- that is a genuine evidence gap and must lower confidence.
    NOT_APPLICABLE means the signal does not bear on this hypothesis at all;
    attribution coverage says nothing about whether total revenue moved, since
    POS counts the same orders regardless of tagging. Scoring that as a gap
    would penalise a finding for an unrelated data-quality problem, so
    inapplicable signals are excluded and the remaining weights renormalised.
    """

    MEASURED = "MEASURED"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ConfidenceSignal(BaseModel):
    name: str
    value: float
    weight: float
    availability: SignalAvailability
    measurement: str

    model_config = {"frozen": True}

    @field_validator("value")
    @classmethod
    def _bounded(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"Confidence signal value must lie in [0, 1], got {v}")
        return v

    @property
    def is_applicable(self) -> bool:
        return self.availability is not SignalAvailability.NOT_APPLICABLE

    @property
    def contribution(self) -> float:
        """Unavailable signals contribute nothing, never a default credit."""
        return self.value * self.weight if self.availability is SignalAvailability.MEASURED else 0.0


class ConfidenceAssessment(BaseModel):
    score: float
    scale: str = "governance_rubric_0_1"
    is_calibrated: bool = False

    signals: list[ConfidenceSignal]
    evidence_coverage: float
    penalties: dict[str, float]

    abstain_threshold: float
    low_confidence_threshold: float
    decision: Decision
    reasons: list[str]
    unblock_instructions: list[str]
    scope: str

    @field_validator("score")
    @classmethod
    def _bounded(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"Confidence must lie in [0, 1], got {v}")
        return v

    @property
    def unavailable_signals(self) -> list[str]:
        """Signals that should have applied but could not be measured."""
        return [s.name for s in self.signals if s.availability is SignalAvailability.UNAVAILABLE]

    @property
    def inapplicable_signals(self) -> list[str]:
        return [s.name for s in self.signals if s.availability is SignalAvailability.NOT_APPLICABLE]

    def sensitivity_table(self) -> list[dict]:
        """Where the decision would flip. Makes an arbitrary cut point visible."""
        return [
            {"threshold": t, "would_abstain": self.score < t}
            for t in (0.30, 0.40, 0.50, 0.60, 0.70, 0.80)
        ]


class ConfidenceEngine:
    def __init__(
        self,
        provider: DuckDBSeriesProvider,
        store: ContractStore | None = None,
    ) -> None:
        self.provider = provider
        self.store = store or get_contract_store()

    # -- measured signals --------------------------------------------------

    @staticmethod
    def _statistically_untestable(hypothesis: HypothesisResult) -> bool:
        """True where no test could ever exist, as opposed to one that failed.

        A product launching from a zero baseline has no prior period to be
        compared against -- not now, and not after better instrumentation. That
        is structurally different from a test that failed for want of enough
        days, which is a real and fixable evidence gap. Scoring the two the same
        way drove genuine launches to near-zero confidence and an abstention,
        when the honest reading is "material event, no significance claim".
        """
        return (
            hypothesis.measurement.change_type is ChangeType.NEW_ACTIVITY
            or hypothesis.test.not_tested_reason is NotTestedReason.NO_BASELINE_OBSERVATIONS
        )

    @classmethod
    def _sample_size_signal(cls, hypothesis: HypothesisResult) -> tuple[float, SignalAvailability, str]:
        """Saturating in n, so more data always helps but with diminishing returns.

        n / (n + k) with k = 10: about 0.5 at ten observations per window and
        approaching 1 well before a hundred. Continuous by design -- the brief
        rules out a crude `if n < 10` cliff, and a cliff would also make the
        score jump discontinuously as a window widens by one day.
        """
        n = min(hypothesis.test.baseline_n, hypothesis.test.event_n)
        if n == 0:
            if cls._statistically_untestable(hypothesis):
                return (
                    0.0,
                    SignalAvailability.NOT_APPLICABLE,
                    "No prior period exists to compare against.",
                )
            return 0.0, SignalAvailability.UNAVAILABLE, "No observations backing a test."
        k = 10.0
        return (
            n / (n + k),
            SignalAvailability.MEASURED,
            f"{n} observations in the smaller of the two windows",
        )

    def _statistical_signal(
        self, hypothesis: HypothesisResult
    ) -> tuple[float, SignalAvailability, str]:
        """Distance below alpha, using the ADJUSTED p-value only."""
        if not hypothesis.test.tested or hypothesis.adjusted_p_value is None:
            availability = (
                SignalAvailability.NOT_APPLICABLE
                if self._statistically_untestable(hypothesis)
                else SignalAvailability.UNAVAILABLE
            )
            return (
                0.0,
                availability,
                f"No valid statistical test ({hypothesis.test.not_tested_reason}).",
            )
        alpha = self.store.detection.fdr_alpha
        adjusted = hypothesis.adjusted_p_value
        if adjusted >= alpha:
            return (
                0.0,
                SignalAvailability.MEASURED,
                f"Adjusted p = {adjusted:.3f} does not clear alpha = {alpha}",
            )
        # Log scale: p = alpha scores 0, p = alpha/1000 scores 1.
        strength = np.log10(alpha / max(adjusted, 1e-300)) / 3.0
        return (
            float(np.clip(strength, 0.0, 1.0)),
            SignalAvailability.MEASURED,
            f"Adjusted p = {adjusted:.2e} against alpha = {alpha}",
        )

    @classmethod
    def _effect_signal(cls, hypothesis: HypothesisResult) -> tuple[float, SignalAvailability, str]:
        if hypothesis.test.effect_size is None:
            if cls._statistically_untestable(hypothesis):
                return (
                    0.0,
                    SignalAvailability.NOT_APPLICABLE,
                    "Effect size is undefined without a baseline to compare against.",
                )
            return 0.0, SignalAvailability.UNAVAILABLE, "No effect size available."
        magnitude = abs(hypothesis.test.effect_size)
        return (
            float(np.clip(magnitude, 0.0, 1.0)),
            SignalAvailability.MEASURED,
            f"Rank-biserial effect size {hypothesis.test.effect_size:+.3f}",
        )

    @staticmethod
    def _completeness_signal(
        hypothesis: HypothesisResult,
    ) -> tuple[float, SignalAvailability, str]:
        coverage = hypothesis.event_coverage
        if coverage.expected_days == 0:
            return 0.0, SignalAvailability.UNAVAILABLE, "Event window is empty."
        return (
            float(np.clip(coverage.coverage, 0.0, 1.0)),
            SignalAvailability.MEASURED,
            f"{coverage.observed_days}/{coverage.expected_days} days observed in the event window",
        )

    def _attribution_signal(
        self, kpi: str, start: date, end: date, entity_filter: str | None
    ) -> tuple[float, SignalAvailability, str]:
        """Share of orders in the window carrying attribution tags.

        Applicable only to KPIs that actually consume attribution data, which the
        contract already says via each KPI's declared sources.
        """
        if "marketing_spend" not in self.store.kpi(kpi).sources:
            return (
                0.0,
                SignalAvailability.NOT_APPLICABLE,
                f"{kpi} is measured without attribution data.",
            )
        params: list = [start, end]
        clause = ""
        if entity_filter:
            clause = " AND category = ?"
            params.append(entity_filter)
        row = self.provider.conn.execute(
            f"""
            SELECT count(*), count(utm_source) FROM pos_orders
            WHERE CAST(date AS DATE) >= ? AND CAST(date AS DATE) < ?{clause}
            """,
            params,
        ).fetchone()
        total, tagged = int(row[0] or 0), int(row[1] or 0)
        if total == 0:
            return 0.0, SignalAvailability.UNAVAILABLE, "No orders in the window to assess."
        return (
            tagged / total,
            SignalAvailability.MEASURED,
            f"{tagged:,}/{total:,} orders carry attribution tags",
        )

    @staticmethod
    def _consistency_signal(
        report: ContradictionReport,
    ) -> tuple[float, SignalAvailability, str]:
        if report.status is ReconciliationStatus.INSUFFICIENT_EVIDENCE:
            return (
                0.0,
                SignalAvailability.UNAVAILABLE,
                "No reconciliation check reached a verdict; agreement is unknown, not assumed.",
            )
        return (
            report.consistency_score(),
            SignalAvailability.MEASURED,
            (
                f"{len(report.contradictions)} of {len(report.conclusive_checks)} conclusive "
                f"checks disagree"
            ),
        )

    def _freshness(self, freshness_rows) -> tuple[float, list[str]]:
        per_day = self.store.confidence.staleness_penalty_per_day
        penalty = 0.0
        stale: list[str] = []
        for row in freshness_rows:
            if row.is_stale:
                penalty += per_day * (row.age_hours - row.sla_hours) / 24.0
                stale.append(row.source)
        return min(penalty, 0.5), stale

    # -- entry point -------------------------------------------------------

    def assess(
        self,
        hypothesis: HypothesisResult,
        contradictions: ContradictionReport,
        freshness_rows,
        entity_filter: str | None = None,
    ) -> ConfidenceAssessment:
        """Score the evidence behind one hypothesis, scoped to its event window.

        The window comes from the hypothesis itself, so a scoped analysis cannot
        silently inherit a wider period's signals -- the leak the audit found,
        where a one-day assessment reported a thirty-day consistency figure.
        """
        weights = self.store.confidence.weights
        start, end = hypothesis.event_coverage.start, hypothesis.event_coverage.end

        builders = {
            "sample_size": self._sample_size_signal(hypothesis),
            "statistical_evidence": self._statistical_signal(hypothesis),
            "effect_magnitude": self._effect_signal(hypothesis),
            "data_completeness": self._completeness_signal(hypothesis),
            "attribution_integrity": self._attribution_signal(
                hypothesis.kpi, start, end, entity_filter
            ),
            "cross_source_consistency": self._consistency_signal(contradictions),
        }

        signals = [
            ConfidenceSignal(
                name=name,
                value=value,
                weight=weights[name],
                availability=availability,
                measurement=measurement,
            )
            for name, (value, availability, measurement) in builders.items()
        ]

        # Renormalise over applicable signals so an inapplicable one neither
        # credits nor penalises: the score stays comparable across KPIs that
        # legitimately have different evidence available to them.
        applicable = [s for s in signals if s.is_applicable]
        applicable_weight = sum(s.weight for s in applicable)
        raw = (
            sum(s.contribution for s in applicable) / applicable_weight
            if applicable_weight > 0
            else 0.0
        )
        staleness_penalty, stale_sources = self._freshness(freshness_rows)

        # Contradiction is a penalty only -- it can never raise the score -- and
        # only contradictions that bear on this KPI count against it.
        relevant_contradictions = contradictions.contradictions_for(hypothesis.kpi)
        contradiction_penalty = 0.15 * len(relevant_contradictions)

        penalties = {
            "staleness": staleness_penalty,
            "contradiction": contradiction_penalty,
        }
        score = float(np.clip(raw - staleness_penalty - contradiction_penalty, 0.0, 1.0))

        measured = [s for s in applicable if s.availability is SignalAvailability.MEASURED]
        evidence_coverage = len(measured) / len(applicable) if applicable else 0.0

        reasons, unblock = self._explain(
            hypothesis, contradictions, signals, stale_sources, entity_filter
        )

        decision = self._decide(hypothesis, score)
        scope = f"{start.isoformat()} to {end.isoformat()}"
        if entity_filter:
            scope += f", {entity_filter}"

        return ConfidenceAssessment(
            score=score,
            signals=signals,
            evidence_coverage=evidence_coverage,
            penalties=penalties,
            abstain_threshold=self.store.confidence.abstain_threshold,
            low_confidence_threshold=self.store.confidence.low_confidence_threshold,
            decision=decision,
            reasons=reasons,
            unblock_instructions=unblock,
            scope=scope,
        )

    def _decide(self, hypothesis: HypothesisResult, score: float) -> Decision:
        """Confidence can only downgrade a detection, never upgrade a rejection."""
        if not hypothesis.decision.is_reportable:
            return hypothesis.decision
        if score < self.store.confidence.abstain_threshold:
            return Decision.ABSTAIN
        if score < self.store.confidence.low_confidence_threshold:
            return Decision.LOW_CONFIDENCE
        return hypothesis.decision

    def _explain(
        self,
        hypothesis: HypothesisResult,
        contradictions: ContradictionReport,
        signals: list[ConfidenceSignal],
        stale_sources: list[str],
        entity_filter: str | None,
    ) -> tuple[list[str], list[str]]:
        reasons: list[str] = []
        unblock: list[str] = []

        for signal in signals:
            if signal.availability is SignalAvailability.UNAVAILABLE:
                reasons.append(f"{signal.name} could not be measured: {signal.measurement}")

        attribution = next(s for s in signals if s.name == "attribution_integrity")
        if attribution.availability is SignalAvailability.MEASURED and attribution.value < 0.95:
            reasons.append(
                f"{1 - attribution.value:.1%} of orders in scope are missing attribution "
                f"parameters ({attribution.measurement})."
            )
            unblock.append(
                "Verify the server-side tag container and re-sync attribution parameters "
                "before drawing channel-level conclusions."
            )

        for check in contradictions.contradictions_for(hypothesis.kpi):
            reasons.append(f"{check.label}: {check.detail}")
            unblock.append(f"Reconcile {check.label.lower()} before acting on this finding.")

        if stale_sources:
            reasons.append(f"Sources past their freshness SLA: {', '.join(stale_sources)}.")
            unblock.append(f"Re-run ingestion for {', '.join(stale_sources)}.")

        if not hypothesis.test.tested:
            reasons.append(
                f"No statistical test was possible ({hypothesis.test.not_tested_reason}); "
                "this finding rests on business materiality alone."
            )

        return reasons, list(dict.fromkeys(unblock))
