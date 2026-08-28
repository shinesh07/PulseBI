"""Closure and correctness tests for the revenue PVM decomposition."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.engines.decomposition import CLOSURE_TOLERANCE, decompose_revenue
from tests.factories import make_period, make_product

# --------------------------------------------------------------------------
# Against the seeded demo data
# --------------------------------------------------------------------------


def test_pvm_closes_on_demo_data(periods):
    prior, current = periods
    result = decompose_revenue(prior, current)

    assert result.closes
    assert abs(result.residual) <= CLOSURE_TOLERANCE
    assert result.total_variance == pytest.approx(
        sum(t.value for t in result.terms), abs=CLOSURE_TOLERANCE
    )


def test_pvm_reports_the_new_product_separately(periods):
    """YOG-01 has no October history, so it must be its own term.

    A three-term bridge would silently fold this $50,000 into "mix and other",
    which is the distortion the Controller-Akademie treatment exists to avoid.
    """
    prior, current = periods
    result = decompose_revenue(prior, current)

    assert result.entering_products == ["YOG-01"]
    assert result.exiting_products == []
    assert result.terms_by_name["new_product"] == pytest.approx(50_000.0, abs=0.01)


def test_pvm_matches_hand_computed_terms(periods):
    """Independently derived by hand from the seed parameters.

    TRD 2000@300 -> 4000@280, SMW 4000@100 -> 3800@100, YOG 0 -> 1000@50.
      price  = (280-300)*4000 + (100-100)*3800            = -80,000
      volume = (7800-6000) * (1,000,000/6000)             = +300,000
      mix    = 7800 * [(.512821-.333333)*300 + (.487179-.666667)*100]
                                                          = +280,000
      new    = 1000*50                                    =  +50,000
    """
    prior, current = periods
    terms = decompose_revenue(prior, current).terms_by_name

    assert terms["price"] == pytest.approx(-80_000.0, abs=0.01)
    assert terms["volume"] == pytest.approx(300_000.0, abs=0.01)
    assert terms["mix"] == pytest.approx(280_000.0, abs=0.01)
    assert terms["new_product"] == pytest.approx(50_000.0, abs=0.01)
    assert terms["discontinued"] == pytest.approx(0.0, abs=0.01)


# --------------------------------------------------------------------------
# Property tests -- closure must hold on arbitrary inputs, not just the demo
# --------------------------------------------------------------------------

_qty = st.floats(min_value=1.0, max_value=100_000.0, allow_nan=False, allow_infinity=False)
_price = st.floats(min_value=0.5, max_value=5_000.0, allow_nan=False, allow_infinity=False)
_cost = st.floats(min_value=0.0, max_value=4_000.0, allow_nan=False, allow_infinity=False)


@st.composite
def _two_periods(draw, min_products: int = 1, max_products: int = 5):
    n = draw(st.integers(min_value=min_products, max_value=max_products))
    ids = [f"P{i:02d}" for i in range(n)]

    # Independently decide which products trade in each period, guaranteeing at
    # least one in each so the bridge has something to decompose.
    in_prior = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    in_current = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    if not any(in_prior):
        in_prior[0] = True
    if not any(in_current):
        in_current[0] = True

    prior_products = {}
    current_products = {}
    for i, pid in enumerate(ids):
        if in_prior[i]:
            prior_products[pid] = make_product(
                pid, units=draw(_qty), price=draw(_price), cogs=draw(_cost), freight=draw(_cost)
            )
        if in_current[i]:
            current_products[pid] = make_product(
                pid, units=draw(_qty), price=draw(_price), cogs=draw(_cost), freight=draw(_cost)
            )

    return make_period("P0", prior_products), make_period("P1", current_products)


@settings(max_examples=250, deadline=None)
@given(_two_periods())
def test_pvm_closes_on_arbitrary_inputs(pair):
    prior, current = pair
    result = decompose_revenue(prior, current)

    total = sum(t.value for t in result.terms)
    scale = max(abs(result.total_variance), 1.0)
    assert abs(result.total_variance - total) / scale < 1e-9


@settings(max_examples=100, deadline=None)
@given(_two_periods())
def test_entering_and_exiting_sets_are_disjoint(pair):
    prior, current = pair
    result = decompose_revenue(prior, current)

    assert not set(result.entering_products) & set(result.exiting_products)
    assert not set(result.entering_products) & set(result.continuing_products)
    assert not set(result.exiting_products) & set(result.continuing_products)


def test_identical_periods_produce_zero_variance():
    products = {
        "A": make_product("A", units=100, price=10, cogs=4, freight=1),
        "B": make_product("B", units=50, price=20, cogs=9, freight=2),
    }
    prior = make_period("P0", products)
    current = make_period("P1", dict(products))

    result = decompose_revenue(prior, current)

    assert result.total_variance == pytest.approx(0.0, abs=1e-9)
    for term in result.terms:
        assert term.value == pytest.approx(0.0, abs=1e-9)


def test_pure_price_move_is_attributed_entirely_to_price():
    prior = make_period("P0", {"A": make_product("A", units=100, price=10, cogs=4, freight=1)})
    current = make_period("P1", {"A": make_product("A", units=100, price=12, cogs=4, freight=1)})

    terms = decompose_revenue(prior, current).terms_by_name

    assert terms["price"] == pytest.approx(200.0, abs=1e-9)
    assert terms["volume"] == pytest.approx(0.0, abs=1e-9)
    assert terms["mix"] == pytest.approx(0.0, abs=1e-9)


def test_pure_volume_move_is_attributed_entirely_to_volume():
    prior = make_period("P0", {"A": make_product("A", units=100, price=10, cogs=4, freight=1)})
    current = make_period("P1", {"A": make_product("A", units=150, price=10, cogs=4, freight=1)})

    terms = decompose_revenue(prior, current).terms_by_name

    assert terms["volume"] == pytest.approx(500.0, abs=1e-9)
    assert terms["price"] == pytest.approx(0.0, abs=1e-9)
    assert terms["mix"] == pytest.approx(0.0, abs=1e-9)
