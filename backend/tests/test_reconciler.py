"""Multi-grain reconciliation, freshness, and data-quality measurement."""

from __future__ import annotations

from datetime import date

import pytest


def test_period_totals_match_ground_truth(periods):
    prior, current = periods

    assert prior.revenue == pytest.approx(1_000_000.00, abs=0.01)
    assert prior.cogs == pytest.approx(640_000.00, abs=0.01)
    assert prior.freight == pytest.approx(60_000.00, abs=0.01)
    assert prior.gross_margin_pct == pytest.approx(30.00, abs=1e-9)

    assert current.revenue == pytest.approx(1_550_000.00, abs=0.01)
    assert current.cogs == pytest.approx(1_016_000.00, abs=0.01)
    assert current.freight == pytest.approx(117_050.00, abs=0.01)
    assert current.gross_margin_pct == pytest.approx(26.90, abs=1e-9)


def test_realised_price_is_revenue_over_units_not_a_naive_mean(periods):
    """An unweighted mean of a price column misstates blended price whenever
    order sizes differ. Realised price is the only defensible input to PVM."""
    _, current = periods
    for facts in current.by_product.values():
        assert facts.avg_price == pytest.approx(facts.revenue / facts.units, abs=1e-9)


def test_weekly_spend_is_split_across_the_month_boundary(reconciler):
    """The week starting 30 October spends into both months.

    Assigning it whole to October would understate November's promotional
    spend by two sevenths of a $15,000 week.
    """
    _, _, allocations = reconciler.allocate_marketing_to_month("2023-11")
    split = [a for a in allocations if a.days_in_month != 7]

    assert split, "expected at least one week straddling the month boundary"
    boundary = next(a for a in split if a.week_start == "2023-10-30")
    assert boundary.days_in_month == 5
    assert boundary.allocated_spend == pytest.approx(15_000.0 * 5 / 7, abs=0.01)


def test_allocated_spend_across_both_months_is_conserved(reconciler):
    """No spend may be created or destroyed by the allocation."""
    oct_spend, _, _ = reconciler.allocate_marketing_to_month("2023-10")
    nov_spend, _, _ = reconciler.allocate_marketing_to_month("2023-11")

    rows = reconciler.conn.execute(
        """
        SELECT sum(spend) FROM marketing_spend
        WHERE CAST(week_start AS DATE) >= DATE '2023-10-02'
          AND CAST(week_start AS DATE) <  DATE '2023-11-27'
        """
    ).fetchone()
    fully_contained = float(rows[0])

    # Everything from the first week through the last fully-in-November week is
    # accounted for across the two months, give or take the tail week that
    # spills into December.
    assert oct_spend + nov_spend >= fully_contained - 0.01


def test_freshness_reflects_each_source_cadence(reconciler):
    by_name = {f.source: f for f in reconciler.freshness()}

    assert not by_name["pos_orders"].is_stale, "the daily feed should be current"
    assert by_name["marketing_spend"].is_stale, "the weekly connector legitimately lags"
    assert by_name["pos_orders"].age_hours < by_name["marketing_spend"].age_hours


def test_freshness_is_deterministic(reconciler):
    """Freshness must not drift with wall-clock time or the demo changes daily."""
    first = {f.source: f.age_hours for f in reconciler.freshness()}
    second = {f.source: f.age_hours for f in reconciler.freshness()}
    assert first == second


def test_attribution_gap_is_measured_and_localised(reconciler):
    """The abstention scenario must be driven by the data, not a hardcoded string."""
    overall, _, _ = reconciler.utm_completeness("2023-11")
    category, rate, tagged, total = reconciler.worst_attribution_segment("2023-11")

    # The aggregate is degraded but survivable while one segment is materially
    # worse -- which is exactly why localising the gap matters. Reporting only
    # the monthly number would average the worst segment away.
    assert 0.5 < overall < 1.0
    assert category == "Bulky"
    assert rate < overall, "the worst segment must be worse than the aggregate"
    assert tagged < total


def test_outage_window_is_far_worse_than_the_month_containing_it(reconciler):
    """A week-long webhook failure must not be washed out by a monthly average."""
    monthly, _, _ = reconciler.utm_completeness("2023-11")
    outage, tagged, total = reconciler.utm_completeness_window(
        date(2023, 11, 1), date(2023, 11, 8)
    )

    assert outage < 0.25, "the outage week should read as near-total tag loss"
    assert outage < monthly / 2
    assert total > 0 and tagged < total


def test_october_has_complete_attribution(reconciler):
    rate, _, _ = reconciler.utm_completeness("2023-10")
    assert rate == pytest.approx(1.0, abs=1e-9)


def test_missing_source_file_fails_with_a_useful_message(tmp_path, store):
    from app.engines.reconciler import DataReconciler

    with pytest.raises(FileNotFoundError, match="app.data.seed"):
        DataReconciler(store=store, data_dir=tmp_path)
