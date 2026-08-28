"""Phase 3: event-window conclusions must not leak data from outside the window.

This is the regression test for the audit's most serious finding. Previously
`ConfidenceEngine.assess(window=...)` scoped two of its three components but not
the third: the contradiction report had no window parameter and always measured
the full period. A one-day analysis reported a consistency figure computed over
thirty days, byte-identical to the monthly number.

The test the brief specifies:

    1. compute a result for an event window
    2. add unrelated observations outside that window
    3. recompute
    4. the event-period result must be unchanged, except where historical data
       is legitimately required for baseline estimation
"""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pandas as pd
import pytest

from app.contracts import get_contract_store
from app.engines.detector import BaselineMode
from app.pipeline import DetectionPipeline
from app.series import SeriesKey

BASELINE = (date(2023, 10, 1), date(2023, 11, 1))
EVENT = (date(2023, 11, 1), date(2023, 11, 8))
REVENUE = SeriesKey("revenue", "total", "ALL")


class _FakeReconciler:
    """A reconciler over an injectable frame, so rows can be added mid-test."""

    def __init__(self, pos: pd.DataFrame, erp: pd.DataFrame) -> None:
        self.conn = duckdb.connect(":memory:")
        self.conn.register("_p", pos)
        self.conn.register("_e", erp)
        self.conn.execute("CREATE TABLE pos_orders AS SELECT * FROM _p")
        self.conn.execute("CREATE TABLE erp_financials AS SELECT * FROM _e")
        self.conn.execute(
            "CREATE TABLE marketing_spend(week_start VARCHAR, channel VARCHAR, "
            "spend DOUBLE, impressions BIGINT, clicks BIGINT, new_customers BIGINT)"
        )

    def freshness(self):
        return []

    def close(self) -> None:
        self.conn.close()


def _orders(start: date, days: int, units: float, price: float, product: str = "P1"):
    return [
        {
            "order_id": f"{product}-{start}-{i}",
            "date": (start + timedelta(days=i)).isoformat(),
            "product_id": product,
            "product_name": product,
            "category": "C1",
            "units": units,
            "price_per_unit": price,
            "revenue": units * price,
            "utm_source": "Meta",
            "utm_medium": "cpc",
        }
        for i in range(days)
    ]


def _erp(product: str = "P1"):
    return [
        {
            "month": month,
            "product_id": product,
            "cogs_per_unit": 5.0,
            "freight_per_unit": 1.0,
        }
        for month in ("2023-10-01", "2023-11-01", "2023-12-01")
    ]


def _build(extra_rows=None) -> DetectionPipeline:
    rows = _orders(date(2023, 10, 1), 31, 10.0, 10.0) + _orders(
        date(2023, 11, 1), 7, 25.0, 10.0
    )
    if extra_rows:
        rows = rows + extra_rows
    pipeline = DetectionPipeline(
        reconciler=_FakeReconciler(pd.DataFrame(rows), pd.DataFrame(_erp())),
        store=get_contract_store(),
    )
    return pipeline


def _revenue_package(pipeline: DetectionPipeline):
    result = pipeline.analyse(BASELINE, EVENT, keys=[REVENUE])
    return next(p for p in result.packages if p.kpi == "revenue")


# -- the specified regression test -----------------------------------------


def test_data_after_the_event_window_does_not_change_the_result():
    """Rows in December must not touch a conclusion about 1-7 November."""
    before = _revenue_package(_build())
    after = _revenue_package(
        _build(extra_rows=_orders(date(2023, 12, 1), 31, 999.0, 50.0))
    )

    assert after.current_value == pytest.approx(before.current_value)
    assert after.observed_change.absolute_change == pytest.approx(
        before.observed_change.absolute_change
    )
    assert after.statistical_test.p_value == pytest.approx(before.statistical_test.p_value)
    assert after.decision is before.decision


def test_data_inside_the_event_window_does_change_the_result():
    """The complement: the test must be capable of detecting a real difference."""
    before = _revenue_package(_build())
    after = _revenue_package(
        _build(extra_rows=_orders(date(2023, 11, 3), 2, 500.0, 10.0, product="P2"))
    )
    assert after.current_value > before.current_value


def test_a_different_event_window_gives_a_different_answer():
    """Guards against a window parameter being accepted and then ignored."""
    pipeline = _build(extra_rows=_orders(date(2023, 11, 8), 22, 5.0, 10.0))

    week_one = pipeline.analyse(BASELINE, EVENT, keys=[REVENUE]).packages[0]
    full_month = pipeline.analyse(
        BASELINE, (date(2023, 11, 1), date(2023, 12, 1)), keys=[REVENUE]
    ).packages[0]

    assert week_one.current_value != pytest.approx(full_month.current_value)
    assert week_one.event_window.days == 7
    assert full_month.event_window.days == 30


def test_baseline_data_legitimately_reaches_the_result():
    """Historical data is *supposed* to inform the baseline -- only the baseline."""
    quiet = _revenue_package(_build())
    busy_history = _build()
    busy_history.reconciler.conn.execute(
        """
        INSERT INTO pos_orders
        SELECT 'extra' || CAST(i AS VARCHAR), '2023-10-15', 'P1', 'P1', 'C1',
               100.0, 10.0, 1000.0, 'Meta', 'cpc'
        FROM range(5) t(i)
        """
    )
    louder = _revenue_package(busy_history)

    assert louder.baseline_value > quiet.baseline_value
    assert louder.current_value == pytest.approx(quiet.current_value)


# -- window shapes ---------------------------------------------------------


def test_one_day_event_window():
    pipeline = _build()
    result = pipeline.analyse(BASELINE, (date(2023, 11, 1), date(2023, 11, 2)), keys=[REVENUE])
    package = result.packages[0]

    assert package.event_window.days == 1
    assert package.statistical_test.event_n <= 1
    assert not package.statistical_test.tested, "one observation cannot support a test"


def test_empty_event_window_is_rejected():
    pipeline = _build()
    with pytest.raises(ValueError, match="non-empty"):
        pipeline.analyse(BASELINE, (date(2023, 11, 1), date(2023, 11, 1)), keys=[REVENUE])


def test_event_window_with_no_observations_abstains():
    pipeline = _build()
    result = pipeline.analyse(
        BASELINE, (date(2024, 6, 1), date(2024, 6, 30)), keys=[REVENUE]
    )
    package = result.packages[0]
    assert not package.statistical_test.tested
    assert package.decision.value in {"ABSTAIN", "NOT_MATERIAL"}


def test_sparse_window_reports_low_coverage():
    rows = _orders(date(2023, 10, 1), 31, 10.0, 10.0) + [
        _orders(date(2023, 11, 1), 1, 25.0, 10.0)[0],
        _orders(date(2023, 11, 6), 1, 25.0, 10.0)[0],
    ]
    pipeline = DetectionPipeline(
        reconciler=_FakeReconciler(pd.DataFrame(rows), pd.DataFrame(_erp())),
        store=get_contract_store(),
    )
    package = pipeline.analyse(BASELINE, EVENT, keys=[REVENUE]).packages[0]

    assert package.data_quality.event_window_coverage < 0.5


def test_unequal_windows_are_normalised_and_flagged():
    """A 7-day event against a 31-day baseline must not read as a collapse."""
    pipeline = _build()
    matched = pipeline.analyse(BASELINE, EVENT, keys=[REVENUE]).packages[0]
    as_reported = pipeline.analyse(
        BASELINE, EVENT, keys=[REVENUE], baseline_mode=BaselineMode.AS_REPORTED
    ).packages[0]

    assert not matched.windows_equal_length
    assert matched.baseline_scale == pytest.approx(7 / 31)
    assert as_reported.baseline_scale == 1.0
    # Raw totals make a shorter window look like a catastrophic decline.
    assert as_reported.observed_change.absolute_change < 0
    assert matched.observed_change.absolute_change > 0
