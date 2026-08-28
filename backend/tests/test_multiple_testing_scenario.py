"""Phase 9: prove the correction is load-bearing, not decorative.

The headline dataset has too few hypotheses for multiplicity correction to
change any decision. This suite runs the wide catalogue, where each product's
ground truth is known by construction, and asserts what the correction actually
buys.
"""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd
import pytest

from app import fdr
from app.contracts import get_contract_store
from app.data.wide_scenario import EffectClass, build_catalogue, generate
from app.engines.detector import Decision, MovementDetector
from app.series import DuckDBSeriesProvider, SeriesKey

BASELINE = (date(2023, 10, 1), date(2023, 11, 1))
EVENT = (date(2023, 11, 1), date(2023, 12, 1))
ALPHA = 0.1


@pytest.fixture(scope="module")
def wide():
    catalogue = build_catalogue()
    pos, erp = generate(catalogue)

    conn = duckdb.connect(":memory:")
    conn.register("_p", pos)
    conn.register("_e", erp)
    conn.execute("CREATE TABLE pos_orders AS SELECT * FROM _p")
    conn.execute("CREATE TABLE erp_financials AS SELECT * FROM _e")
    conn.execute(
        "CREATE TABLE marketing_spend(week_start VARCHAR, channel VARCHAR, "
        "spend DOUBLE, impressions BIGINT, clicks BIGINT, new_customers BIGINT)"
    )

    store = get_contract_store()
    provider = DuckDBSeriesProvider(conn, store)
    keys = [SeriesKey("revenue", "product", spec.product_id) for spec in catalogue]
    result = MovementDetector(provider, store).detect(BASELINE, EVENT, keys=keys)

    truth = {spec.product_id: spec.effect_class for spec in catalogue}
    yield catalogue, result, truth
    conn.close()


def _pool(result):
    return {h.key: h.test.p_value for h in result.hypotheses if h.test.tested}


def _classes(keys, truth):
    counts: dict[str, int] = {}
    for key in keys:
        entity = key.split("/")[-1]
        label = truth[entity].value
        counts[label] = counts.get(label, 0) + 1
    return counts


# -- the pool is large enough to matter ------------------------------------


def test_the_scenario_has_enough_simultaneous_hypotheses(wide):
    _, result, _ = wide
    assert result.hypotheses_tested >= 20, (
        "multiplicity correction cannot demonstrate anything on a handful of tests"
    )


def test_the_catalogue_spans_all_three_effect_classes(wide):
    catalogue, _, _ = wide
    present = {spec.effect_class for spec in catalogue}
    assert present == set(EffectClass)


# -- correction changes the outcome ----------------------------------------


def test_correction_rejects_fewer_hypotheses_than_a_raw_threshold(wide):
    _, result, truth = wide
    corrected = fdr.correct(_pool(result), alpha=ALPHA)

    raw = [k for k, v in corrected.corrected.items() if v.raw_p_value <= ALPHA]
    adjusted = [k for k, v in corrected.corrected.items() if v.significant]

    assert len(adjusted) < len(raw), "on this scenario the correction must bite"
    assert corrected.changed_by_correction


def test_correction_trims_borderline_results_not_true_effects(wide):
    """The correction should cost power at the margin, not lose real signal."""
    _, result, truth = wide
    corrected = fdr.correct(_pool(result), alpha=ALPHA)

    removed = _classes(corrected.changed_by_correction, truth)
    assert removed.get(EffectClass.TRUE_EFFECT.value, 0) == 0, (
        "a genuine effect must not be discarded by the correction"
    )
    assert removed.get(EffectClass.BORDERLINE.value, 0) > 0


def test_all_true_effects_survive_correction(wide):
    catalogue, result, truth = wide
    corrected = fdr.correct(_pool(result), alpha=ALPHA)
    surviving = _classes(
        [k for k, v in corrected.corrected.items() if v.significant], truth
    )
    expected = sum(1 for s in catalogue if s.effect_class is EffectClass.TRUE_EFFECT)

    assert surviving.get(EffectClass.TRUE_EFFECT.value, 0) == expected


def test_by_is_stricter_than_bh_on_the_same_evidence(wide):
    _, result, _ = wide
    pool = _pool(result)

    bh = fdr.correct(pool, alpha=ALPHA, method=fdr.FDRMethod.BENJAMINI_HOCHBERG)
    by = fdr.correct(pool, alpha=ALPHA, method=fdr.FDRMethod.BENJAMINI_YEKUTIELI)

    assert by.n_significant <= bh.n_significant
    assert len(by.changed_by_correction) >= len(bh.changed_by_correction)


def test_null_hypotheses_are_largely_rejected_after_correction(wide):
    """False-discovery control means few nulls survive."""
    _, result, truth = wide
    corrected = fdr.correct(_pool(result), alpha=ALPHA)
    accepted = _classes([k for k, v in corrected.corrected.items() if v.significant], truth)

    nulls_accepted = accepted.get(EffectClass.NULL.value, 0)
    total_accepted = sum(accepted.values())
    assert total_accepted > 0
    assert nulls_accepted / total_accepted <= ALPHA + 1e-9, (
        "observed false-discovery proportion should respect the target rate"
    )


# -- the detector uses the corrected decision ------------------------------


def test_detector_decisions_track_the_corrected_verdict(wide):
    _, result, _ = wide
    for hypothesis in result.hypotheses:
        if not hypothesis.test.tested:
            continue
        if hypothesis.decision is Decision.DETECTED:
            assert hypothesis.significant_after_fdr
        if hypothesis.decision is Decision.NOT_SIGNIFICANT:
            assert not hypothesis.significant_after_fdr


def test_some_raw_significant_hypotheses_are_not_detected(wide):
    """The observable proof that the pipeline uses adjusted rather than raw p."""
    _, result, _ = wide
    raw_only = [
        h
        for h in result.hypotheses
        if h.test.tested
        and h.test.p_value is not None
        and h.test.p_value <= ALPHA
        and not h.significant_after_fdr
    ]
    assert raw_only, "expected at least one hypothesis the correction overturned"
    for hypothesis in raw_only:
        assert hypothesis.decision is not Decision.DETECTED
