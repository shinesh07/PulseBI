"""Multi-grain reconciliation over DuckDB.

Three sources arrive at three grains -- daily POS, weekly ad spend, monthly ERP
-- and none of them agree on a calendar. Reconciling them is the part the brief
calls out as "different source-system refresh cadences and grains", and the
awkward case is real: a marketing week that starts 30 October spends into both
October and November, so it has to be split pro-rata rather than assigned whole
to whichever month it happens to start in.

All SQL here is parameterised. Source files are loaded through pandas and
registered as views, so no filesystem path is ever interpolated into a query.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from pydantic import BaseModel

from app.contracts import ContractStore, get_contract_store
from app.dbaccess import ThreadSafeConnection

DEFAULT_DATA_DIR = Path(__file__).parent.parent / "data"


class ProductPeriodFacts(BaseModel):
    """Per-product economics for one period -- the inputs to PVM and the bridge."""

    product_id: str
    product_name: str
    category: str
    units: float
    revenue: float
    avg_price: float          # revenue / units, i.e. realised price, not a naive mean
    cogs_per_unit: float
    freight_per_unit: float

    @property
    def cogs(self) -> float:
        return self.units * self.cogs_per_unit

    @property
    def freight(self) -> float:
        return self.units * self.freight_per_unit


class PeriodSummary(BaseModel):
    period: str
    revenue: float
    units: float
    cogs: float
    freight: float
    gross_margin_pct: float
    marketing_spend: float
    new_customers: float
    by_product: dict[str, ProductPeriodFacts]

    @property
    def blended_cac(self) -> float:
        return self.marketing_spend / self.new_customers if self.new_customers else 0.0


@dataclass(frozen=True)
class SourceFreshness:
    source: str
    system: str
    grain: str
    latest_record: str
    age_hours: float
    sla_hours: int

    @property
    def is_stale(self) -> bool:
        return self.age_hours > self.sla_hours

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "system": self.system,
            "grain": self.grain,
            "latest_record": self.latest_record,
            "age_hours": round(self.age_hours, 1),
            "sla_hours": self.sla_hours,
            "is_stale": self.is_stale,
        }


@dataclass(frozen=True)
class WeekAllocation:
    """One marketing week's contribution to one month."""

    week_start: str
    days_in_month: int
    total_days: int
    allocated_spend: float
    allocated_new_customers: float


class DataReconciler:
    def __init__(
        self,
        store: ContractStore | None = None,
        data_dir: Path | str | None = None,
    ) -> None:
        self.store = store or get_contract_store()
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
        # Thread-safe: the API serves sync endpoints from a pool and the dashboard
        # issues several fetches at once. A bare connection interleaves parameter
        # bindings across threads.
        self.conn = ThreadSafeConnection(":memory:")
        self._frames: dict[str, pd.DataFrame] = {}
        self._load_sources()

    # -- loading -----------------------------------------------------------

    def _load_sources(self) -> None:
        for name, source in self.store.contract.sources.items():
            path = self.data_dir / source.file
            if not path.exists():
                raise FileNotFoundError(
                    f"Source '{name}' expects {path}. Run `python -m app.data.seed` first."
                )
            frame = pd.read_csv(path)
            self._frames[name] = frame
            # register() binds the DataFrame by name -- no path interpolation.
            self.conn.register(f"_raw_{name}", frame)
            # Materialise on the root connection: a registered frame is visible
            # only there, while the resulting table is visible to every thread.
            self.conn.bootstrap_execute(
                f'CREATE TABLE "{name}" AS SELECT * FROM "_raw_{name}"'
            )

    # -- freshness ---------------------------------------------------------

    def analysis_as_of(self) -> datetime:
        """Fixed reference point: one hour after the newest POS record.

        Deterministic on purpose -- freshness must not drift with wall-clock
        time, or the demo tells a different story every day. Anchoring just
        past the daily feed means POS reads fresh while the weekly ad connector
        legitimately breaches its SLA, which is the real-world picture: the
        fastest source is current and the slower ones lag behind it.
        """
        latest = self._frames["pos_orders"]["date"].max()
        return datetime.fromisoformat(str(latest)) + timedelta(hours=1)

    def freshness(self) -> list[SourceFreshness]:
        now = self.analysis_as_of()
        out: list[SourceFreshness] = []
        for name, source in self.store.contract.sources.items():
            latest_raw = str(self._frames[name][source.date_column].max())
            latest = datetime.fromisoformat(latest_raw)
            out.append(
                SourceFreshness(
                    source=name,
                    system=source.system,
                    grain=source.grain,
                    latest_record=latest_raw,
                    age_hours=(now - latest).total_seconds() / 3600.0,
                    sla_hours=source.freshness_sla_hours,
                )
            )
        return out

    # -- grain alignment ---------------------------------------------------

    @staticmethod
    def _month_bounds(period: str) -> tuple[date, date]:
        year, month = (int(p) for p in period.split("-"))
        start = date(year, month, 1)
        end = date(year + (month == 12), (month % 12) + 1, 1)
        return start, end

    def allocate_marketing_to_month(self, period: str) -> tuple[float, float, list[WeekAllocation]]:
        """Split weekly ad spend across month boundaries, pro-rata by day.

        A week is seven days of spend. If three of those days fall in the target
        month, three sevenths of the spend belongs to it. Assigning the whole
        week to its starting month would misstate November's CAC by roughly a
        week of the promotional push.
        """
        start, end = self._month_bounds(period)
        rows = self.conn.execute(
            'SELECT week_start, spend, new_customers FROM marketing_spend ORDER BY week_start'
        ).fetchall()

        allocations: list[WeekAllocation] = []
        total_spend = 0.0
        total_customers = 0.0

        for week_start_raw, spend, new_customers in rows:
            week_start = date.fromisoformat(str(week_start_raw))
            week_end = week_start + timedelta(days=7)

            overlap_start = max(week_start, start)
            overlap_end = min(week_end, end)
            overlap_days = (overlap_end - overlap_start).days
            if overlap_days <= 0:
                continue

            fraction = overlap_days / 7.0
            alloc_spend = float(spend) * fraction
            alloc_customers = float(new_customers) * fraction

            total_spend += alloc_spend
            total_customers += alloc_customers
            allocations.append(
                WeekAllocation(
                    week_start=week_start.isoformat(),
                    days_in_month=overlap_days,
                    total_days=7,
                    allocated_spend=alloc_spend,
                    allocated_new_customers=alloc_customers,
                )
            )

        return total_spend, total_customers, allocations

    # -- period rollup -----------------------------------------------------

    def period_summary(self, period: str) -> PeriodSummary:
        """Roll daily POS and monthly ERP up to a single reconciled month."""
        start, end = self._month_bounds(period)

        rows = self.conn.execute(
            """
            SELECT
                p.product_id,
                any_value(p.product_name)      AS product_name,
                any_value(p.category)          AS category,
                sum(p.units)                   AS units,
                sum(p.revenue)                 AS revenue,
                any_value(e.cogs_per_unit)     AS cogs_per_unit,
                any_value(e.freight_per_unit)  AS freight_per_unit
            FROM pos_orders p
            LEFT JOIN erp_financials e
                   ON e.product_id = p.product_id
                  AND CAST(e.month AS DATE) >= ?
                  AND CAST(e.month AS DATE) <  ?
            WHERE CAST(p.date AS DATE) >= ?
              AND CAST(p.date AS DATE) <  ?
            GROUP BY p.product_id
            ORDER BY p.product_id
            """,
            [start, end, start, end],
        ).fetchall()

        by_product: dict[str, ProductPeriodFacts] = {}
        for pid, name, category, units, revenue, cogs_pu, freight_pu in rows:
            units = float(units or 0.0)
            revenue = float(revenue or 0.0)
            by_product[pid] = ProductPeriodFacts(
                product_id=pid,
                product_name=name,
                category=category,
                units=units,
                revenue=revenue,
                avg_price=revenue / units if units else 0.0,
                cogs_per_unit=float(cogs_pu or 0.0),
                freight_per_unit=float(freight_pu or 0.0),
            )

        revenue = sum(p.revenue for p in by_product.values())
        units = sum(p.units for p in by_product.values())
        cogs = sum(p.cogs for p in by_product.values())
        freight = sum(p.freight for p in by_product.values())
        margin = (revenue - cogs - freight) / revenue * 100.0 if revenue else 0.0

        spend, customers, _ = self.allocate_marketing_to_month(period)

        return PeriodSummary(
            period=period,
            revenue=revenue,
            units=units,
            cogs=cogs,
            freight=freight,
            gross_margin_pct=margin,
            marketing_spend=spend,
            new_customers=customers,
            by_product=by_product,
        )

    # -- series for detection and cold start -------------------------------

    def daily_revenue_series(self, period: str, product_id: str | None = None) -> list[float]:
        start, end = self._month_bounds(period)
        if product_id:
            sql = """
                SELECT sum(revenue) FROM pos_orders
                WHERE CAST(date AS DATE) >= ? AND CAST(date AS DATE) < ? AND product_id = ?
                GROUP BY date ORDER BY date
            """
            params: list = [start, end, product_id]
        else:
            sql = """
                SELECT sum(revenue) FROM pos_orders
                WHERE CAST(date AS DATE) >= ? AND CAST(date AS DATE) < ?
                GROUP BY date ORDER BY date
            """
            params = [start, end]
        return [float(r[0]) for r in self.conn.execute(sql, params).fetchall()]

    def daily_units_series(self, product_id: str) -> list[float]:
        rows = self.conn.execute(
            """
            SELECT sum(units) FROM pos_orders
            WHERE product_id = ?
            GROUP BY date ORDER BY date
            """,
            [product_id],
        ).fetchall()
        return [float(r[0]) for r in rows]

    def category_daily_units(self, exclude_product_id: str | None = None) -> list[float]:
        """Daily units for mature SKUs -- the prior pool for cold-start shrinkage."""
        if exclude_product_id:
            rows = self.conn.execute(
                """
                SELECT sum(units) FROM pos_orders
                WHERE product_id <> ?
                GROUP BY date, product_id ORDER BY date
                """,
                [exclude_product_id],
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT sum(units) FROM pos_orders GROUP BY date, product_id ORDER BY date"
            ).fetchall()
        return [float(r[0]) for r in rows]

    def utm_completeness_window(
        self, start: date, end: date, category: str | None = None
    ) -> tuple[float, int, int]:
        """Attribution coverage over an arbitrary date window.

        Scoping matters: a feed can look survivable across a whole month while
        one week inside it is unusable. Reporting only the monthly figure would
        average an outage away.
        """
        params: list = [start, end]
        clause = ""
        if category is not None:
            clause = " AND category = ?"
            params.append(category)

        total, tagged = self.conn.execute(
            f"""
            SELECT count(*), count(utm_source)
            FROM pos_orders
            WHERE CAST(date AS DATE) >= ? AND CAST(date AS DATE) < ?{clause}
            """,
            params,
        ).fetchone()
        total = int(total or 0)
        tagged = int(tagged or 0)
        return (tagged / total if total else 1.0), tagged, total

    def utm_completeness(
        self, period: str, category: str | None = None
    ) -> tuple[float, int, int]:
        """Share of orders in the period carrying attribution tags.

        Measured from the rows, not asserted. Accepts a category filter because
        attribution breaks unevenly: a feed can look acceptable in aggregate
        while being badly degraded in exactly the segment that moved. Localising
        the gap is what lets the engine abstain on one segment instead of
        discarding the whole analysis.
        """
        start, end = self._month_bounds(period)
        return self.utm_completeness_window(start, end, category)

    def daily_revenue_by_category(self, period: str, category: str) -> list[float]:
        start, end = self._month_bounds(period)
        rows = self.conn.execute(
            """
            SELECT sum(revenue) FROM pos_orders
            WHERE CAST(date AS DATE) >= ? AND CAST(date AS DATE) < ? AND category = ?
            GROUP BY date ORDER BY date
            """,
            [start, end, category],
        ).fetchall()
        return [float(r[0]) for r in rows]

    def categories(self) -> list[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT category FROM pos_orders ORDER BY category"
            ).fetchall()
        ]

    def products(self) -> list[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT product_id FROM pos_orders ORDER BY product_id"
            ).fetchall()
        ]

    def daily_cost_series(
        self, period: str, kind: str, product_id: str | None = None
    ) -> list[float]:
        """Daily COGS or freight: daily units valued at the month's ERP unit rate.

        The ERP publishes monthly, so the unit rate is constant within a month
        and the daily shape comes entirely from POS volume. That is exactly the
        grain reconciliation the brief asks for, and it gives the cost KPIs a
        real series to test rather than excluding them from detection.
        """
        column = {"cogs": "cogs_per_unit", "freight": "freight_per_unit"}[kind]
        start, end = self._month_bounds(period)
        params: list = [start, end, start, end]
        clause = ""
        if product_id is not None:
            clause = " AND p.product_id = ?"
            params.append(product_id)

        rows = self.conn.execute(
            f"""
            SELECT p.date, sum(p.units * e.{column})
            FROM pos_orders p
            JOIN erp_financials e
              ON e.product_id = p.product_id
             AND CAST(e.month AS DATE) >= ? AND CAST(e.month AS DATE) < ?
            WHERE CAST(p.date AS DATE) >= ? AND CAST(p.date AS DATE) < ?{clause}
            GROUP BY p.date ORDER BY p.date
            """,
            params,
        ).fetchall()
        return [float(r[1]) for r in rows]

    def daily_margin_series(self, period: str) -> list[float]:
        """Daily gross margin percentage, reconciling daily POS against monthly ERP."""
        start, end = self._month_bounds(period)
        rows = self.conn.execute(
            """
            SELECT p.date,
                   sum(p.revenue) AS revenue,
                   sum(p.units * (e.cogs_per_unit + e.freight_per_unit)) AS cost
            FROM pos_orders p
            JOIN erp_financials e
              ON e.product_id = p.product_id
             AND CAST(e.month AS DATE) >= ? AND CAST(e.month AS DATE) < ?
            WHERE CAST(p.date AS DATE) >= ? AND CAST(p.date AS DATE) < ?
            GROUP BY p.date ORDER BY p.date
            """,
            [start, end, start, end],
        ).fetchall()
        return [
            (float(rev) - float(cost)) / float(rev) * 100.0
            for _, rev, cost in rows
            if rev and float(rev) > 0
        ]

    def worst_attribution_segment(self, period: str) -> tuple[str, float, int, int]:
        """The category with the weakest attribution coverage in the period."""
        categories = [
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT category FROM pos_orders ORDER BY category"
            ).fetchall()
        ]
        worst = ("", 1.0, 0, 0)
        for category in categories:
            rate, tagged, total = self.utm_completeness(period, category)
            if total and rate < worst[1]:
                worst = (category, rate, tagged, total)
        return worst

    def close(self) -> None:
        self.conn.close()
