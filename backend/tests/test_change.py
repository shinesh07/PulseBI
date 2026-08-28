"""Phase 1 and 2: explicit change classification, and no infinite growth rates."""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.change import ChangeType, measure_change
from app.contracts import Materiality
from app.materiality import MAX_EXCEEDANCE, assess_materiality

# -- the classification table ----------------------------------------------


@pytest.mark.parametrize(
    "baseline,current,expected",
    [
        (None, 100.0, ChangeType.NO_PRIOR_BASELINE),
        (100.0, None, ChangeType.NO_CURRENT_VALUE),
        (0.0, 0.0, ChangeType.NO_ACTIVITY),
        (0.0, 100.0, ChangeType.NEW_ACTIVITY),
        (100.0, 0.0, ChangeType.CEASED_ACTIVITY),
        (100.0, 100.0, ChangeType.NO_CHANGE),
        (100.0, 150.0, ChangeType.INCREASE),
        (100.0, 50.0, ChangeType.DECREASE),
        (-50.0, 50.0, ChangeType.INCREASE),
    ],
)
def test_change_classification(baseline, current, expected):
    assert measure_change(baseline, current).change_type is expected


def test_new_activity_has_no_relative_change():
    """0 -> 100 is a real event, but there is no valid percentage for it."""
    result = measure_change(0.0, 100.0)
    assert result.change_type is ChangeType.NEW_ACTIVITY
    assert result.relative_change_pct is None
    assert result.absolute_change == 100.0
    assert "undefined" in result.reason


def test_near_zero_baseline_is_classified_not_exploded():
    """The audit found 1e-12 -> 100 producing a 1e16% change.

    That number then propagated into materiality scoring and dominated every
    ranking. With a declared floor it is classified instead of computed.
    """
    result = measure_change(1e-12, 100.0, baseline_floor=1.0)
    assert result.change_type is ChangeType.UNSTABLE_BASELINE
    assert result.relative_change_pct is None
    assert result.absolute_change == pytest.approx(100.0)


def test_baseline_above_the_floor_still_gets_a_ratio():
    result = measure_change(10.0, 20.0, baseline_floor=1.0)
    assert result.change_type is ChangeType.INCREASE
    assert result.relative_change_pct == pytest.approx(100.0)


def test_ceased_activity_is_minus_one_hundred_percent():
    result = measure_change(100.0, 0.0)
    assert result.relative_change_pct == pytest.approx(-100.0)


def test_negative_baseline_uses_magnitude_for_the_ratio():
    """Direction comes from the absolute change, not from the sign of the base."""
    result = measure_change(-100.0, -50.0)
    assert result.absolute_change == pytest.approx(50.0)
    assert result.relative_change_pct == pytest.approx(50.0)
    assert result.direction == "up"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_inputs_are_rejected(bad):
    with pytest.raises(ValueError, match="Non-finite"):
        measure_change(bad, 100.0)
    with pytest.raises(ValueError, match="Non-finite"):
        measure_change(100.0, bad)


def test_every_measurement_carries_a_reason():
    for baseline, current in [(None, 1.0), (1.0, None), (0.0, 0.0), (0.0, 5.0), (5.0, 0.0)]:
        assert measure_change(baseline, current).reason


# -- no non-finite value may escape ----------------------------------------


@settings(max_examples=400, deadline=None)
@given(
    st.one_of(
        st.none(),
        st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False),
    ),
    st.one_of(
        st.none(),
        st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False),
    ),
    st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
)
def test_no_measurement_ever_contains_a_non_finite_value(baseline, current, floor):
    result = measure_change(baseline, current, baseline_floor=floor)
    for value in (result.absolute_change, result.relative_change_pct):
        assert value is None or math.isfinite(value)


# -- materiality is separate from significance -----------------------------


def test_materiality_uses_absolute_gate_when_ratio_is_undefined():
    """A new product's importance is its absolute contribution."""
    thresholds = Materiality(abs_usd=1000.0, pct=10.0, baseline_floor=100.0)
    decision = assess_materiality(measure_change(0.0, 50_000.0, baseline_floor=100.0), thresholds)

    assert decision.is_material
    assert decision.gates_evaluated == ["absolute"]
    assert "baseline is zero" in decision.reason


def test_zero_threshold_means_the_gate_does_not_apply():
    """Previously abs_usd=0 made every movement trivially material."""
    thresholds = Materiality(abs_usd=0.0, pct=10.0, baseline_floor=0.5)
    decision = assess_materiality(measure_change(30.0, 30.1, baseline_floor=0.5), thresholds)

    assert not decision.is_material
    assert "absolute" not in decision.gates_evaluated


def test_exceedance_is_capped_so_a_degenerate_baseline_cannot_dominate():
    thresholds = Materiality(abs_usd=1e-9, pct=1e-9, baseline_floor=0.0)
    decision = assess_materiality(measure_change(1.0, 1e9), thresholds)

    assert decision.exceedance <= MAX_EXCEEDANCE
    assert math.isfinite(decision.exceedance)


def test_unmeasurable_change_is_not_material():
    thresholds = Materiality(abs_usd=1.0, pct=1.0, baseline_floor=0.0)
    decision = assess_materiality(measure_change(None, 100.0), thresholds)

    assert not decision.is_material
    assert not decision.was_gated


def test_no_applicable_gate_is_distinct_from_failing_one():
    """Both thresholds off and no ratio: nothing could be judged."""
    thresholds = Materiality(abs_usd=0.0, pct=10.0, baseline_floor=1.0)
    decision = assess_materiality(measure_change(0.0, 100.0, baseline_floor=1.0), thresholds)

    assert not decision.is_material
    assert not decision.was_gated
    assert "No materiality gate" in decision.reason


def test_materiality_decision_always_explains_itself():
    thresholds = Materiality(abs_usd=100.0, pct=5.0, baseline_floor=1.0)
    for baseline, current in [(1000.0, 1001.0), (1000.0, 2000.0), (0.0, 500.0)]:
        assert assess_materiality(measure_change(baseline, current), thresholds).reason
