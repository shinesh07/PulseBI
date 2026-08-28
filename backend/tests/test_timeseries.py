"""Phase 4: the time series must be robust to real-world messiness."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.timeseries import DuplicatePolicy, Observation, TimeSeries

D = date(2023, 11, 1)


def day(offset: int) -> date:
    # timedelta rather than arithmetic on the day field, so offsets may cross
    # month boundaries -- the exact class of bug this module exists to prevent.
    return date(2023, 11, 1) + timedelta(days=offset)


# -- normalisation ---------------------------------------------------------


def test_unsorted_input_is_sorted():
    series = TimeSeries.from_pairs([(day(3), 3.0), (day(1), 1.0), (day(2), 2.0)])
    assert series.days() == [day(1), day(2), day(3)]
    assert series.values() == [1.0, 2.0, 3.0]


def test_duplicates_are_summed_by_default():
    """Two batches of orders on one day are one day's worth of orders."""
    series = TimeSeries.from_pairs([(day(0), 10.0), (day(0), 5.0)])
    assert series.n == 1
    assert series.values() == [15.0]


def test_duplicate_policy_last_wins_for_snapshots():
    series = TimeSeries(
        [Observation(day(0), 10.0), Observation(day(0), 5.0)],
        duplicate_policy=DuplicatePolicy.LAST,
    )
    assert series.values() == [5.0]


def test_duplicate_policy_error_refuses_to_guess():
    with pytest.raises(ValueError, match="Duplicate"):
        TimeSeries(
            [Observation(day(0), 10.0), Observation(day(0), 5.0)],
            duplicate_policy=DuplicatePolicy.ERROR,
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_values_are_rejected_at_construction(bad):
    """The single choke point that keeps NaN and Infinity out of the pipeline."""
    with pytest.raises(ValueError, match="Non-finite"):
        TimeSeries([Observation(day(0), bad)])


def test_null_values_are_dropped_by_the_row_adapter():
    from app.timeseries import series_from_rows

    series = series_from_rows([(day(0), 1.0), (day(1), None), (day(2), 3.0)])
    assert series.n == 2


# -- windowing -------------------------------------------------------------


def test_window_is_half_open():
    series = TimeSeries.from_pairs([(day(i), float(i)) for i in range(5)])
    windowed = series.window(day(1), day(3))
    assert windowed.days() == [day(1), day(2)], "end must be exclusive"


def test_window_excludes_everything_outside_it():
    series = TimeSeries.from_pairs([(day(i), float(i)) for i in range(10)])
    assert series.window(day(2), day(4)).values() == [2.0, 3.0]


def test_empty_window_yields_empty_series():
    series = TimeSeries.from_pairs([(day(i), float(i)) for i in range(5)])
    assert series.window(day(2), day(2)).is_empty


def test_reversed_window_is_rejected():
    series = TimeSeries.from_pairs([(day(0), 1.0)])
    with pytest.raises(ValueError, match="precedes"):
        series.window(day(5), day(1))


def test_one_day_window():
    series = TimeSeries.from_pairs([(day(i), float(i)) for i in range(5)])
    single = series.window(day(2), day(3))
    assert single.n == 1 and single.values() == [2.0]


def test_before_selects_only_history():
    series = TimeSeries.from_pairs([(day(i), float(i)) for i in range(5)])
    assert series.before(day(2)).days() == [day(0), day(1)]


# -- coverage --------------------------------------------------------------


def test_missing_days_are_visible_rather_than_silently_shortening():
    """The bug this class exists to fix: a gap used to just make the list shorter."""
    series = TimeSeries.from_pairs([(day(0), 1.0), (day(3), 4.0)])
    missing = series.missing_days(day(0), day(4))
    assert missing == [day(1), day(2)]
    assert series.coverage(day(0), day(4)) == pytest.approx(0.5)


def test_internal_gaps_are_detected():
    assert TimeSeries.from_pairs([(day(0), 1.0), (day(5), 1.0)]).has_internal_gaps()
    assert not TimeSeries.from_pairs([(day(0), 1.0), (day(1), 1.0)]).has_internal_gaps()


def test_sparse_series_reports_low_coverage():
    series = TimeSeries.from_pairs([(day(0), 1.0), (day(9), 1.0)])
    assert series.coverage(day(0), day(10)) == pytest.approx(0.2)


def test_densify_is_explicit_never_automatic():
    """A gap may mean 'no sales' or 'the feed broke'; only the caller knows."""
    series = TimeSeries.from_pairs([(day(0), 1.0), (day(2), 3.0)])
    assert series.n == 2
    filled = series.densify(day(0), day(3), fill=0.0)
    assert filled.values() == [1.0, 0.0, 3.0]


def test_empty_series_has_no_coverage():
    assert TimeSeries.empty().coverage(day(0), day(5)) == 0.0


# -- properties ------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=60),
            st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
        ),
        max_size=40,
    )
)
def test_series_is_always_sorted_and_unique(pairs):
    series = TimeSeries.from_pairs([(day(i), v) for i, v in pairs])
    days = series.days()
    assert days == sorted(days)
    assert len(days) == len(set(days))


@settings(max_examples=100, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=30),
            st.floats(min_value=0, max_value=1e5, allow_nan=False, allow_infinity=False),
        ),
        min_size=1,
        max_size=30,
    ),
    st.integers(min_value=0, max_value=15),
    st.integers(min_value=16, max_value=31),
)
def test_windowing_never_returns_observations_outside_the_window(pairs, lo, hi):
    series = TimeSeries.from_pairs([(day(i), v) for i, v in pairs])
    start, end = day(lo), day(hi)
    for obs in series.window(start, end):
        assert start <= obs.day < end
