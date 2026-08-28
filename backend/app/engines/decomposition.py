"""Price-Volume-Mix decomposition of a revenue movement.

The standard FP&A three-term bridge is

    Price  = SUM (P1_i - P0_i) * Q1_i
    Volume = (Q1_total - Q0_total) * P0_bar
    Mix    = Q1_total * SUM (S1_i - S0_i) * P0_i          where S_i = Q_i / Q_total

which is an exact identity over products present in *both* periods:

    Price + Volume + Mix
      = [Rev1 - SUM Q1_i P0_i] + [-Rev0 + SUM Q1_i P0_i]
      = Rev1 - Rev0

That identity breaks the moment a product enters or leaves the range, which is
why the plain mix-change method is documented as distorting results for
businesses with changing product lines. This module uses the Controller-Akademie
treatment instead: entering and exiting products are reported as their own terms
rather than being folded into a residual labelled "mix".

That matters here concretely -- YOG-01 launches mid-November with no October
history, so a three-term bridge would silently attribute $50,000 of new-product
revenue to "mix and other".
"""

from __future__ import annotations

from pydantic import BaseModel

from app.engines.reconciler import PeriodSummary, ProductPeriodFacts

CLOSURE_TOLERANCE = 0.01


class PVMTerm(BaseModel):
    name: str
    label: str
    value: float
    explanation: str


class PVMResult(BaseModel):
    prior_period: str
    current_period: str
    prior_revenue: float
    current_revenue: float
    total_variance: float
    terms: list[PVMTerm]
    residual: float
    closes: bool
    continuing_products: list[str]
    entering_products: list[str]
    exiting_products: list[str]

    @property
    def terms_by_name(self) -> dict[str, float]:
        return {t.name: t.value for t in self.terms}

    def ranked_terms(self) -> list[PVMTerm]:
        """Largest absolute contribution first -- the driver ranking."""
        return sorted(self.terms, key=lambda t: abs(t.value), reverse=True)


def decompose_revenue(prior: PeriodSummary, current: PeriodSummary) -> PVMResult:
    """Decompose the revenue movement between two reconciled periods.

    Raises ValueError if the terms fail to reconstruct the total variance --
    a decomposition that does not close is a bug, and surfacing it beats
    quietly shipping a residual.
    """
    prior_products = {p for p, f in prior.by_product.items() if f.units > 0}
    current_products = {p for p, f in current.by_product.items() if f.units > 0}

    continuing = sorted(prior_products & current_products)
    entering = sorted(current_products - prior_products)
    exiting = sorted(prior_products - current_products)

    # --- terms for products that traded in both periods --------------------
    price_effect = 0.0
    mix_effect = 0.0
    volume_effect = 0.0

    q0_total = sum(prior.by_product[p].units for p in continuing)
    q1_total = sum(current.by_product[p].units for p in continuing)
    rev0_continuing = sum(prior.by_product[p].revenue for p in continuing)

    if continuing and q0_total > 0 and q1_total > 0:
        avg_price_0 = rev0_continuing / q0_total

        for pid in continuing:
            p0: ProductPeriodFacts = prior.by_product[pid]
            p1: ProductPeriodFacts = current.by_product[pid]

            price_effect += (p1.avg_price - p0.avg_price) * p1.units

            share_0 = p0.units / q0_total
            share_1 = p1.units / q1_total
            mix_effect += (share_1 - share_0) * p0.avg_price * q1_total

        volume_effect = (q1_total - q0_total) * avg_price_0

    # --- terms for range changes ------------------------------------------
    new_product_effect = sum(current.by_product[p].revenue for p in entering)
    discontinued_effect = -sum(prior.by_product[p].revenue for p in exiting)

    total_variance = current.revenue - prior.revenue

    terms = [
        PVMTerm(
            name="price",
            label="Price",
            value=price_effect,
            explanation=(
                "Realised price change on continuing products, valued at "
                "current volumes."
            ),
        ),
        PVMTerm(
            name="volume",
            label="Volume",
            value=volume_effect,
            explanation=(
                "Total unit growth on continuing products, valued at the prior "
                "average price and holding mix constant."
            ),
        ),
        PVMTerm(
            name="mix",
            label="Mix",
            value=mix_effect,
            explanation=(
                "Shift in the unit share between continuing products, valued at "
                "prior prices."
            ),
        ),
        PVMTerm(
            name="new_product",
            label="New product",
            value=new_product_effect,
            explanation=(
                "Revenue from products with no prior-period history, reported "
                "separately rather than absorbed into mix."
                if entering
                else "No products entered the range this period."
            ),
        ),
        PVMTerm(
            name="discontinued",
            label="Discontinued",
            value=discontinued_effect,
            explanation=(
                "Revenue lost from products that stopped trading."
                if exiting
                else "No products left the range this period."
            ),
        ),
    ]

    residual = total_variance - sum(t.value for t in terms)
    closes = abs(residual) <= CLOSURE_TOLERANCE

    if not closes:
        raise ValueError(
            f"PVM decomposition failed to close: total variance "
            f"{total_variance:,.4f} vs sum of terms "
            f"{sum(t.value for t in terms):,.4f} (residual {residual:,.4f})"
        )

    return PVMResult(
        prior_period=prior.period,
        current_period=current.period,
        prior_revenue=prior.revenue,
        current_revenue=current.revenue,
        total_variance=total_variance,
        terms=terms,
        residual=residual,
        closes=closes,
        continuing_products=continuing,
        entering_products=entering,
        exiting_products=exiting,
    )
