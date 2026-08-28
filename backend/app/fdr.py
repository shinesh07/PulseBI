"""Multiple-testing correction with hypothesis identity preserved.

Two mistakes the audit found in the previous implementation, both of which make
the correction wrong rather than merely weak:

1. **Untested hypotheses were entering the pool.** Every candidate contributed
   to m, including those for which no test had been run. Inflating m makes every
   adjusted p-value larger and so weakens genuine findings. Only hypotheses with
   a valid p-value in [0, 1] may be corrected.

2. **Dependence was never stated.** Benjamini-Hochberg controls the FDR under
   independence or positive regression dependence (PRDS). In this engine the
   hypotheses are emphatically not independent -- revenue, COGS and freight for
   one product are near-deterministic functions of the same daily unit counts.
   Benjamini-Yekutieli controls the FDR under *arbitrary* dependence by dividing
   alpha by the harmonic number H(m), at the cost of power.

Both procedures are implemented. The choice is declared in the contract and
reported on the result, so the assumption being relied on is always visible.

Step-up formulation, for m tested hypotheses at level alpha, with p-values
sorted ascending as p_(1) <= ... <= p_(m):

    BH: reject up to the largest i with  p_(i) <= (i / m) * alpha
    BY: reject up to the largest i with  p_(i) <= (i / m) * alpha / H(m)

Equivalently, adjusted p-values are computed by a monotone step-up transform and
compared against alpha directly, which is what this module returns. The two
formulations select the same hypotheses; adjusted p-values are reported because
they are auditable per-hypothesis.
"""

from __future__ import annotations

from enum import Enum
from math import isfinite
from typing import Hashable, Mapping, Sequence

import numpy as np
from pydantic import BaseModel


class FDRMethod(str, Enum):
    BENJAMINI_HOCHBERG = "benjamini_hochberg"
    BENJAMINI_YEKUTIELI = "benjamini_yekutieli"

    @property
    def dependence_assumption(self) -> str:
        if self is FDRMethod.BENJAMINI_HOCHBERG:
            return (
                "Independence or positive regression dependence (PRDS) among the "
                "tested hypotheses."
            )
        return "Arbitrary dependence. No assumption made about the correlation structure."


class CorrectedHypothesis(BaseModel):
    key: str
    raw_p_value: float
    adjusted_p_value: float
    significant: bool
    rank: int


class FDRResult(BaseModel):
    method: FDRMethod
    alpha: float
    dependence_assumption: str
    m_tested: int
    n_excluded: int
    excluded_keys: list[str]
    corrected: dict[str, CorrectedHypothesis]

    @property
    def n_significant(self) -> int:
        return sum(1 for c in self.corrected.values() if c.significant)

    @property
    def n_raw_significant(self) -> int:
        """How many would pass an uncorrected threshold at the same alpha."""
        return sum(1 for c in self.corrected.values() if c.raw_p_value <= self.alpha)

    @property
    def changed_by_correction(self) -> list[str]:
        """Hypotheses that a raw threshold would have accepted but the correction rejects.

        This is the observable effect of the correction, and it is what makes the
        procedure load-bearing rather than a displayed statistic.
        """
        return sorted(
            key
            for key, c in self.corrected.items()
            if c.raw_p_value <= self.alpha and not c.significant
        )

    def is_significant(self, key: str) -> bool:
        """Final decision for one hypothesis. Never consults the raw p-value."""
        entry = self.corrected.get(key)
        return bool(entry and entry.significant)

    def adjusted_p(self, key: str) -> float | None:
        entry = self.corrected.get(key)
        return entry.adjusted_p_value if entry else None


def _harmonic(m: int) -> float:
    """H(m) = sum_{i=1..m} 1/i, the Benjamini-Yekutieli penalty."""
    return float(np.sum(1.0 / np.arange(1, m + 1))) if m > 0 else 1.0


def _is_valid_p(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(float(value))
        and 0.0 <= float(value) <= 1.0
    )


def correct(
    p_values: Mapping[Hashable, float | None],
    *,
    alpha: float,
    method: FDRMethod = FDRMethod.BENJAMINI_HOCHBERG,
) -> FDRResult:
    """Apply a step-up FDR correction, keyed by hypothesis identity.

    Hypotheses whose p-value is None, NaN, infinite, or outside [0, 1] are
    excluded from the pool entirely -- they are not tested hypotheses and must
    not inflate m.

    Returns adjusted p-values keyed by the original identifiers. Sorting happens
    internally and identity is restored afterwards, so no caller can accidentally
    zip a sorted p-value list against an unsorted hypothesis list.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be strictly between 0 and 1, got {alpha}")

    valid: list[tuple[str, float]] = []
    excluded: list[str] = []
    for key, value in p_values.items():
        if _is_valid_p(value):
            valid.append((str(key), float(value)))
        else:
            excluded.append(str(key))

    m = len(valid)
    if m == 0:
        return FDRResult(
            method=method,
            alpha=alpha,
            dependence_assumption=method.dependence_assumption,
            m_tested=0,
            n_excluded=len(excluded),
            excluded_keys=sorted(excluded),
            corrected={},
        )

    penalty = _harmonic(m) if method is FDRMethod.BENJAMINI_YEKUTIELI else 1.0

    # Sort by p-value, keeping the key attached so identity survives.
    ordered = sorted(valid, key=lambda kv: kv[1])
    keys = [k for k, _ in ordered]
    ranked = np.array([p for _, p in ordered], dtype=float)

    # Step-up: scale each p by m/i (and by the BY penalty), then take a running
    # minimum from the largest downward to enforce monotonicity.
    scaled = ranked * penalty * m / np.arange(1, m + 1)
    adjusted = np.minimum.accumulate(scaled[::-1])[::-1]
    adjusted = np.clip(adjusted, 0.0, 1.0)

    corrected = {
        key: CorrectedHypothesis(
            key=key,
            raw_p_value=float(raw),
            adjusted_p_value=float(adj),
            significant=bool(adj <= alpha),
            rank=rank,
        )
        for rank, (key, raw, adj) in enumerate(zip(keys, ranked, adjusted), start=1)
    }

    return FDRResult(
        method=method,
        alpha=alpha,
        dependence_assumption=method.dependence_assumption,
        m_tested=m,
        n_excluded=len(excluded),
        excluded_keys=sorted(excluded),
        corrected=corrected,
    )
