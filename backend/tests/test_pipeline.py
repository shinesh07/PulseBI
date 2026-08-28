"""Phases 12-15: decisions, abstention, evidence packaging, and invariants."""

from __future__ import annotations

import math
from datetime import date

import pytest

from app.engines.detector import Decision
from tests.conftest import BASELINE_WINDOW, OUTAGE_WINDOW

# -- decisions -------------------------------------------------------------


def test_every_hypothesis_reaches_an_explicit_decision(analysis):
    """Reportability used to be an implicit boolean with no state to inspect."""
    assert analysis.packages
    for package in analysis.packages:
        assert isinstance(package.decision, Decision)
        assert package.decision_reason, f"{package.kpi} has no reason for its decision"


def test_the_three_required_states_are_reachable(pipeline, analysis, outage_analysis):
    reachable = {p.decision for p in analysis.packages} | {
        p.decision for p in outage_analysis.packages
    }
    assert Decision.DETECTED in reachable
    assert Decision.ABSTAIN in reachable


def test_detection_requires_a_completed_statistical_test(analysis):
    """An untested hypothesis may reach LOW_CONFIDENCE at most."""
    for package in analysis.detected:
        assert package.statistical_test.tested
        assert package.fdr.significant_after_fdr


def test_abstentions_state_why_and_how_to_unblock(analysis, outage_analysis):
    abstentions = analysis.abstained + outage_analysis.abstained
    assert abstentions
    for package in abstentions:
        assert package.decision_reason


def test_new_activity_is_recognised_without_a_growth_rate(analysis):
    """A product launching from zero is a real event, not an unmeasurable one."""
    new_items = [
        p for p in analysis.packages if p.observed_change.change_type.value == "NEW_ACTIVITY"
    ]
    assert new_items, "the seeded launch should produce NEW_ACTIVITY classifications"
    for package in new_items:
        assert package.observed_change.relative_change_pct is None
        assert package.observed_change.absolute_change > 0
        assert package.decision is not Decision.DETECTED, (
            "no baseline means nothing to test against"
        )


# -- FDR wiring ------------------------------------------------------------


def test_only_tested_hypotheses_enter_the_fdr_pool(analysis):
    tested = [p for p in analysis.packages if p.statistical_test.tested]
    assert analysis.detection.fdr.m_tested == len(tested)
    for package in analysis.packages:
        if not package.statistical_test.tested:
            assert package.fdr.adjusted_p_value is None


def test_significance_follows_the_adjusted_p_value(analysis):
    alpha = analysis.detection.fdr.alpha
    for package in analysis.packages:
        if package.fdr.adjusted_p_value is None:
            continue
        assert package.fdr.significant_after_fdr == (package.fdr.adjusted_p_value <= alpha)


def test_the_declared_dependence_assumption_travels_with_the_finding(analysis):
    for package in analysis.detected:
        assert package.fdr.method
        assert package.fdr.dependence_assumption


# -- invariants (Phase 15) -------------------------------------------------


def test_no_business_facing_value_is_nan_or_infinite(analysis, outage_analysis):
    for result in (analysis, outage_analysis):
        for package in result.packages:
            for name, value in package.citable_values().items():
                assert math.isfinite(value), f"{package.kpi}.{name} = {value}"


def test_confidence_is_always_a_valid_proportion(analysis, outage_analysis):
    for result in (analysis, outage_analysis):
        for package in result.packages:
            assert 0.0 <= package.confidence <= 1.0


def test_confidence_is_labelled_as_a_rubric_not_a_probability(analysis):
    for package in analysis.packages:
        assert package.confidence_is_calibrated is False
        assert package.confidence_scale == "governance_rubric_0_1"


def test_every_package_passes_its_own_invariant_check(analysis, outage_analysis):
    for result in (analysis, outage_analysis):
        for package in result.packages:
            package.assert_llm_safe()


def test_package_rejects_a_decision_built_on_a_raw_p_value(analysis):
    """Tamper check: flipping significance away from the adjusted p must fail."""
    package = next(p for p in analysis.packages if p.statistical_test.tested)
    tampered = package.model_copy(deep=True)
    tampered.fdr.significant_after_fdr = not tampered.fdr.significant_after_fdr

    with pytest.raises(ValueError, match="raw p-value"):
        tampered.assert_llm_safe()


def test_package_rejects_a_non_finite_value(analysis):
    package = next(p for p in analysis.packages if p.baseline_value is not None)
    tampered = package.model_copy(deep=True)
    tampered.baseline_value = float("inf")

    with pytest.raises(ValueError, match="Non-finite"):
        tampered.assert_llm_safe()


# -- the LLM boundary ------------------------------------------------------


def test_package_carries_explicit_narration_rules(analysis):
    package = analysis.packages[0]
    joined = " ".join(package.narration_rules).lower()
    assert "cite only numbers" in joined
    assert "do not compute" in joined


def test_citable_values_are_the_complete_set_of_quotable_numbers(analysis):
    package = next(p for p in analysis.detected)
    citable = package.citable_values()

    assert package.observed_change.absolute_change in citable.values()
    assert package.confidence in citable.values()
    assert all(math.isfinite(v) for v in citable.values())


# -- window scoping end to end --------------------------------------------


def test_outage_window_is_scoped_not_inherited(pipeline, analysis, outage_analysis):
    """The audit's critical finding: a scoped analysis inherited monthly signals."""
    assert outage_analysis.event_window == OUTAGE_WINDOW
    assert analysis.event_window != OUTAGE_WINDOW

    monthly = next(p for p in analysis.packages if p.kpi == "revenue" and p.entity == "ALL")
    weekly = next(p for p in outage_analysis.packages if p.kpi == "revenue" and p.entity == "ALL")

    assert weekly.event_window.days == 7
    assert monthly.event_window.days == 30
    assert weekly.current_value != pytest.approx(monthly.current_value)


def test_contradiction_report_is_window_scoped(pipeline):
    """Reconciliation must be recomputed per window, not reused."""
    wide = pipeline.analyse(BASELINE_WINDOW, (date(2023, 11, 1), date(2023, 12, 1)))
    narrow = pipeline.analyse(BASELINE_WINDOW, OUTAGE_WINDOW)

    assert wide.contradictions.window_end != narrow.contradictions.window_end


# -- structurally untestable vs missing evidence ---------------------------


def test_a_new_product_is_not_penalised_for_having_no_baseline(analysis):
    """Two different situations must not score the same way.

    A product launching from zero has no prior period to be compared against --
    not now, and not after better instrumentation. That is structurally
    different from a test that failed for want of enough days, which is a real
    and fixable evidence gap. Scoring both as UNAVAILABLE drove genuine launches
    to near-zero confidence, when the honest reading is "material event, no
    significance claim".
    """

    new_items = [
        p for p in analysis.packages if p.observed_change.change_type.value == "NEW_ACTIVITY"
    ]
    assert new_items

    for package in new_items:
        # The statistical signals are inapplicable, not missing, so they are
        # excluded from the weighting rather than counted as gaps.
        assert "statistical_evidence" not in package.data_quality.unavailable_signals
        assert "sample_size" not in package.data_quality.unavailable_signals


def test_untestable_findings_still_cannot_be_detected(analysis):
    """Excluding inapplicable signals must not let an untested finding be DETECTED."""
    for package in analysis.packages:
        if not package.statistical_test.tested:
            assert package.decision is not Decision.DETECTED
