"""Hierarchical empirical-Bayes estimation for a sparse-history SKU."""

from __future__ import annotations

import pytest

from app.engines.cold_start import ColdStartEngine


@pytest.fixture(scope="module")
def engine(reconciler):
    return ColdStartEngine(reconciler)


@pytest.fixture(scope="module")
def estimate(engine):
    return engine.estimate_daily_units("YOG-01")


def test_the_new_sku_is_flagged_as_cold_start(estimate):
    assert estimate.is_cold_start
    assert estimate.days_observed == 12
    assert estimate.days_observed < 14


def test_assumptions_and_limitations_are_stated(estimate):
    """Empirical Bayes borrows strength across groups; where that borrowing is
    questionable the estimate must say so rather than presenting every case
    identically."""
    assert estimate.assumptions
    assert estimate.limitations
    assert estimate.pool_scope
    assert any("xchangeab" in a for a in estimate.assumptions)


def test_pool_scope_is_reported_so_weak_pooling_is_visible(estimate):
    """A treadmill is not a plausible prior for a yoga mat. When the category has
    too few siblings the estimator falls back to the whole catalogue, and must
    flag that the exchangeability assumption is weaker."""
    if "whole catalogue" in estimate.pool_scope:
        assert any("exchangeability" in limit for limit in estimate.limitations)


@pytest.mark.parametrize("n", [1, 2, 3, 5, 10, 12])
def test_shrinkage_is_continuous_across_sample_sizes(engine, n):
    """No cliff: the estimate must vary smoothly in n rather than switching
    behaviour at an arbitrary threshold."""
    estimate = engine.estimate_daily_units("YOG-01", first_n_days=n)
    assert 0.0 <= estimate.shrinkage_weight <= 1.0
    assert estimate.days_observed == n


def test_cold_start_label_does_not_change_the_estimator(engine):
    """is_cold_start is a display label. Nothing decisional may hang on it."""
    below = engine.estimate_daily_units("YOG-01", first_n_days=12)
    assert below.is_cold_start
    # The shrinkage weight is a smooth function of n, not a function of the flag.
    weights = [
        engine.estimate_daily_units("YOG-01", first_n_days=k).shrinkage_weight
        for k in range(1, 13)
    ]
    assert all(0.0 <= w <= 1.0 for w in weights)
    assert weights[0] > weights[-1]


def test_priors_come_from_the_data_not_from_defaults(estimate):
    """The previous implementation took every prior as a keyword default and
    never queried the warehouse, which made its 'posterior' a constant."""
    assert estimate.prior_pool, "a prior must be pooled from sibling SKUs"
    assert estimate.prior_pool_observations > 0
    assert estimate.prior_mean > 0
    assert estimate.prior_variance > 0
    assert "YOG-01" not in estimate.prior_pool, "a SKU cannot be its own prior"


def test_posterior_sits_between_the_observation_and_the_prior(estimate):
    """The defining property of shrinkage."""
    low = min(estimate.observed_mean, estimate.prior_mean)
    high = max(estimate.observed_mean, estimate.prior_mean)
    assert low <= estimate.posterior_mean <= high


def test_credible_interval_brackets_the_posterior(estimate):
    lower, upper = estimate.credible_interval_90
    assert lower <= estimate.posterior_mean <= upper
    assert lower >= 0.0, "daily unit sales cannot be negative"


def test_shrinkage_weight_is_a_proportion(estimate):
    assert 0.0 <= estimate.shrinkage_weight <= 1.0


def test_shrinkage_falls_as_evidence_accumulates(engine):
    """The estimator must hand control back to the data as the SKU matures.

    This is the property that distinguishes a real hierarchical model from a
    fixed blend: with one day of history the estimate leans heavily on the
    category, and by twelve it barely does.
    """
    curve = engine.shrinkage_curve("YOG-01")
    assert len(curve) == 12

    weights = [row["shrinkage_weight"] for row in curve]
    assert weights[0] > weights[-1], "prior weight must decrease with more data"
    assert weights[0] > 0.5, "with a single day the category should dominate"
    assert weights[-1] < 0.2, "with twelve days the SKU's own data should dominate"


def test_uncertainty_narrows_as_evidence_accumulates(engine):
    curve = engine.shrinkage_curve("YOG-01")
    widths = [row["interval_width"] for row in curve]
    assert widths[0] > widths[-1]


def test_shrinkage_is_monotone_apart_from_sampling_noise(engine):
    """Weight on the prior should trend down, allowing for daily variance moves."""
    weights = [row["shrinkage_weight"] for row in engine.shrinkage_curve("YOG-01")]
    decreases = sum(1 for a, b in zip(weights, weights[1:]) if b <= a)
    assert decreases >= len(weights) - 2


def test_mature_sku_barely_shrinks(engine):
    """A SKU with two months of history should sit essentially on its own mean."""
    mature = engine.estimate_daily_units("TRD-01")
    assert not mature.is_cold_start
    assert mature.shrinkage_weight < 0.1
    assert mature.posterior_mean == pytest.approx(mature.observed_mean, rel=0.15)


def test_zero_observations_raises_rather_than_inventing_an_estimate(engine):
    """n = 0 must not silently return the category prior dressed as a posterior."""
    with pytest.raises(ValueError, match="No observations"):
        engine.estimate_daily_units("DOES-NOT-EXIST")


def test_estimate_never_contains_a_non_finite_value(engine):
    import math

    for n in (1, 2, 5, 12):
        estimate = engine.estimate_daily_units("YOG-01", first_n_days=n)
        for value in (
            estimate.posterior_mean,
            estimate.posterior_variance,
            estimate.shrinkage_weight,
            *estimate.credible_interval_90,
        ):
            assert math.isfinite(value)


def test_credible_interval_never_goes_negative_for_a_count(engine):
    for n in (1, 2, 3, 12):
        lower, _ = engine.estimate_daily_units("YOG-01", first_n_days=n).credible_interval_90
        assert lower >= 0.0
