"""Statistical helpers, checked against hand-computed values."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.stats import benjamini_hochberg, pct_change, robust_z


def test_bh_on_a_uniform_ladder():
    """p = [.01 .. .05] with m=5: every q collapses to 0.05."""
    assert benjamini_hochberg([0.01, 0.02, 0.03, 0.04, 0.05]) == pytest.approx(
        [0.05] * 5, abs=1e-12
    )


def test_bh_hand_computed():
    """q_(i) = min over j>=i of (m/j) * p_(j)."""
    assert benjamini_hochberg([0.001, 0.5, 0.9]) == pytest.approx(
        [0.003, 0.75, 0.9], abs=1e-12
    )


def test_bh_preserves_input_order():
    """Adjustment happens on the sorted sequence but returns in input order."""
    assert benjamini_hochberg([0.9, 0.001, 0.5]) == pytest.approx(
        [0.9, 0.003, 0.75], abs=1e-12
    )


def test_bh_on_empty_input():
    assert benjamini_hochberg([]) == []


@settings(max_examples=200, deadline=None)
@given(st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=1, max_size=40))
def test_bh_q_values_are_bounded_and_never_below_p(p_values):
    q_values = benjamini_hochberg(p_values)
    for p, q in zip(p_values, q_values):
        assert 0.0 <= q <= 1.0
        # BH only ever inflates a p-value; a q below its p would be nonsense.
        assert q >= p - 1e-12


@settings(max_examples=200, deadline=None)
@given(st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=2, max_size=40))
def test_bh_is_monotone_in_p(p_values):
    """A smaller p-value can never receive a larger q-value."""
    pairs = sorted(zip(p_values, benjamini_hochberg(p_values)))
    for (_, q_lo), (_, q_hi) in zip(pairs, pairs[1:]):
        assert q_lo <= q_hi + 1e-12


def test_bh_is_more_powerful_than_bonferroni():
    """The reason to prefer FDR control: it rejects at least as much."""
    p_values = [0.001, 0.008, 0.02, 0.04, 0.2]
    m = len(p_values)
    alpha = 0.05

    bh_rejections = sum(1 for q in benjamini_hochberg(p_values) if q <= alpha)
    bonferroni_rejections = sum(1 for p in p_values if p <= alpha / m)

    assert bh_rejections >= bonferroni_rejections
    assert bh_rejections > bonferroni_rejections, "expected FDR to be strictly more powerful here"


def test_robust_z_is_not_fooled_by_a_single_spike():
    """The point of MAD: an outlier inflates sigma enough to hide itself."""
    import numpy as np

    baseline = [10.0] * 20 + [500.0]
    value = 60.0

    mad_based = robust_z(baseline, value)
    sd = float(np.std(baseline))
    sd_based = (value - float(np.mean(baseline))) / sd

    assert abs(mad_based) > abs(sd_based)


def test_robust_z_handles_a_constant_baseline():
    assert robust_z([5.0] * 10, 5.0) == 0.0
    assert robust_z([5.0] * 10, 99.0) == 0.0  # zero spread: no scale to divide by


def test_robust_z_needs_two_points():
    assert robust_z([1.0], 5.0) == 0.0


def test_pct_change_returns_none_from_a_zero_base():
    """Growth from nothing is undefined, not infinite.

    Returning inf would let any new product sort to the top of every ranking on
    an artefact of the arithmetic.
    """
    assert pct_change(0.0, 100.0) is None
    assert pct_change(0.0, 0.0) == 0.0
    assert pct_change(100.0, 150.0) == pytest.approx(50.0)
    assert pct_change(-100.0, -150.0) == pytest.approx(-50.0)
