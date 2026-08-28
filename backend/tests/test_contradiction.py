"""Phase 5: contradictions from measured evidence, with a third outcome."""

from __future__ import annotations

from datetime import date

import pytest

from app.engines.contradiction import (
    ReconciliationStatus,
    reconcile_additive,
    reconcile_sources,
)

WINDOW = (date(2023, 11, 1), date(2023, 12, 1))


# -- additive reconciliation -----------------------------------------------


def test_parts_that_explain_the_whole_are_consistent():
    check = reconcile_additive(
        name="t", label="t", observed=100.0, parts={"a": 60.0, "b": 40.0}
    )
    assert check.status is ReconciliationStatus.CONSISTENT
    assert check.unexplained == pytest.approx(0.0)


def test_parts_that_fail_to_explain_the_whole_are_contradictory():
    check = reconcile_additive(
        name="t", label="t", observed=100.0, parts={"a": 30.0, "b": 20.0}
    )
    assert check.status is ReconciliationStatus.CONTRADICTORY
    assert check.unexplained == pytest.approx(50.0)
    assert check.divergence == pytest.approx(50.0)


def test_a_missing_part_is_insufficient_evidence_not_contradiction():
    """The distinction the brief insists on: absence is not disagreement."""
    check = reconcile_additive(
        name="t", label="t", observed=100.0, parts={"a": 60.0, "b": None}
    )
    assert check.status is ReconciliationStatus.INSUFFICIENT_EVIDENCE
    assert not check.is_contradictory


def test_a_missing_total_is_insufficient_evidence():
    check = reconcile_additive(name="t", label="t", observed=None, parts={"a": 1.0})
    assert check.status is ReconciliationStatus.INSUFFICIENT_EVIDENCE


def test_small_residual_within_tolerance_is_consistent():
    check = reconcile_additive(
        name="t", label="t", observed=1000.0, parts={"a": 999.0}, tolerance=0.02
    )
    assert check.status is ReconciliationStatus.CONSISTENT


def test_flat_total_with_moving_parts_is_contradictory():
    check = reconcile_additive(name="t", label="t", observed=0.0, parts={"a": 50.0})
    assert check.status is ReconciliationStatus.CONTRADICTORY


def test_everything_flat_is_consistent():
    check = reconcile_additive(name="t", label="t", observed=0.0, parts={"a": 0.0})
    assert check.status is ReconciliationStatus.CONSISTENT


# -- cross-source reconciliation -------------------------------------------


def test_sources_that_agree_are_consistent():
    check = reconcile_sources(
        name="t",
        label="t",
        growth_a=10.0,
        growth_b=12.0,
        source_a="A",
        source_b="B",
        likely_cause="",
    )
    assert check.status is ReconciliationStatus.CONSISTENT


def test_sources_that_diverge_widely_are_contradictory():
    check = reconcile_sources(
        name="t",
        label="t",
        growth_a=55.0,
        growth_b=5.0,
        source_a="A",
        source_b="B",
        likely_cause="tag loss",
    )
    assert check.status is ReconciliationStatus.CONTRADICTORY
    assert "tag loss" in check.detail


def test_an_absent_source_is_insufficient_evidence():
    check = reconcile_sources(
        name="t",
        label="t",
        growth_a=10.0,
        growth_b=None,
        source_a="A",
        source_b="B",
        likely_cause="",
    )
    assert check.status is ReconciliationStatus.INSUFFICIENT_EVIDENCE


# -- report semantics ------------------------------------------------------


def test_report_status_reflects_the_worst_outcome(analysis):
    report = analysis.contradictions
    if report.contradictions:
        assert report.status is ReconciliationStatus.CONTRADICTORY
    elif report.conclusive_checks:
        assert report.status is ReconciliationStatus.CONSISTENT
    else:
        assert report.status is ReconciliationStatus.INSUFFICIENT_EVIDENCE


def test_no_checks_is_insufficient_evidence_not_perfect_agreement(analysis):
    """The audit's critical confidence bug.

    consistency_score() previously returned 1.0 for an empty signal list, so a
    total measurement failure scored as flawless corroboration.
    """
    empty = analysis.contradictions.model_copy(update={"checks": []})
    assert empty.status is ReconciliationStatus.INSUFFICIENT_EVIDENCE
    assert empty.consistency_score() == 0.0
    assert empty.evidence_coverage == 0.0


def test_consistency_score_stays_inside_the_unit_interval(analysis):
    assert 0.0 <= analysis.contradictions.consistency_score() <= 1.0


def test_evidence_coverage_counts_only_conclusive_checks(analysis):
    report = analysis.contradictions
    assert report.evidence_coverage == pytest.approx(
        len(report.conclusive_checks) / len(report.checks)
    )


def test_checks_are_scoped_to_a_kpi_where_relevant(analysis):
    """An attribution failure must not condemn a KPI measured without attribution."""
    report = analysis.contradictions
    attribution = next(c for c in report.checks if c.name == "attribution_divergence")

    assert attribution.affected_kpis, "the attribution check must declare its scope"
    assert "revenue" not in attribution.affected_kpis
    assert report.contradictions_for("revenue") != report.contradictions_for("blended_cac")


def test_slice_additivity_is_checked_for_additive_kpis(analysis, store):
    from app.contracts import Relationship

    names = {c.name for c in analysis.contradictions.checks}
    for kpi in store.kpi_names:
        if store.kpi(kpi).metric_tree.relationship is Relationship.ADDITIVE:
            assert f"{kpi}_slice_additivity" in names


def test_contradiction_is_detected_from_the_data_not_a_hardcoded_string(analysis):
    """The seeded attribution outage must surface as a measured divergence."""
    attribution = next(
        c for c in analysis.contradictions.checks if c.name == "attribution_divergence"
    )
    assert attribution.is_contradictory
    assert attribution.divergence is not None
    assert abs(attribution.divergence) > attribution.tolerance
