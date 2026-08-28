"""The structured evidence object handed to the narrative layer.

This is the boundary of the system's quantitative authority. Everything numeric
is computed before this object is built; the narrative layer receives it and may
only describe what it contains.

Specifically, the narrative layer must NOT:

  * compute a percentage, a p-value, or a confidence score
  * decide whether a finding is significant
  * invent a driver, a threshold, or an owner
  * override or soften a decision made here

and MUST:

  * cite only values present in this package
  * repeat the decision verbatim
  * reproduce abstentions as abstentions, never as hedged findings

`assert_llm_safe()` enforces the invariants that make that boundary meaningful,
and the faithfulness verifier downstream checks that every numeral in a rendered
sentence resolves back to a value here.
"""

from __future__ import annotations

from datetime import date
from math import isfinite
from typing import Any

from pydantic import BaseModel

from app.change import ChangeMeasurement
from app.engines.confidence import ConfidenceAssessment
from app.engines.contradiction import ContradictionReport, ReconciliationStatus
from app.engines.detector import Decision, HypothesisResult
from app.fdr import FDRResult
from app.materiality import MaterialityDecision


class WindowSpec(BaseModel):
    start: date
    end: date
    days: int

    @classmethod
    def of(cls, start: date, end: date) -> WindowSpec:
        return cls(start=start, end=end, days=max(0, (end - start).days))


class StatisticalTestSpec(BaseModel):
    tested: bool
    test: str | None
    assumptions: list[str]
    p_value: float | None
    effect_size: float | None
    effect_measure: str | None
    baseline_n: int
    event_n: int
    not_tested_reason: str | None


class FDRSpec(BaseModel):
    tested: bool
    method: str | None
    dependence_assumption: str | None
    alpha: float
    hypotheses_in_pool: int
    raw_p_value: float | None
    adjusted_p_value: float | None
    significant_after_fdr: bool


class DriverSpec(BaseModel):
    name: str
    label: str
    contribution: float
    unit: str
    method: str
    evidence_tier: str


class DataQualitySpec(BaseModel):
    event_window_coverage: float
    baseline_window_coverage: float
    evidence_coverage: float
    unavailable_signals: list[str]
    stale_sources: list[str]
    reconciliation_status: str


class EvidencePackage(BaseModel):
    """One fully-evidenced finding, ready for narration and nothing else."""

    kpi: str
    entity: str
    dimension: str
    label: str
    unit: str

    baseline_window: WindowSpec
    event_window: WindowSpec
    windows_equal_length: bool
    baseline_mode: str
    baseline_scale: float

    baseline_value: float | None
    current_value: float | None
    observed_change: ChangeMeasurement
    materiality: MaterialityDecision
    statistical_test: StatisticalTestSpec
    fdr: FDRSpec

    drivers: list[DriverSpec]
    supporting_evidence: list[str]
    contradicting_evidence: list[str]
    # Figures quoted inside the evidence prose above. They are computed facts
    # like any other, so they must be citable -- otherwise a narrative that
    # repeats a caveat verbatim fails verification for quoting a number the
    # package never declared.
    evidence_values: dict[str, float] = {}
    data_quality: DataQualitySpec

    confidence: float
    confidence_scale: str
    confidence_is_calibrated: bool
    decision: Decision
    decision_reason: str
    unblock_instructions: list[str]

    narration_rules: list[str] = [
        "Cite only numbers present in this package.",
        "Do not compute any new quantity, including percentages.",
        "Report the decision exactly as given; do not soften an ABSTAIN.",
        "Do not introduce drivers, owners, or thresholds not listed here.",
        "State the confidence scale as given; it is a rubric, not a probability.",
    ]

    # -- invariants --------------------------------------------------------

    def assert_llm_safe(self) -> None:
        """Fail loudly rather than hand a malformed package to a narrator."""
        for name, value in self._numeric_fields().items():
            if value is not None and not isfinite(value):
                raise ValueError(f"Non-finite value in evidence package: {name} = {value!r}")

        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"Confidence outside [0, 1]: {self.confidence}")

        if self.fdr.tested:
            if self.fdr.adjusted_p_value is None:
                raise ValueError("A tested hypothesis must carry an adjusted p-value.")
            if self.fdr.raw_p_value is not None and (
                self.fdr.adjusted_p_value < self.fdr.raw_p_value - 1e-12
            ):
                raise ValueError(
                    "Adjusted p-value below the raw p-value; correction can only inflate."
                )
            # The decision must follow the adjusted p-value, never the raw one.
            expected = self.fdr.adjusted_p_value <= self.fdr.alpha
            if self.fdr.significant_after_fdr != expected:
                raise ValueError(
                    "significant_after_fdr disagrees with the adjusted p-value; a decision "
                    "somewhere is using a raw p-value."
                )
        elif self.decision is Decision.DETECTED:
            raise ValueError(
                "DETECTED requires a completed statistical test. An untested hypothesis may "
                "reach LOW_CONFIDENCE at most."
            )

        if self.decision is Decision.ABSTAIN and not self.decision_reason:
            raise ValueError("An abstention must state why.")

    def _numeric_fields(self) -> dict[str, float | None]:
        return {
            "baseline_value": self.baseline_value,
            "current_value": self.current_value,
            "absolute_change": self.observed_change.absolute_change,
            "relative_change_pct": self.observed_change.relative_change_pct,
            "exceedance": self.materiality.exceedance,
            "p_value": self.statistical_test.p_value,
            "effect_size": self.statistical_test.effect_size,
            "adjusted_p_value": self.fdr.adjusted_p_value,
            "confidence": self.confidence,
            **{f"driver[{d.name}]": d.contribution for d in self.drivers},
            **self.evidence_values,
        }

    def citable_values(self) -> dict[str, float]:
        """Every number a narrator is allowed to state."""
        return {k: v for k, v in self._numeric_fields().items() if v is not None}


def build_evidence_package(
    hypothesis: HypothesisResult,
    confidence: ConfidenceAssessment,
    contradictions: ContradictionReport,
    fdr_result: FDRResult,
    *,
    drivers: list[DriverSpec] | None = None,
    stale_sources: list[str] | None = None,
) -> EvidencePackage:
    """Assemble the package and verify it before it leaves the engine."""
    test = hypothesis.test
    supporting: list[str] = []
    contradicting: list[str] = []

    evidence_values: dict[str, float] = {}
    for check in contradictions.checks:
        if check.status is ReconciliationStatus.CONSISTENT:
            supporting.append(f"{check.label}: {check.detail}")
        elif check.status is ReconciliationStatus.CONTRADICTORY:
            contradicting.append(f"{check.label}: {check.detail}")
        for field_name in ("observed", "explained", "unexplained", "divergence", "tolerance"):
            value = getattr(check, field_name, None)
            if value is not None:
                evidence_values[f"{check.name}.{field_name}"] = float(value)

    if hypothesis.materiality.is_material:
        supporting.append(hypothesis.materiality.reason)
    if test.tested and hypothesis.significant_after_fdr:
        supporting.append(hypothesis.decision_reason)
    if not test.tested and test.note:
        contradicting.append(f"No statistical test: {test.note}")

    package = EvidencePackage(
        kpi=hypothesis.kpi,
        entity=hypothesis.entity,
        dimension=hypothesis.dimension,
        label=hypothesis.label,
        unit=hypothesis.unit,
        baseline_window=WindowSpec.of(
            hypothesis.baseline_coverage.start, hypothesis.baseline_coverage.end
        ),
        event_window=WindowSpec.of(
            hypothesis.event_coverage.start, hypothesis.event_coverage.end
        ),
        windows_equal_length=hypothesis.windows_equal_length,
        baseline_mode=hypothesis.baseline_mode.value,
        baseline_scale=hypothesis.baseline_scale,
        baseline_value=hypothesis.measurement.baseline,
        current_value=hypothesis.measurement.current,
        observed_change=hypothesis.measurement,
        materiality=hypothesis.materiality,
        statistical_test=StatisticalTestSpec(
            tested=test.tested,
            test=test.test_name,
            assumptions=(
                [
                    "Two independent samples of daily observations.",
                    "No distributional form assumed (non-parametric rank test).",
                    "Observations within a window treated as exchangeable.",
                ]
                if test.tested
                else []
            ),
            p_value=test.p_value,
            effect_size=test.effect_size,
            effect_measure="rank_biserial_correlation" if test.tested else None,
            baseline_n=test.baseline_n,
            event_n=test.event_n,
            not_tested_reason=(
                test.not_tested_reason.value if test.not_tested_reason else None
            ),
        ),
        fdr=FDRSpec(
            tested=test.tested,
            method=fdr_result.method.value if test.tested else None,
            dependence_assumption=(
                fdr_result.dependence_assumption if test.tested else None
            ),
            alpha=fdr_result.alpha,
            hypotheses_in_pool=fdr_result.m_tested,
            raw_p_value=test.p_value,
            adjusted_p_value=hypothesis.adjusted_p_value,
            significant_after_fdr=hypothesis.significant_after_fdr,
        ),
        drivers=drivers or [],
        supporting_evidence=supporting,
        contradicting_evidence=contradicting,
        evidence_values=evidence_values,
        data_quality=DataQualitySpec(
            event_window_coverage=hypothesis.event_coverage.coverage,
            baseline_window_coverage=hypothesis.baseline_coverage.coverage,
            evidence_coverage=confidence.evidence_coverage,
            unavailable_signals=confidence.unavailable_signals,
            stale_sources=stale_sources or [],
            reconciliation_status=contradictions.status.value,
        ),
        confidence=confidence.score,
        confidence_scale=confidence.scale,
        confidence_is_calibrated=confidence.is_calibrated,
        decision=confidence.decision,
        decision_reason=hypothesis.decision_reason,
        unblock_instructions=confidence.unblock_instructions,
    )
    package.assert_llm_safe()
    return package
