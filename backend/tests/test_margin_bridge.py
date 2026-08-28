"""Closure, invariance and correctness tests for the Shapley margin bridge."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.engines.margin_bridge import (
    CLOSURE_TOLERANCE,
    FACTORS,
    build_margin_bridge,
    freight_rate_change,
)
from tests.factories import make_period, make_product, scale_quantities

# --------------------------------------------------------------------------
# Against the seeded demo data
# --------------------------------------------------------------------------


def test_bridge_closes_on_demo_data(periods):
    prior, current = periods
    bridge = build_margin_bridge(prior, current)

    assert bridge.closes
    assert bridge.delta_pp == pytest.approx(
        sum(c.value_pp for c in bridge.contributions), abs=CLOSURE_TOLERANCE
    )


def test_bridge_reproduces_the_observed_margin_movement(periods, reconciler):
    """The bridge must explain the margin the reconciler independently computed."""
    prior, current = periods
    bridge = build_margin_bridge(prior, current)

    assert bridge.prior_margin_pct == pytest.approx(prior.gross_margin_pct, abs=1e-9)
    assert bridge.current_margin_pct == pytest.approx(current.gross_margin_pct, abs=1e-9)
    assert bridge.delta_pp == pytest.approx(-3.10, abs=1e-9)


def test_price_is_the_dominant_margin_driver(periods):
    """Regression guard on the finding that overturned the original narrative.

    The repo previously hardcoded the margin drivers as mix -1.90 pp and freight
    -2.40 pp. Computed honestly, the treadmill price cut ($300 -> $280) is the
    dominant driver at roughly -3.2 pp, freight is a much smaller -0.7 pp, and
    mix is *positive* -- treadmills carry a 33.3% margin rate against the
    smartwatch's 25.0%, so shifting share toward them helps.
    """
    prior, current = periods
    by_factor = build_margin_bridge(prior, current).by_factor

    assert by_factor["price"] < -3.0
    assert by_factor["mix"] > 0.0, "mix shift toward higher-rate SKUs must help margin"
    assert -1.0 < by_factor["freight"] < 0.0
    assert build_margin_bridge(prior, current).primary_driver().factor == "price"


def test_freight_rate_is_derived_not_asserted(periods):
    prior, current = periods
    rates = freight_rate_change(prior, current)

    # 20.000 -> 22.875 per unit on the treadmill.
    assert rates["TRD-01"] == pytest.approx(14.375, abs=1e-6)
    assert rates["SMW-01"] == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------
# Scale invariance -- proves "mix" is really mix, not volume in disguise
# --------------------------------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(st.floats(min_value=0.1, max_value=50.0, allow_nan=False, allow_infinity=False))
def test_margin_bridge_is_scale_invariant(factor):
    """Margin% does not depend on absolute volume, only on relative shares.

    Multiplying every quantity in the current period by a constant must leave
    every contribution unchanged. If a "mix" term moved under pure scaling it
    would actually be capturing volume, which does not belong in a ratio bridge
    at all.
    """
    prior = make_period(
        "P0",
        {
            "A": make_product("A", units=100, price=10, cogs=4, freight=1),
            "B": make_product("B", units=200, price=20, cogs=12, freight=2),
        },
    )
    current = make_period(
        "P1",
        {
            "A": make_product("A", units=180, price=11, cogs=4, freight=1.5),
            "B": make_product("B", units=150, price=19, cogs=12, freight=2),
        },
    )

    base = build_margin_bridge(prior, current)
    scaled = build_margin_bridge(prior, scale_quantities(current, factor))

    for name in FACTORS:
        assert scaled.by_factor[name] == pytest.approx(base.by_factor[name], abs=1e-9)


# --------------------------------------------------------------------------
# Property tests -- the Shapley efficiency axiom must hold everywhere
# --------------------------------------------------------------------------

_qty = st.floats(min_value=1.0, max_value=10_000.0, allow_nan=False, allow_infinity=False)
_price = st.floats(min_value=5.0, max_value=1_000.0, allow_nan=False, allow_infinity=False)


@st.composite
def _two_periods(draw):
    n = draw(st.integers(min_value=1, max_value=4))
    ids = [f"P{i:02d}" for i in range(n)]

    in_prior = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    in_current = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    if not any(in_prior):
        in_prior[0] = True
    if not any(in_current):
        in_current[0] = True

    def build(mask):
        out = {}
        for i, pid in enumerate(ids):
            if not mask[i]:
                continue
            price = draw(_price)
            # Keep unit costs below price so margins stay in a sane range.
            cogs = draw(st.floats(min_value=0.0, max_value=price * 0.6))
            freight = draw(st.floats(min_value=0.0, max_value=price * 0.2))
            out[pid] = make_product(pid, units=draw(_qty), price=price, cogs=cogs, freight=freight)
        return out

    return make_period("P0", build(in_prior)), make_period("P1", build(in_current))


@settings(max_examples=150, deadline=None)
@given(_two_periods())
def test_shapley_efficiency_holds_on_arbitrary_inputs(pair):
    """Sum of contributions == total movement. Guaranteed by the axiom; verified anyway."""
    prior, current = pair
    bridge = build_margin_bridge(prior, current)

    assert bridge.delta_pp == pytest.approx(
        sum(c.value_pp for c in bridge.contributions), abs=1e-6
    )


@settings(max_examples=100, deadline=None)
@given(_two_periods())
def test_every_factor_receives_a_contribution(pair):
    prior, current = pair
    bridge = build_margin_bridge(prior, current)

    assert {c.factor for c in bridge.contributions} == set(FACTORS)


def test_identical_periods_yield_zero_contributions():
    products = {
        "A": make_product("A", units=100, price=10, cogs=4, freight=1),
        "B": make_product("B", units=50, price=20, cogs=9, freight=2),
    }
    bridge = build_margin_bridge(make_period("P0", products), make_period("P1", dict(products)))

    assert bridge.delta_pp == pytest.approx(0.0, abs=1e-9)
    for contribution in bridge.contributions:
        assert contribution.value_pp == pytest.approx(0.0, abs=1e-9)


def test_pure_cogs_rise_is_attributed_to_cogs():
    prior = make_period("P0", {"A": make_product("A", units=100, price=10, cogs=4, freight=1)})
    current = make_period("P1", {"A": make_product("A", units=100, price=10, cogs=6, freight=1)})

    by_factor = build_margin_bridge(prior, current).by_factor

    assert by_factor["cogs"] == pytest.approx(-20.0, abs=1e-9)
    for other in ("price", "freight", "mix", "new"):
        assert by_factor[other] == pytest.approx(0.0, abs=1e-9)
