"""Window-scoped access to KPI time series.

The detector must not know that revenue lives in `pos_orders.revenue` while
freight is `units * erp.freight_per_unit`. It asks a provider for the series
behind a key and receives a normalised TimeSeries or nothing.

That indirection is what keeps the detection algorithm free of KPI names: adding
a metric means declaring it in the contract and registering a query here, with
no change to the detector, the FDR pool, or the confidence model.

Every accessor takes an explicit half-open [start, end) window. There is no
"give me the whole period" call, because that is precisely the shape that let a
seven-day analysis silently read thirty days of data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.contracts import ContractStore, Relationship
from app.timeseries import TimeSeries, series_from_rows


@dataclass(frozen=True, order=True)
class SeriesKey:
    """Identifies one testable series: a KPI sliced by one entity."""

    kpi: str
    dimension: str  # "total" | "product" | "category"
    entity: str  # "ALL" for an unsliced KPI

    def __str__(self) -> str:
        return f"{self.kpi}/{self.dimension}/{self.entity}"

    @property
    def is_total(self) -> bool:
        return self.dimension == "total"


class SeriesProvider(Protocol):
    def available_keys(self) -> list[SeriesKey]: ...

    def daily_series(self, key: SeriesKey, start: date, end: date) -> TimeSeries | None: ...

    def aggregate(self, key: SeriesKey, start: date, end: date) -> float | None: ...


class DuckDBSeriesProvider:
    """Serves KPI series from the reconciler's in-memory DuckDB tables.

    Which KPIs can be sliced, and along which dimensions, is data-driven: the
    provider intersects the KPIs declared in the contract with the query
    templates it knows how to run.
    """

    # Additive KPIs expressed as a per-row daily quantity. A KPI absent here has
    # no daily representation and will be reported on materiality alone.
    _DAILY_SQL: dict[str, str] = {
        "revenue": "sum(p.revenue)",
        "cogs": "sum(p.units * e.cogs_per_unit)",
        "freight": "sum(p.units * e.freight_per_unit)",
    }
    _NEEDS_ERP = {"cogs", "freight"}

    def __init__(self, conn, store: ContractStore) -> None:
        self.conn = conn
        self.store = store

    # -- discovery ---------------------------------------------------------

    def _entities(self, dimension: str) -> list[str]:
        column = {"product": "product_id", "category": "category"}[dimension]
        return [
            r[0]
            for r in self.conn.execute(
                f"SELECT DISTINCT {column} FROM pos_orders ORDER BY {column}"
            ).fetchall()
        ]

    def available_keys(self) -> list[SeriesKey]:
        keys: list[SeriesKey] = []
        for kpi in sorted(self.store.kpi_names):
            keys.append(SeriesKey(kpi, "total", "ALL"))
            if kpi not in self._DAILY_SQL:
                continue
            for dimension in ("product", "category"):
                for entity in self._entities(dimension):
                    keys.append(SeriesKey(kpi, dimension, entity))
        return keys

    # -- series ------------------------------------------------------------

    def _slice_clause(self, key: SeriesKey) -> tuple[str, list]:
        if key.dimension == "product":
            return " AND p.product_id = ?", [key.entity]
        if key.dimension == "category":
            return " AND p.category = ?", [key.entity]
        return "", []

    def daily_series(self, key: SeriesKey, start: date, end: date) -> TimeSeries | None:
        """Daily observations for `key` restricted to [start, end).

        Returns None where the KPI has no daily representation -- a weekly
        metric compared against a monthly window, for instance. None means
        "no test is possible", which is deliberately distinct from an empty
        series, which means "the test is possible and found nothing".
        """
        # Ratio KPIs are built same-day rather than summed, so they take a
        # dedicated path. Only the unsliced total is defined: a per-product
        # margin would need per-product revenue in the denominator, which the
        # contract does not declare.
        if key.kpi == "gross_margin":
            return self.daily_margin_series(start, end) if key.is_total else None

        expression = self._DAILY_SQL.get(key.kpi)
        if expression is None:
            return None

        clause, params = self._slice_clause(key)

        if key.kpi in self._NEEDS_ERP:
            sql = f"""
                SELECT p.date, {expression}
                FROM pos_orders p
                JOIN erp_financials e
                  ON e.product_id = p.product_id
                 AND date_trunc('month', CAST(e.month AS DATE))
                     = date_trunc('month', CAST(p.date AS DATE))
                WHERE CAST(p.date AS DATE) >= ? AND CAST(p.date AS DATE) < ?{clause}
                GROUP BY p.date ORDER BY p.date
            """
        else:
            sql = f"""
                SELECT p.date, {expression}
                FROM pos_orders p
                WHERE CAST(p.date AS DATE) >= ? AND CAST(p.date AS DATE) < ?{clause}
                GROUP BY p.date ORDER BY p.date
            """

        rows = self.conn.execute(sql, [start, end, *params]).fetchall()
        return series_from_rows(rows, name=str(key))

    def daily_margin_series(self, start: date, end: date) -> TimeSeries:
        """Gross margin percentage per day.

        A ratio, so it is built from same-day revenue and cost rather than
        summed. Days with zero revenue are omitted rather than filled: a margin
        on no sales is undefined, not zero.
        """
        rows = self.conn.execute(
            """
            SELECT p.date,
                   sum(p.revenue) AS revenue,
                   sum(p.units * (e.cogs_per_unit + e.freight_per_unit)) AS cost
            FROM pos_orders p
            JOIN erp_financials e
              ON e.product_id = p.product_id
             AND date_trunc('month', CAST(e.month AS DATE))
                 = date_trunc('month', CAST(p.date AS DATE))
            WHERE CAST(p.date AS DATE) >= ? AND CAST(p.date AS DATE) < ?
            GROUP BY p.date ORDER BY p.date
            """,
            [start, end],
        ).fetchall()
        return TimeSeries.from_pairs(
            (
                (day, (float(rev) - float(cost)) / float(rev) * 100.0)
                for day, rev, cost in rows
                if rev is not None and float(rev) > 0
            ),
            name="gross_margin/total/ALL",
        )

    def aggregate(self, key: SeriesKey, start: date, end: date) -> float | None:
        """The KPI's value over the whole window, for materiality assessment.

        Additive KPIs sum their daily series. Ratios and rate metrics are
        computed from window totals rather than averaged, because the mean of a
        daily ratio is not the ratio of the window.
        """
        if key.kpi == "gross_margin" and key.is_total:
            row = self.conn.execute(
                """
                SELECT sum(p.revenue),
                       sum(p.units * (e.cogs_per_unit + e.freight_per_unit))
                FROM pos_orders p
                JOIN erp_financials e
                  ON e.product_id = p.product_id
                 AND date_trunc('month', CAST(e.month AS DATE))
                     = date_trunc('month', CAST(p.date AS DATE))
                WHERE CAST(p.date AS DATE) >= ? AND CAST(p.date AS DATE) < ?
                """,
                [start, end],
            ).fetchone()
            revenue, cost = row
            if not revenue or float(revenue) <= 0:
                return None
            return (float(revenue) - float(cost or 0.0)) / float(revenue) * 100.0

        if key.kpi == "blended_cac" and key.is_total:
            spend, customers = self._marketing_totals(start, end)
            return spend / customers if customers > 0 else None

        series = self.daily_series(key, start, end)
        if series is None:
            return None
        if series.n:
            return series.total()

        # An empty window means different things for different metric shapes, so
        # the contract decides rather than this function guessing. For an
        # additive KPI the sum of no rows is genuinely zero -- a product that
        # sold nothing earned nothing -- and collapsing that to "unknown" is what
        # made a new product's launch read as an unmeasurable event instead of a
        # move from a zero baseline. For a ratio or rate, no rows means the
        # denominator is absent and the value is undefined.
        relationship = self.store.kpi(key.kpi).metric_tree.relationship
        return 0.0 if relationship is Relationship.ADDITIVE else None

    def _marketing_totals(self, start: date, end: date) -> tuple[float, float]:
        """Weekly spend allocated pro-rata into an arbitrary window.

        A marketing week is seven days of spend. Only the portion of it that
        falls inside the window belongs to the window -- assigning whole weeks
        to whichever period they start in misstates any window shorter than a
        week, which is most event windows.
        """
        rows = self.conn.execute(
            "SELECT week_start, spend, new_customers FROM marketing_spend ORDER BY week_start"
        ).fetchall()

        spend_total = 0.0
        customer_total = 0.0
        for week_start_raw, spend, customers in rows:
            week_start = date.fromisoformat(str(week_start_raw))
            week_end = date.fromordinal(week_start.toordinal() + 7)
            overlap = (min(week_end, end) - max(week_start, start)).days
            if overlap <= 0:
                continue
            fraction = overlap / 7.0
            spend_total += float(spend) * fraction
            customer_total += float(customers) * fraction
        return spend_total, customer_total
