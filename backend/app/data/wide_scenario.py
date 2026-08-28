"""A wide catalogue for exercising multiple-testing correction.

The headline demo has three products, which yields roughly a dozen testable
hypotheses -- too few for a multiplicity correction to change any decision. That
makes FDR control decorative: correct, but never observable.

This scenario generates a catalogue of many products whose effects are known by
construction:

    TRUE_EFFECT   -- a real shift, large enough that any method finds it
    BORDERLINE    -- a small shift near the detection boundary, where the choice
                     of correction genuinely decides the outcome
    NULL          -- no shift at all; every rejection here is a false discovery

Because the ground truth is known per product, the suite can assert what the
correction actually bought: how many null hypotheses a raw threshold would have
rejected, and how many survive Benjamini-Hochberg and Benjamini-Yekutieli.

The generator is parameterised rather than fixed, so a test can request whatever
number of hypotheses it needs and the algorithms are never tuned to one shape of
data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent


class EffectClass(str, Enum):
    TRUE_EFFECT = "TRUE_EFFECT"
    BORDERLINE = "BORDERLINE"
    NULL = "NULL"


@dataclass(frozen=True)
class ProductSpec:
    product_id: str
    category: str
    effect_class: EffectClass
    baseline_daily_units: float
    effect_multiplier: float
    price: float
    cogs_per_unit: float
    freight_per_unit: float
    noise_cv: float

    @property
    def has_true_effect(self) -> bool:
        return self.effect_class is not EffectClass.NULL


def build_catalogue(
    *,
    n_true: int = 6,
    n_borderline: int = 10,
    n_null: int = 14,
    seed: int = 7,
) -> list[ProductSpec]:
    """Products with known ground truth, spread across effect classes."""
    rng = np.random.default_rng(seed)
    specs: list[ProductSpec] = []

    def add(index: int, effect_class: EffectClass, multiplier: float) -> None:
        specs.append(
            ProductSpec(
                product_id=f"SKU-{index:03d}",
                category=f"CAT-{index % 5}",
                effect_class=effect_class,
                baseline_daily_units=float(rng.uniform(40, 160)),
                effect_multiplier=multiplier,
                price=float(rng.uniform(20, 200)),
                cogs_per_unit=0.0,
                freight_per_unit=0.0,
                # Coefficient of variation: how noisy each product's daily series
                # is. Higher noise makes a given effect harder to detect, which is
                # what creates genuinely borderline cases rather than a clean split.
                noise_cv=float(rng.uniform(0.15, 0.40)),
            )
        )

    index = 0
    for _ in range(n_true):
        add(index, EffectClass.TRUE_EFFECT, float(rng.uniform(1.45, 1.9)))
        index += 1
    for _ in range(n_borderline):
        add(index, EffectClass.BORDERLINE, float(rng.uniform(1.10, 1.22)))
        index += 1
    for _ in range(n_null):
        add(index, EffectClass.NULL, 1.0)
        index += 1

    # Unit economics derived from price so margins stay plausible.
    return [
        ProductSpec(
            **{
                **spec.__dict__,
                "cogs_per_unit": round(spec.price * 0.55, 2),
                "freight_per_unit": round(spec.price * 0.06, 2),
            }
        )
        for spec in specs
    ]


def generate(
    catalogue: list[ProductSpec],
    *,
    baseline_start: date = date(2023, 10, 1),
    baseline_days: int = 31,
    event_days: int = 30,
    seed: int = 7,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Daily POS rows and monthly ERP rates for the wide catalogue.

    Daily units are drawn from a gamma distribution, which is positive, skewed,
    and has a tunable coefficient of variation -- a much better model of daily
    order counts than a normal, and the reason the detector uses a
    non-parametric test.
    """
    rng = np.random.default_rng(seed)
    event_start = baseline_start + timedelta(days=baseline_days)

    rows: list[dict] = []
    for spec in catalogue:
        for window_start, n_days, multiplier in (
            (baseline_start, baseline_days, 1.0),
            (event_start, event_days, spec.effect_multiplier),
        ):
            mean = spec.baseline_daily_units * multiplier
            shape = 1.0 / (spec.noise_cv**2)
            scale = mean / shape
            draws = rng.gamma(shape, scale, size=n_days)

            for offset, units in enumerate(draws):
                units = float(max(0.0, units))
                day = window_start + timedelta(days=offset)
                rows.append(
                    {
                        "order_id": f"W-{spec.product_id}-{offset}-{multiplier:.2f}",
                        "date": day.isoformat(),
                        "product_id": spec.product_id,
                        "product_name": spec.product_id,
                        "category": spec.category,
                        "units": round(units, 3),
                        "price_per_unit": spec.price,
                        "revenue": round(units * spec.price, 2),
                        "utm_source": "Meta",
                        "utm_medium": "cpc",
                    }
                )

    erp_rows = [
        {
            "month": month.isoformat(),
            "product_id": spec.product_id,
            "cogs_per_unit": spec.cogs_per_unit,
            "freight_per_unit": spec.freight_per_unit,
        }
        for spec in catalogue
        for month in (baseline_start.replace(day=1), event_start.replace(day=1))
    ]

    return pd.DataFrame(rows), pd.DataFrame(erp_rows)


def write(out_dir: Path | None = None, **kwargs) -> dict[str, Path]:
    out = Path(out_dir) if out_dir else DATA_DIR
    catalogue = build_catalogue()
    pos, erp = generate(catalogue, **kwargs)

    written = {}
    for name, frame in (("wide_pos_orders", pos), ("wide_erp_financials", erp)):
        path = out / f"{name}.csv"
        frame.to_csv(path, index=False)
        written[name] = path
    return written


if __name__ == "__main__":
    catalogue = build_catalogue()
    counts: dict[str, int] = {}
    for spec in catalogue:
        counts[spec.effect_class.value] = counts.get(spec.effect_class.value, 0) + 1
    print(f"catalogue of {len(catalogue)} products: {counts}")
    for path in write().values():
        print(f"  wrote {path}")
