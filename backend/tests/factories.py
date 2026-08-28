"""Builders for synthetic PeriodSummary objects used by the property tests.

The point of these tests is to prove the decompositions close on *arbitrary*
inputs, not just on the seeded demo numbers. A suite that only asserts the
rehearsed figures proves the demo reproduces itself, which is not the same as
proving the engines are correct.
"""

from __future__ import annotations

from app.engines.reconciler import PeriodSummary, ProductPeriodFacts


def make_product(
    pid: str,
    *,
    units: float,
    price: float,
    cogs: float,
    freight: float,
    category: str = "Test",
) -> ProductPeriodFacts:
    return ProductPeriodFacts(
        product_id=pid,
        product_name=pid,
        category=category,
        units=units,
        revenue=units * price,
        avg_price=price,
        cogs_per_unit=cogs,
        freight_per_unit=freight,
    )


def make_period(period: str, products: dict[str, ProductPeriodFacts]) -> PeriodSummary:
    revenue = sum(p.revenue for p in products.values())
    cogs = sum(p.cogs for p in products.values())
    freight = sum(p.freight for p in products.values())
    return PeriodSummary(
        period=period,
        revenue=revenue,
        units=sum(p.units for p in products.values()),
        cogs=cogs,
        freight=freight,
        gross_margin_pct=(revenue - cogs - freight) / revenue * 100.0 if revenue else 0.0,
        marketing_spend=0.0,
        new_customers=0.0,
        by_product=products,
    )


def scale_quantities(period: PeriodSummary, factor: float) -> PeriodSummary:
    """Multiply every quantity by a constant, leaving unit economics untouched."""
    scaled = {
        pid: make_product(
            pid,
            units=p.units * factor,
            price=p.avg_price,
            cogs=p.cogs_per_unit,
            freight=p.freight_per_unit,
            category=p.category,
        )
        for pid, p in period.by_product.items()
    }
    return make_period(period.period, scaled)
