"""Phases 8-10: correctness of the multiple-testing correction."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.fdr import FDRMethod, correct

ALPHA = 0.1


# -- pool hygiene ----------------------------------------------------------


def test_untested_hypotheses_are_excluded_from_the_pool():
    """The audit's core FDR bug.

    Untested candidates were inflating m, which enlarges every adjusted p-value
    and weakens genuine findings.
    """
    result = correct(
        {"a": 0.001, "b": None, "c": 0.002, "d": None},
        alpha=ALPHA,
    )
    assert result.m_tested == 2
    assert result.n_excluded == 2
    assert set(result.excluded_keys) == {"b", "d"}
    assert set(result.corrected) == {"a", "c"}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1, 1.5, None])
def test_invalid_p_values_are_excluded(bad):
    result = correct({"good": 0.01, "bad": bad}, alpha=ALPHA)
    assert result.m_tested == 1
    assert "bad" in result.excluded_keys


def test_excluding_untested_hypotheses_strengthens_real_findings():
    """Demonstrates the size of the bug: same evidence, different verdict."""
    tested = {f"h{i}": 0.02 for i in range(3)}
    padded = {**tested, **{f"pad{i}": None for i in range(60)}}

    honest = correct(tested, alpha=ALPHA)
    padded_result = correct(padded, alpha=ALPHA)

    assert honest.m_tested == 3
    assert padded_result.m_tested == 3, "None values must never enter m"
    assert honest.n_significant == padded_result.n_significant


def test_zero_tested_hypotheses():
    result = correct({"a": None}, alpha=ALPHA)
    assert result.m_tested == 0
    assert result.corrected == {}
    assert result.n_significant == 0


def test_one_tested_hypothesis_is_uncorrected():
    """With m=1 there is no multiplicity, so adjusted equals raw."""
    result = correct({"only": 0.04}, alpha=ALPHA)
    assert result.corrected["only"].adjusted_p_value == pytest.approx(0.04)
    assert result.is_significant("only")


def test_invalid_alpha_is_rejected():
    for alpha in (0.0, 1.0, -0.5, 2.0):
        with pytest.raises(ValueError, match="alpha"):
            correct({"a": 0.01}, alpha=alpha)


# -- identity preservation -------------------------------------------------


def test_hypothesis_identity_survives_sorting():
    """Sorting happens internally; a caller must never have to zip lists."""
    p_values = {"z": 0.9, "a": 0.001, "m": 0.05, "b": 0.4}
    result = correct(p_values, alpha=ALPHA)

    for key, raw in p_values.items():
        assert result.corrected[key].raw_p_value == pytest.approx(raw)

    # The smallest raw p must hold rank 1.
    assert result.corrected["a"].rank == 1
    assert result.corrected["z"].rank == 4


@settings(max_examples=200, deadline=None)
@given(
    st.dictionaries(
        st.text(min_size=1, max_size=6),
        st.floats(min_value=0.0, max_value=1.0),
        min_size=1,
        max_size=25,
    )
)
def test_every_key_keeps_its_own_p_value(p_values):
    result = correct(p_values, alpha=ALPHA)
    for key, raw in p_values.items():
        assert result.corrected[key].raw_p_value == pytest.approx(raw)


# -- procedure correctness -------------------------------------------------


def test_bh_matches_the_hand_computed_step_up():
    """q_(i) = min over j>=i of (m/j) * p_(j)."""
    result = correct({"a": 0.001, "b": 0.5, "c": 0.9}, alpha=ALPHA)
    assert result.corrected["a"].adjusted_p_value == pytest.approx(0.003)
    assert result.corrected["b"].adjusted_p_value == pytest.approx(0.75)
    assert result.corrected["c"].adjusted_p_value == pytest.approx(0.9)


def test_uniform_ladder_collapses_to_alpha():
    values = {f"h{i}": p for i, p in enumerate([0.01, 0.02, 0.03, 0.04, 0.05])}
    adjusted = {k: v.adjusted_p_value for k, v in correct(values, alpha=ALPHA).corrected.items()}
    assert all(a == pytest.approx(0.05) for a in adjusted.values())


@settings(max_examples=300, deadline=None)
@given(st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=1, max_size=30))
def test_adjusted_p_never_falls_below_raw_and_stays_bounded(p_values):
    """Correction can only ever inflate a p-value."""
    values = {f"h{i}": p for i, p in enumerate(p_values)}
    for method in FDRMethod:
        for entry in correct(values, alpha=ALPHA, method=method).corrected.values():
            assert 0.0 <= entry.adjusted_p_value <= 1.0
            assert entry.adjusted_p_value >= entry.raw_p_value - 1e-12


@settings(max_examples=200, deadline=None)
@given(st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=2, max_size=30))
def test_adjusted_p_values_are_monotone_in_raw(p_values):
    values = {f"h{i}": p for i, p in enumerate(p_values)}
    entries = sorted(
        correct(values, alpha=ALPHA).corrected.values(), key=lambda e: e.raw_p_value
    )
    for lo, hi in zip(entries, entries[1:]):
        assert lo.adjusted_p_value <= hi.adjusted_p_value + 1e-12


# -- dependence ------------------------------------------------------------


def test_by_is_more_conservative_than_bh():
    """Benjamini-Yekutieli trades power for validity under arbitrary dependence."""
    values = {f"h{i}": p for i, p in enumerate([0.001, 0.01, 0.02, 0.03, 0.04, 0.06, 0.08])}

    bh = correct(values, alpha=ALPHA, method=FDRMethod.BENJAMINI_HOCHBERG)
    by = correct(values, alpha=ALPHA, method=FDRMethod.BENJAMINI_YEKUTIELI)

    assert by.n_significant <= bh.n_significant
    for key in values:
        assert by.corrected[key].adjusted_p_value >= bh.corrected[key].adjusted_p_value


def test_each_method_states_its_dependence_assumption():
    for method in FDRMethod:
        result = correct({"a": 0.01}, alpha=ALPHA, method=method)
        assert result.dependence_assumption
    assert "PRDS" in FDRMethod.BENJAMINI_HOCHBERG.dependence_assumption
    assert "Arbitrary" in FDRMethod.BENJAMINI_YEKUTIELI.dependence_assumption


# -- the correction must be able to change a decision ----------------------


def test_correction_can_overturn_raw_significance():
    """Raw significant but FDR non-significant: the case that makes it matter.

    One marginal hypothesis among many nulls. Uncorrected it clears alpha; after
    correction it does not, because the chance of seeing one p-value that small
    among thirty tests is unremarkable.
    """
    values = {"marginal": 0.09, **{f"null{i}": 0.9 for i in range(29)}}
    result = correct(values, alpha=ALPHA)

    assert result.corrected["marginal"].raw_p_value <= ALPHA
    assert not result.is_significant("marginal")
    assert result.changed_by_correction == ["marginal"]


def test_uniformly_marginal_hypotheses_are_all_rejected_together():
    """The complement, and a guard against over-asserting what BH does.

    With every p-value at 0.09 and m=20, the step-up condition holds at i=m
    (0.09 <= (20/20) * 0.1), so BH rejects all twenty. That is correct: if every
    test is marginal, the expected false-discovery proportion is still bounded.
    """
    values = {f"h{i}": 0.09 for i in range(20)}
    result = correct(values, alpha=ALPHA)

    assert result.n_significant == 20
    assert result.changed_by_correction == []


def test_decisions_come_from_adjusted_p_values_only():
    values = {"strong": 0.0001, "weak": 0.09}
    result = correct({**values, **{f"noise{i}": 0.5 for i in range(30)}}, alpha=ALPHA)

    assert result.is_significant("strong")
    assert not result.is_significant("weak"), "raw 0.09 <= alpha but must fail after correction"
    assert result.corrected["weak"].raw_p_value <= ALPHA
