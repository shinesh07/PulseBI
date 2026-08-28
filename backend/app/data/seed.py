"""Deterministic synthetic data generator.

Three sources at three grains, engineered so the four demo scenarios are real
computations rather than narrated constants:

  * Multi-factor movement -- revenue +55% while gross margin falls 3.10 pp,
    driven by a genuine mix shift toward bulky goods plus a freight rate rise.
  * New product entry     -- YOG-01 launches mid-November with 12 days of
    history, so it appears as its own bar in the PVM waterfall AND is the
    cold-start SKU.
  * Broken attribution    -- 35% of November treadmill orders lose their UTM
    tags, which the confidence engine detects from the data.
  * Grain mismatch        -- marketing weeks straddle the month boundary, so
    the reconciler must allocate them pro-rata.

Ground truth (verified in tests/test_seed.py):
    October  revenue  1,000,000.00   margin 30.0%
    November revenue  1,550,000.00   margin 26.9%
    delta             +550,000.00    margin -3.10 pp
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent
SEED = 42

PRODUCTS = {
    "TRD-01": {"name": "Treadmill", "category": "Bulky"},
    "SMW-01": {"name": "SmartWatch", "category": "Light"},
    "YOG-01": {"name": "Yoga Mat", "category": "Fitness Accessory"},
}

# Period targets. October is the baseline, November the period under analysis.
UNITS = {
    "2023-10": {"TRD-01": 2000, "SMW-01": 4000, "YOG-01": 0},
    "2023-11": {"TRD-01": 4000, "SMW-01": 3800, "YOG-01": 1000},
}
PRICES = {
    "2023-10": {"TRD-01": 300.0, "SMW-01": 100.0, "YOG-01": 0.0},
    "2023-11": {"TRD-01": 280.0, "SMW-01": 100.0, "YOG-01": 50.0},
}

# ERP unit economics. The November treadmill freight rise of 20.000 -> 22.875
# is a +14.375% surcharge -- the figure the narrative cites, derived not typed.
ERP = [
    {"month": "2023-10-01", "product_id": "TRD-01", "cogs_per_unit": 180.0, "freight_per_unit": 20.000},
    {"month": "2023-10-01", "product_id": "SMW-01", "cogs_per_unit": 70.0, "freight_per_unit": 5.000},
    {"month": "2023-11-01", "product_id": "TRD-01", "cogs_per_unit": 180.0, "freight_per_unit": 22.875},
    {"month": "2023-11-01", "product_id": "SMW-01", "cogs_per_unit": 70.0, "freight_per_unit": 5.000},
    {"month": "2023-11-01", "product_id": "YOG-01", "cogs_per_unit": 30.0, "freight_per_unit": 6.550},
]

# YOG-01 launches with fewer than 14 days of history -- the cold-start trigger.
COLD_START_ACTIVE_DAYS = 12

# Two distinct attribution failures, because they behave differently:
#
#  * A chronic 35% tag loss on bulky checkout flows across all of November.
#    Degraded, but survivable -- the engine should still answer, with lowered
#    confidence and a caveat.
#  * An acute webhook outage over 1-7 November that drops 85% of tags on
#    everything. Scoped to that window there is genuinely not enough evidence,
#    and the engine should abstain rather than guess.
#
# Having both is the point: abstention is only meaningful if the system can also
# recognise the cases where degradation does *not* warrant abstaining.
UTM_LOSS_RATE_CHRONIC = 0.35
UTM_LOSS_RATE_OUTAGE = 0.85
OUTAGE_START = date(2023, 11, 1)
OUTAGE_END = date(2023, 11, 8)  # exclusive


def _period_days(period: str) -> tuple[date, int]:
    year, month = (int(p) for p in period.split("-"))
    start = date(year, month, 1)
    next_month = date(year + (month == 12), (month % 12) + 1, 1)
    return start, (next_month - start).days


def _split_into_orders(day_units: int, rng: random.Random) -> list[int]:
    """Break a day's units into individual orders, preserving the total exactly."""
    orders: list[int] = []
    remaining = day_units
    while remaining > 0:
        qty = min(remaining, rng.randint(1, 3))
        orders.append(qty)
        remaining -= qty
    return orders


def generate_pos_orders() -> pd.DataFrame:
    rng = random.Random(SEED)
    npr = np.random.default_rng(SEED)

    rows: list[dict] = []
    order_id = 1000

    for period, targets in UNITS.items():
        start, days = _period_days(period)
        for pid, total_units in targets.items():
            if total_units == 0:
                continue

            # The new SKU only trades for its final COLD_START_ACTIVE_DAYS.
            if pid == "YOG-01":
                active_days = COLD_START_ACTIVE_DAYS
                active_start = start + timedelta(days=days - COLD_START_ACTIVE_DAYS)
            else:
                active_days = days
                active_start = start

            # Multinomial keeps the period total exact while varying by day.
            daily = npr.multinomial(total_units, np.ones(active_days) / active_days)

            for offset, day_units in enumerate(daily):
                if day_units == 0:
                    continue
                day = active_start + timedelta(days=int(offset))
                in_outage = OUTAGE_START <= day < OUTAGE_END
                if in_outage:
                    loss_rate = UTM_LOSS_RATE_OUTAGE
                elif period == "2023-11" and pid == "TRD-01":
                    loss_rate = UTM_LOSS_RATE_CHRONIC
                else:
                    loss_rate = 0.0

                for qty in _split_into_orders(int(day_units), rng):
                    lost_utm = loss_rate > 0.0 and rng.random() < loss_rate
                    rows.append(
                        {
                            "order_id": f"ORD-{order_id}",
                            "date": day.isoformat(),
                            "product_id": pid,
                            "product_name": PRODUCTS[pid]["name"],
                            "category": PRODUCTS[pid]["category"],
                            "units": qty,
                            "price_per_unit": PRICES[period][pid],
                            "revenue": round(qty * PRICES[period][pid], 2),
                            "utm_source": None if lost_utm else "Meta",
                            "utm_medium": None if lost_utm else "cpc",
                        }
                    )
                    order_id += 1

    return pd.DataFrame(rows)


def generate_erp_financials() -> pd.DataFrame:
    return pd.DataFrame(ERP)


def generate_marketing_spend() -> pd.DataFrame:
    """Weekly grain, deliberately misaligned with month boundaries.

    Weeks start on Monday 2023-10-02, so the week of 30 Oct straddles October
    and November and must be split pro-rata by the reconciler.
    """
    rows: list[dict] = []
    week = date(2023, 10, 2)
    end = date(2023, 12, 4)
    while week < end:
        # Spend triples for the November promotional push.
        spend = 15000.0 if week.month == 11 or week == date(2023, 10, 30) else 5000.0

        # Acquisition saturates: tripling spend does not triple customers. The
        # 0.6 exponent gives diminishing returns, so blended CAC genuinely rises
        # under the promotion instead of staying pinned to a constant.
        new_customers = int(round(spend**0.6))

        rows.append(
            {
                "week_start": week.isoformat(),
                "channel": "Meta",
                "spend": spend,
                "impressions": int(spend * 100),
                "clicks": int(spend * 2),
                "new_customers": new_customers,
            }
        )
        week += timedelta(days=7)
    return pd.DataFrame(rows)


def write_all(out_dir: Path | None = None) -> dict[str, Path]:
    out = Path(out_dir) if out_dir else DATA_DIR
    out.mkdir(parents=True, exist_ok=True)

    written: dict[str, Path] = {}
    for name, frame in (
        ("pos_orders", generate_pos_orders()),
        ("erp_financials", generate_erp_financials()),
        ("marketing_spend", generate_marketing_spend()),
    ):
        path = out / f"{name}.csv"
        frame.to_csv(path, index=False)
        written[name] = path
    return written


def _summarise() -> str:
    """Print the ground truth so a regression is obvious at generation time."""
    pos = generate_pos_orders()
    erp = generate_erp_financials()
    lines = []
    for period in ("2023-10", "2023-11"):
        month_rows = pos[pos["date"].str.startswith(period)]
        revenue = month_rows["revenue"].sum()
        units = month_rows.groupby("product_id")["units"].sum()
        erp_month = erp[erp["month"].str.startswith(period)].set_index("product_id")
        cogs = sum(u * erp_month.loc[pid, "cogs_per_unit"] for pid, u in units.items())
        freight = sum(u * erp_month.loc[pid, "freight_per_unit"] for pid, u in units.items())
        margin = (revenue - cogs - freight) / revenue * 100.0
        lines.append(
            f"  {period}  revenue={revenue:>12,.2f}  cogs={cogs:>11,.2f}  "
            f"freight={freight:>10,.2f}  margin={margin:6.2f}%"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    written = write_all()
    print(f"Generated at {datetime.now():%Y-%m-%d %H:%M:%S} (seed={SEED})")
    for name, path in written.items():
        print(f"  {name:<20} -> {path}")
    print("\nGround truth:")
    print(_summarise())
