"""Shapley decomposition of a gross-margin movement.

Gross margin is a *ratio*:

    margin% = (Revenue - COGS - Freight) / Revenue

Ratios do not decompose additively. The multi-dimensional root-cause literature
(Adtributor, HotSpot) is explicit that its methods target *additive* KPIs, and
CMMD exists precisely because derived measures need separate treatment. So the
honest options are a closed-form bridge or nothing -- and a hardcoded set of
driver contributions is nothing wearing a lab coat.

This module treats the movement as a cooperative game over five factors:

    price   -- realised selling prices per product
    cogs    -- unit cost of goods per product
    freight -- unit freight per product
    mix     -- the quantity vector, i.e. relative shares between products
    new     -- entry of products with no prior-period history

and computes each factor's Shapley value over the 2^5 counterfactual states.

Two properties make this defensible rather than decorative:

1. **Efficiency.** The Shapley axioms guarantee the contributions sum *exactly*
   to margin1 - margin0. Closure is not an approximation to be tolerated; it is
   a theorem, and the code asserts it anyway.

2. **Order independence.** A sequential "hold everything else at base" bridge
   gives different answers depending on the order factors are substituted.
   Shapley averages over all orderings, removing that arbitrariness -- the same
   reason it is used for causal change attribution in Budhathoki & Janzing,
   "Why did the distribution change?".

A useful sanity check falls out of the construction: margin% is scale-invariant,
so multiplying every quantity by a constant must leave every contribution
unchanged. `tests/test_margin_bridge.py` asserts exactly that, which is what
proves the "mix" term really is mix and not volume in disguise.
"""

from __future__ import annotations

from itertools import combinations
from math import factorial

from pydantic import BaseModel

from app.engines.reconciler import PeriodSummary

CLOSURE_TOLERANCE = 1e-6  # percentage points

FACTORS: tuple[str, ...] = ("price", "cogs", "freight", "mix", "new")

FACTOR_LABELS = {
    "price": "Price",
    "cogs": "Unit COGS",
    "freight": "Unit freight",
    "mix": "Product mix",
    "new": "New product entry",
}

FACTOR_EXPLANATIONS = {
    "price": "Change in realised selling price per unit.",
    "cogs": "Change in cost of goods per unit at constant mix.",
    "freight": "Change in inbound freight per unit at constant mix.",
    "mix": "Shift in unit share toward products with different margin profiles.",
    "new": "Margin effect of products entering the range with no prior history.",
}


class MarginContribution(BaseModel):
    factor: str
    label: str
    value_pp: float
    explanation: str


class MarginBridge(BaseModel):
    prior_period: str
    current_period: str
    prior_margin_pct: float
    current_margin_pct: float
    delta_pp: float
    contributions: list[MarginContribution]
    residual_pp: float
    closes: bool
    method: str = "shapley_5_factor"

    @property
    def by_factor(self) -> dict[str, float]:
        return {c.factor: c.value_pp for c in self.contributions}

    def ranked(self) -> list[MarginContribution]:
        return sorted(self.contributions, key=lambda c: abs(c.value_pp), reverse=True)

    def primary_driver(self) -> MarginContribution | None:
        ranked = self.ranked()
        return ranked[0] if ranked else None


class _MarginGame:
    """Evaluates margin% for any subset of factors moved to the current period."""

    def __init__(self, prior: PeriodSummary, current: PeriodSummary) -> None:
        prior_ids = {p for p, f in prior.by_product.items() if f.units > 0}
        current_ids = {p for p, f in current.by_product.items() if f.units > 0}

        # Products with prior history. Products present only now are governed by
        # the `new` factor; a product that stops trading simply has q1 = 0, so
        # the `mix` factor removes it naturally.
        self.base_ids = sorted(prior_ids)
        self.entering_ids = sorted(current_ids - prior_ids)

        self.prior = prior
        self.current = current
        self._cache: dict[frozenset[str], float] = {}

    def _attr(self, pid: str, period: PeriodSummary, fallback: PeriodSummary, name: str) -> float:
        """Read a per-unit attribute, falling back when the product is absent.

        A product that did not trade in a period has no meaningful price or unit
        cost there. Its quantity is zero in that state, so the fallback value
        never affects the result -- it only keeps the arithmetic total.
        """
        facts = period.by_product.get(pid) or fallback.by_product.get(pid)
        if facts is None:
            return 0.0
        return getattr(facts, name)

    def value(self, moved: frozenset[str]) -> float:
        """Margin% with the named factors at current-period values."""
        if moved in self._cache:
            return self._cache[moved]

        revenue = 0.0
        cost = 0.0

        for pid in self.base_ids:
            qty_source = self.current if "mix" in moved else self.prior
            qty = qty_source.by_product[pid].units if pid in qty_source.by_product else 0.0
            if qty == 0.0:
                continue

            price = self._attr(
                pid, self.current if "price" in moved else self.prior, self.prior, "avg_price"
            )
            cogs = self._attr(
                pid, self.current if "cogs" in moved else self.prior, self.prior, "cogs_per_unit"
            )
            freight = self._attr(
                pid,
                self.current if "freight" in moved else self.prior,
                self.prior,
                "freight_per_unit",
            )

            revenue += price * qty
            cost += (cogs + freight) * qty

        if "new" in moved:
            for pid in self.entering_ids:
                facts = self.current.by_product[pid]
                revenue += facts.avg_price * facts.units
                cost += (facts.cogs_per_unit + facts.freight_per_unit) * facts.units

        result = (revenue - cost) / revenue * 100.0 if revenue else 0.0
        self._cache[moved] = result
        return result


def build_margin_bridge(prior: PeriodSummary, current: PeriodSummary) -> MarginBridge:
    """Decompose the margin movement into exact, order-independent contributions."""
    game = _MarginGame(prior, current)
    n = len(FACTORS)
    contributions: dict[str, float] = {}

    for factor in FACTORS:
        others = [f for f in FACTORS if f != factor]
        total = 0.0
        # Shapley value: average marginal contribution over every coalition.
        for size in range(len(others) + 1):
            weight = factorial(size) * factorial(n - size - 1) / factorial(n)
            for subset in combinations(others, size):
                without = frozenset(subset)
                with_factor = without | {factor}
                total += weight * (game.value(with_factor) - game.value(without))
        contributions[factor] = total

    prior_margin = game.value(frozenset())
    current_margin = game.value(frozenset(FACTORS))
    delta = current_margin - prior_margin
    residual = delta - sum(contributions.values())
    closes = abs(residual) <= CLOSURE_TOLERANCE

    if not closes:
        raise ValueError(
            f"Margin bridge failed to close: delta {delta:.9f} pp vs sum of "
            f"contributions {sum(contributions.values()):.9f} pp "
            f"(residual {residual:.9f} pp). The Shapley efficiency axiom "
            f"guarantees closure, so this indicates a bug in the game definition."
        )

    return MarginBridge(
        prior_period=prior.period,
        current_period=current.period,
        prior_margin_pct=prior_margin,
        current_margin_pct=current_margin,
        delta_pp=delta,
        contributions=[
            MarginContribution(
                factor=f,
                label=FACTOR_LABELS[f],
                value_pp=contributions[f],
                explanation=FACTOR_EXPLANATIONS[f],
            )
            for f in FACTORS
        ],
        residual_pp=residual,
        closes=closes,
    )


def freight_rate_change(prior: PeriodSummary, current: PeriodSummary) -> dict[str, float]:
    """Per-product freight rate movement, so the narrative can cite it.

    The demo's headline "+14.4% ocean freight surcharge" is derived here from
    the ERP unit rates rather than typed into a template.
    """
    out: dict[str, float] = {}
    for pid, cur in current.by_product.items():
        old = prior.by_product.get(pid)
        if old is None or old.freight_per_unit == 0:
            continue
        out[pid] = (cur.freight_per_unit - old.freight_per_unit) / old.freight_per_unit * 100.0
    return out
