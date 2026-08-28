"""Explicit modelling of what a KPI movement *is*.

The percentage-change formula is undefined when the baseline is zero, and nearly
useless when the baseline is merely close to zero: at a baseline of 1e-12 a move
to 100 yields a relative change of 1e16 percent, which then propagates into
materiality scoring and dominates every ranking on an artefact of the arithmetic.

The audit found exactly that. The fix is not to substitute 1 for a zero
denominator, nor to emit Infinity, but to say plainly which of several distinct
situations occurred and to leave the ratio undefined where it genuinely is.

    baseline    current     classification
    --------    -------     --------------
    absent      any         NO_PRIOR_BASELINE
    any         absent      NO_CURRENT_VALUE
    0           0           NO_ACTIVITY
    0           > 0         NEW_ACTIVITY          (ratio undefined)
    > 0         0           CEASED_ACTIVITY       (ratio = -100%)
    ~0          any         UNSTABLE_BASELINE     (ratio undefined)
    > 0         same        NO_CHANGE
    > 0         higher      INCREASE
    > 0         lower       DECREASE

`relative_change` is None wherever the ratio is not meaningful, and callers must
fall back to absolute evidence rather than inventing a number.
"""

from __future__ import annotations

from enum import Enum
from math import isfinite

from pydantic import BaseModel, field_validator


class ChangeType(str, Enum):
    NO_PRIOR_BASELINE = "NO_PRIOR_BASELINE"
    NO_CURRENT_VALUE = "NO_CURRENT_VALUE"
    NO_ACTIVITY = "NO_ACTIVITY"
    NEW_ACTIVITY = "NEW_ACTIVITY"
    CEASED_ACTIVITY = "CEASED_ACTIVITY"
    UNSTABLE_BASELINE = "UNSTABLE_BASELINE"
    NO_CHANGE = "NO_CHANGE"
    INCREASE = "INCREASE"
    DECREASE = "DECREASE"

    @property
    def is_directional(self) -> bool:
        """Whether the type carries a meaningful up/down reading."""
        return self in {ChangeType.INCREASE, ChangeType.DECREASE, ChangeType.CEASED_ACTIVITY}

    @property
    def is_measurable(self) -> bool:
        """Whether an absolute change could be computed at all."""
        return self not in {ChangeType.NO_PRIOR_BASELINE, ChangeType.NO_CURRENT_VALUE}


class ChangeMeasurement(BaseModel):
    """A movement, with the ratio present only where it is defined."""

    baseline: float | None
    current: float | None
    absolute_change: float | None
    relative_change_pct: float | None
    change_type: ChangeType
    reason: str
    baseline_floor: float

    model_config = {"frozen": True}

    @field_validator("baseline", "current", "absolute_change", "relative_change_pct")
    @classmethod
    def _must_be_finite(cls, v: float | None) -> float | None:
        """Invariant: no NaN or Infinity may exist on a measurement."""
        if v is not None and not isfinite(v):
            raise ValueError(
                f"Non-finite value {v!r} in a ChangeMeasurement. Relative change must be "
                "None where undefined, never Infinity."
            )
        return v

    @property
    def has_relative(self) -> bool:
        return self.relative_change_pct is not None

    @property
    def direction(self) -> str:
        if self.absolute_change is None or self.absolute_change == 0:
            return "flat"
        return "up" if self.absolute_change > 0 else "down"

    def describe(self) -> str:
        # The absolute value may be absent because it was never measurable, or
        # because access control redacted it. In the second case the relative
        # change survives and is what the reader is entitled to see.
        if self.absolute_change is None:
            if self.has_relative:
                return f"{self.relative_change_pct:+.1f}%"
            return self.reason
        if self.has_relative:
            return f"{self.absolute_change:+,.2f} ({self.relative_change_pct:+.1f}%)"
        return f"{self.absolute_change:+,.2f} ({self.change_type.value})"


def measure_change(
    baseline: float | None,
    current: float | None,
    *,
    baseline_floor: float = 0.0,
) -> ChangeMeasurement:
    """Classify a movement and compute the ratio only where it is defined.

    `baseline_floor` is the magnitude below which a relative change stops being
    informative. It is supplied by the caller from the KPI's declared scale
    rather than guessed here, because what counts as "near zero" is a property
    of the metric: a $5 baseline is negligible for revenue and enormous for a
    per-unit freight rate.
    """
    if baseline is not None and not isfinite(baseline):
        raise ValueError(f"Non-finite baseline: {baseline!r}")
    if current is not None and not isfinite(current):
        raise ValueError(f"Non-finite current value: {current!r}")
    if baseline_floor < 0:
        raise ValueError("baseline_floor must be non-negative")

    def build(
        change_type: ChangeType,
        reason: str,
        absolute: float | None,
        relative: float | None,
    ) -> ChangeMeasurement:
        return ChangeMeasurement(
            baseline=baseline,
            current=current,
            absolute_change=absolute,
            relative_change_pct=relative,
            change_type=change_type,
            reason=reason,
            baseline_floor=baseline_floor,
        )

    if baseline is None:
        return build(
            ChangeType.NO_PRIOR_BASELINE,
            "No baseline observation available; the movement cannot be quantified.",
            None,
            None,
        )
    if current is None:
        return build(
            ChangeType.NO_CURRENT_VALUE,
            "No current observation available; the movement cannot be quantified.",
            None,
            None,
        )

    absolute = current - baseline

    if baseline == 0.0 and current == 0.0:
        return build(ChangeType.NO_ACTIVITY, "No activity in either period.", 0.0, 0.0)

    if baseline == 0.0:
        return build(
            ChangeType.NEW_ACTIVITY,
            (
                "Activity began from a zero baseline. Relative change is undefined; "
                "assess on absolute impact."
            ),
            absolute,
            None,
        )

    if abs(baseline) < baseline_floor:
        return build(
            ChangeType.UNSTABLE_BASELINE,
            (
                f"Baseline magnitude {abs(baseline):.6g} is below the floor "
                f"{baseline_floor:.6g}; a ratio against it is not informative. "
                "Assess on absolute impact."
            ),
            absolute,
            None,
        )

    relative = absolute / abs(baseline) * 100.0

    # A declared floor is the intended guard, but it cannot be the only one: a
    # baseline at the very bottom of the float range overflows this division to
    # infinity even when the floor is zero. Any ratio that is not representable
    # is by definition not informative, so it is classified rather than emitted.
    if not isfinite(relative):
        return build(
            ChangeType.UNSTABLE_BASELINE,
            (
                f"Baseline magnitude {abs(baseline):.3g} is too small for a ratio to be "
                "representable; the relative change overflows. Assess on absolute impact."
            ),
            absolute,
            None,
        )

    if current == 0.0:
        return build(
            ChangeType.CEASED_ACTIVITY,
            "Activity ceased entirely relative to the baseline.",
            absolute,
            relative,
        )
    if absolute == 0.0:
        return build(ChangeType.NO_CHANGE, "No movement against the baseline.", 0.0, 0.0)

    return build(
        ChangeType.INCREASE if absolute > 0 else ChangeType.DECREASE,
        "Movement measured against a stable baseline.",
        absolute,
        relative,
    )
