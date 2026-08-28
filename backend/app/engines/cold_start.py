"""Hierarchical empirical-Bayes estimate for a SKU with too little history.

A product that launched eight days ago has a sample mean, but that mean is a
terrible estimate: with n small, the sampling variance swamps it. The standard
remedy is to borrow strength from a hierarchy -- pool the sibling products in
the same category and shrink the sparse estimate toward the pooled prior, with
the amount of shrinkage decided by how much evidence the SKU has of its own.

The normal-normal model:

    y_i | theta_i ~ N(theta_i, sigma^2 / n_i)      sample mean of n_i days
    theta_i       ~ N(mu, tau^2)                   prior across sibling SKUs

    B              = (sigma^2 / n_i) / (sigma^2 / n_i + tau^2)
    posterior mean = B * mu + (1 - B) * y_i
    posterior var  = B * tau^2

B is the weight on the prior. It goes to 1 when the SKU has almost no data, and
to 0 as the SKU accumulates history -- so the estimator hands control back to the
observations exactly as fast as the evidence justifies.

Every one of mu, tau^2 and sigma^2 is estimated from the warehouse here. The
previous implementation accepted them as keyword defaults and never queried the
database at all, which meant its "posterior" was a constant.
"""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel

from app.engines.reconciler import DataReconciler

Z_90 = 1.6448536269514722  # two-sided 90% normal quantile

# Reported as a convenience label only. The estimator's behaviour is continuous
# in n through the shrinkage weight B -- nothing changes discontinuously when an
# entity crosses this line, and no decision is taken on it. It exists so a UI
# can flag "this SKU is young" without implying a cliff.
COLD_START_DAY_LABEL_THRESHOLD = 14

# Assumptions this estimator relies on, stated because empirical Bayes borrows
# strength across groups and is only valid where that borrowing is defensible:
#
#   1. EXCHANGEABILITY. The target and its prior pool are draws from a common
#      population. Pooling across an arbitrary catalogue violates this -- a
#      treadmill is not a plausible prior for a yoga mat -- so the pool is
#      restricted to the target's own category where one exists, and the pool
#      actually used is reported on the estimate.
#   2. APPROXIMATE NORMALITY of the daily mean. Justified by the CLT for a mean
#      over several days; weak for n of 1 or 2, where the interval is indicative
#      rather than calibrated.
#   3. KNOWN VARIANCE COMPONENTS. sigma^2 and tau^2 are estimated, not known, so
#      the credible interval is narrower than a fully Bayesian treatment would
#      give. With a small pool, tau^2 is itself noisy.
#
# Where assumption 1 cannot be met -- no sibling entities at all -- the estimator
# refuses rather than silently pooling across unrelated groups.
MIN_PRIOR_POOL_SIZE = 2


class ColdStartEstimate(BaseModel):
    sku: str
    is_cold_start: bool
    days_observed: int

    observed_mean: float
    observed_variance: float

    prior_mean: float
    prior_variance: float
    prior_pool: list[str]
    prior_pool_observations: int

    shrinkage_weight: float
    posterior_mean: float
    posterior_variance: float
    credible_interval_90: tuple[float, float]

    pool_scope: str
    assumptions: list[str]
    limitations: list[str]
    method: str = "hierarchical_empirical_bayes"

    @property
    def interval_width(self) -> float:
        lo, hi = self.credible_interval_90
        return hi - lo

    def prior_share_pct(self) -> float:
        """How much of the estimate comes from the category rather than the SKU."""
        return self.shrinkage_weight * 100.0


class ColdStartEngine:
    def __init__(self, reconciler: DataReconciler) -> None:
        self.reconciler = reconciler

    def _entity_category(self, entity: str) -> str | None:
        row = self.reconciler.conn.execute(
            "SELECT any_value(category) FROM pos_orders WHERE product_id = ?", [entity]
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def _prior_pool(self, exclude: str) -> tuple[dict[str, list[float]], str]:
        """Sibling series to borrow strength from, and the scope actually used.

        Exchangeability is the load-bearing assumption of empirical Bayes, so the
        pool is drawn from the target's own category first. Falling back to the
        whole catalogue is a weaker assumption, and the estimate says so rather
        than presenting both cases identically.
        """
        category = self._entity_category(exclude)

        def pool_for(sql: str, params: list) -> dict[str, list[float]]:
            entities = [r[0] for r in self.reconciler.conn.execute(sql, params).fetchall()]
            series = {e: self.reconciler.daily_units_series(e) for e in entities}
            return {e: s for e, s in series.items() if len(s) >= 2}

        if category is not None:
            same_category = pool_for(
                """
                SELECT DISTINCT product_id FROM pos_orders
                WHERE product_id <> ? AND category = ? ORDER BY product_id
                """,
                [exclude, category],
            )
            if len(same_category) >= MIN_PRIOR_POOL_SIZE:
                return same_category, f"category '{category}'"

        catalogue = pool_for(
            "SELECT DISTINCT product_id FROM pos_orders WHERE product_id <> ? ORDER BY product_id",
            [exclude],
        )
        scope = (
            f"whole catalogue (category '{category}' had fewer than "
            f"{MIN_PRIOR_POOL_SIZE} siblings)"
            if category
            else "whole catalogue"
        )
        return catalogue, scope

    def shrinkage_curve(self, sku: str, max_days: int | None = None) -> list[dict]:
        """How the estimate evolves as the SKU accumulates history.

        A single posterior number hides the mechanism. Recomputing it after each
        additional day shows the estimator handing control back from the category
        prior to the SKU's own data exactly as fast as the evidence justifies --
        which is the whole argument for using a hierarchical model rather than
        either a raw sample mean or a flat category assumption.
        """
        observed = self.reconciler.daily_units_series(sku)
        limit = min(max_days or len(observed), len(observed))
        curve: list[dict] = []
        for n in range(1, limit + 1):
            estimate = self.estimate_daily_units(sku, first_n_days=n)
            curve.append(
                {
                    "days": n,
                    "observed_mean": round(estimate.observed_mean, 2),
                    "posterior_mean": round(estimate.posterior_mean, 2),
                    "shrinkage_weight": round(estimate.shrinkage_weight, 4),
                    "interval_width": round(estimate.interval_width, 2),
                }
            )
        return curve

    def estimate_daily_units(self, sku: str, first_n_days: int | None = None) -> ColdStartEstimate:
        """Posterior daily-units estimate for `sku`, shrunk toward its siblings."""
        observed = self.reconciler.daily_units_series(sku)
        if first_n_days is not None:
            observed = observed[:first_n_days]
        n = len(observed)
        if n == 0:
            raise ValueError(f"No observations for SKU '{sku}'")

        obs = np.asarray(observed, dtype=float)
        observed_mean = float(obs.mean())
        # ddof=1 needs at least two points; a single day carries no within-variance
        # information, so fall back to the pooled estimate below.
        observed_var = float(obs.var(ddof=1)) if n >= 2 else 0.0

        siblings, pool_scope = self._prior_pool(sku)
        if not siblings:
            raise ValueError(f"No sibling SKUs available to form a prior for '{sku}'")

        sibling_means = np.array([np.mean(s) for s in siblings.values()], dtype=float)

        # mu: the category's central daily rate.
        prior_mean = float(sibling_means.mean())

        # tau^2: genuine between-SKU spread. With a small pool this is noisy, so
        # floor it at a small positive value -- a zero prior variance would claim
        # the category rate is known exactly and shrink every SKU onto it.
        prior_var = float(sibling_means.var(ddof=1)) if len(sibling_means) >= 2 else 0.0
        prior_var = max(prior_var, 1e-6)

        # sigma^2: within-SKU daily variance, pooled across siblings for
        # stability and blended with the target's own where it has enough days.
        pooled_within = float(np.mean([np.var(s, ddof=1) for s in siblings.values()]))
        within_var = observed_var if n >= 2 else pooled_within
        within_var = max((within_var + pooled_within) / 2.0, 1e-9)

        sampling_var = within_var / n
        shrinkage = sampling_var / (sampling_var + prior_var)

        posterior_mean = shrinkage * prior_mean + (1.0 - shrinkage) * observed_mean
        posterior_var = shrinkage * prior_var
        half_width = Z_90 * float(np.sqrt(posterior_var))

        limitations = [
            "Score is an estimate, not a forecast: it describes the daily rate observed "
            "so far, shrunk toward the pool, with no trend or seasonality term.",
            f"tau^2 is estimated from {len(siblings)} sibling entities and is itself noisy "
            "at this pool size.",
        ]
        if n < 3:
            limitations.append(
                f"With n={n}, the normal approximation for the daily mean is weak; the "
                "interval is indicative rather than calibrated."
            )
        if "whole catalogue" in pool_scope:
            limitations.append(
                "Prior pooled across unlike entities, weakening the exchangeability "
                "assumption; treat the prior mean as a rough anchor."
            )

        return ColdStartEstimate(
            sku=sku,
            pool_scope=pool_scope,
            assumptions=[
                f"Exchangeability between the target and its prior pool ({pool_scope}).",
                "Approximate normality of the daily mean.",
                "Variance components treated as known once estimated.",
            ],
            limitations=limitations,
            is_cold_start=n < COLD_START_DAY_LABEL_THRESHOLD,
            days_observed=n,
            observed_mean=observed_mean,
            observed_variance=observed_var,
            prior_mean=prior_mean,
            prior_variance=prior_var,
            prior_pool=sorted(siblings),
            prior_pool_observations=sum(len(s) for s in siblings.values()),
            shrinkage_weight=shrinkage,
            posterior_mean=posterior_mean,
            posterior_variance=posterior_var,
            credible_interval_90=(
                max(0.0, posterior_mean - half_width),
                posterior_mean + half_width,
            ),
        )
