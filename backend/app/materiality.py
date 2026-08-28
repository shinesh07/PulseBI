"""Business materiality, kept strictly separate from statistical significance.

These answer different questions and must never be conflated:

    statistical significance  -- "is this movement distinguishable from noise?"
    business materiality      -- "is this movement large enough to act on?"

A movement can be either without being the other. A 0.4% shift measured over
millions of orders is statistically unambiguous and commercially irrelevant. A
40% shift on eleven orders may matter enormously if it clears an absolute
impact threshold, while being far too noisy to call significant.

The gate here evaluates only the second question. It is driven entirely by
thresholds declared in the contract, so it depends on a KPI's economics rather
than on its name.
"""

from __future__ import annotations

from math import isfinite

from pydantic import BaseModel

from app.change import ChangeMeasurement, ChangeType
from app.contracts import Materiality

# An exceedance is a ratio of a movement to a threshold. Anything beyond this is
# a pathology of the arithmetic rather than a real business signal, so it is
# capped to keep one degenerate baseline from dominating every ranking.
MAX_EXCEEDANCE = 1_000.0


class MaterialityDecision(BaseModel):
    """Why a movement did or did not clear the business bar."""

    is_material: bool
    exceedance: float
    reason: str
    gates_evaluated: list[str]
    gates_passed: list[str]

    model_config = {"frozen": True}

    @property
    def was_gated(self) -> bool:
        """False when no gate could be applied at all -- distinct from failing one."""
        return bool(self.gates_evaluated)


def assess_materiality(
    measurement: ChangeMeasurement,
    thresholds: Materiality,
) -> MaterialityDecision:
    """Apply the contract's absolute and relative gates to a measured movement.

    A movement clears the bar if it passes EITHER gate. The absolute gate catches
    large moves that look small in percentage terms; the relative gate catches
    large relative moves on a small base.

    Where the relative change is undefined -- a new product, or a baseline too
    close to zero for a ratio to mean anything -- only the absolute gate can
    speak. That is the correct behaviour: a new product's importance is its
    absolute contribution, not an imaginary growth rate.
    """
    gates_evaluated: list[str] = []
    gates_passed: list[str] = []
    ratios: list[float] = []

    if not measurement.change_type.is_measurable:
        return MaterialityDecision(
            is_material=False,
            exceedance=0.0,
            reason=measurement.reason,
            gates_evaluated=[],
            gates_passed=[],
        )

    absolute = measurement.absolute_change or 0.0

    if thresholds.abs_usd > 0:
        gates_evaluated.append("absolute")
        ratio = abs(absolute) / thresholds.abs_usd
        ratios.append(ratio)
        if ratio >= 1.0:
            gates_passed.append("absolute")

    if thresholds.pct > 0 and measurement.has_relative:
        gates_evaluated.append("relative")
        ratio = abs(measurement.relative_change_pct) / thresholds.pct
        ratios.append(ratio)
        if ratio >= 1.0:
            gates_passed.append("relative")

    exceedance = min(max(ratios) if ratios else 0.0, MAX_EXCEEDANCE)
    if not isfinite(exceedance):
        # Unreachable while ChangeMeasurement rejects non-finite values, but the
        # invariant is asserted here too because this number drives ranking.
        raise ValueError(f"Non-finite exceedance computed from {measurement!r}")

    if not gates_evaluated:
        return MaterialityDecision(
            is_material=False,
            exceedance=0.0,
            reason=(
                "No materiality gate could be applied: the contract declares no "
                "absolute threshold and the relative change is undefined."
            ),
            gates_evaluated=[],
            gates_passed=[],
        )

    if gates_passed:
        reason = f"Cleared the {' and '.join(gates_passed)} materiality threshold."
        if measurement.change_type is ChangeType.NEW_ACTIVITY:
            reason += " Assessed on absolute impact because the baseline is zero."
        elif measurement.change_type is ChangeType.UNSTABLE_BASELINE:
            reason += " Assessed on absolute impact because the baseline is near zero."
        return MaterialityDecision(
            is_material=True,
            exceedance=exceedance,
            reason=reason,
            gates_evaluated=gates_evaluated,
            gates_passed=gates_passed,
        )

    return MaterialityDecision(
        is_material=False,
        exceedance=exceedance,
        reason=(
            f"Below the {' and '.join(gates_evaluated)} materiality threshold "
            f"({exceedance:.2f}x of the bar)."
        ),
        gates_evaluated=gates_evaluated,
        gates_passed=[],
    )
