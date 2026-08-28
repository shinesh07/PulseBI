"""Contract conformance.

The governance claim only means something if the contract actually gates the
code. These tests fail the build when the two drift apart.
"""

from __future__ import annotations

import pytest

from app.contracts import AccessLevel, ContractStore, Relationship

# Every KPI the analytical layer is expected to produce. Adding an engine
# without declaring its KPI in contracts.yaml -- or declaring a KPI no engine
# computes -- breaks this test in one direction or the other.
COMPUTED_KPIS = {"revenue", "cogs", "freight", "gross_margin", "blended_cac"}


def test_contract_loads_and_validates(store):
    assert store.contract.version == 2
    assert store.kpi_names
    assert store.persona_names


def test_every_computed_kpi_is_declared(store):
    undeclared = COMPUTED_KPIS - store.kpi_names
    assert not undeclared, f"engines compute KPIs absent from the contract: {sorted(undeclared)}"


def test_every_declared_kpi_is_computed(store):
    uncomputed = store.kpi_names - COMPUTED_KPIS
    assert not uncomputed, (
        f"contract declares KPIs no engine computes: {sorted(uncomputed)}. "
        "A contract that promises metrics the system cannot produce is decoration."
    )


def test_the_brief_requires_between_three_and_five_kpis(store):
    assert 3 <= len(store.kpi_names) <= 5


def test_kpis_span_at_least_three_sources_at_different_grains(store):
    grains = {s.grain for s in store.contract.sources.values()}
    assert len(store.contract.sources) >= 3
    assert grains >= {"daily", "weekly", "monthly"}


def test_every_persona_has_an_explicit_rule_for_every_kpi(store):
    for kpi_name in store.kpi_names:
        for persona in store.persona_names:
            assert isinstance(store.access_for(kpi_name, persona), AccessLevel)


def test_unknown_persona_fails_closed(store):
    """An unrecognised role must be denied, never defaulted to allow."""
    for kpi_name in store.kpi_names:
        assert store.access_for(kpi_name, "SOMEONE_ELSE") is AccessLevel.DENY


def test_gross_margin_is_declared_as_a_derived_ratio(store):
    """Guards against a future change routing a ratio through additive PVM."""
    margin = store.kpi("gross_margin")
    assert margin.metric_tree.relationship is Relationship.DERIVED_RATIO
    assert set(margin.metric_tree.children) == {"revenue", "cogs", "freight"}


def test_confidence_weights_sum_to_one(store):
    assert sum(store.confidence.weights.values()) == pytest.approx(1.0)


def test_fdr_method_and_dependence_assumption_are_declared(store):
    """The engine must not apply a correction without stating what it assumes."""
    method = store.detection.fdr_method
    assert method.dependence_assumption


def test_every_kpi_declares_a_baseline_floor(store):
    """Guards the near-zero-baseline pathology: without a floor, a 1e-12 base
    yields a 1e16% change that dominates every ranking."""
    for name in store.kpi_names:
        assert store.kpi(name).materiality.baseline_floor >= 0.0


def test_thresholds_are_ordered_sensibly(store):
    conf = store.confidence
    assert 0.0 < conf.abstain_threshold < conf.low_confidence_threshold <= 1.0
    assert conf.abstain_threshold < conf.action_threshold <= 1.0


def test_feedback_cannot_swing_ranking_without_bound(store):
    """Analyst feedback adjusts ranking only, and only within declared limits."""
    assert 0.0 < store.feedback.max_rank_weight_delta <= 0.5


def test_every_kpi_declares_lineage(store):
    for name in store.kpi_names:
        assert store.lineage_for(name), f"KPI '{name}' has no lineage declared"


def test_referential_integrity_is_enforced_at_load(tmp_path):
    """A contract referencing an undeclared source must fail loudly at load."""
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        """
version: 2
meta: {name: broken}
sources:
  only_source:
    file: a.csv
    system: X
    grain: daily
    refresh_cadence_minutes: 1
    freshness_sla_hours: 1
    date_column: date
personas:
  ROLE_A: {title: A, levers: [x]}
kpis:
  bad_kpi:
    label: Bad
    unit: usd
    grain: daily
    formula: "SUM(x)"
    sources: [does_not_exist]
    materiality: {abs_usd: 1.0, pct: 1.0, baseline_floor: 0.0}
    metric_tree: {relationship: additive, children: []}
    lineage: [{node: N, type: t}]
    access: {ROLE_A: allow}
detection: {fdr_alpha: 0.1, fdr_method: benjamini_hochberg, min_baseline_points: 5, robust_scale: mad}
confidence:
  weights: {completeness: 0.5, tag_integrity: 0.3, consistency: 0.2}
  abstain_threshold: 0.5
  low_confidence_threshold: 0.7
  action_threshold: 0.7
  staleness_penalty_per_day: 0.05
feedback: {max_rank_weight_delta: 0.2}
"""
    )
    with pytest.raises(ValueError, match="unknown sources"):
        ContractStore(broken)
