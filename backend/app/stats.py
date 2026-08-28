"""Small statistical helpers, implemented directly so they can be explained.

The only non-obvious one is Benjamini-Hochberg. Every automated-insight system
tests many hypotheses at once -- each KPI against each dimension slice -- and
that inflates the family-wise error rate. With m independent tests at a raw 5%
threshold you expect roughly m/20 false positives *by construction*, so an
uncorrected ranked list is guaranteed to contain spurious findings.

Benjamini-Hochberg controls the false discovery rate instead: the expected
proportion of false positives *among the findings you report*. It is less
stringent than Bonferroni-style family-wise control and therefore more powerful,
which matters when the whole point is to surface real movements.
"""

from __future__ import annotations

import numpy as np

MAD_TO_SIGMA = 1.4826  # makes MAD a consistent estimator of sigma for normal data


def benjamini_hochberg(p_values: list[float]) -> list[float]:
    """Return BH-adjusted q-values, in the same order as the input.

    q_(i) = min over j >= i of (m / j) * p_(j), enforced monotone and clipped
    to 1.0. A q-value of 0.10 means: if you accept every finding at or below
    this level, at most 10% of them are expected to be false discoveries.
    """
    m = len(p_values)
    if m == 0:
        return []

    p = np.asarray(p_values, dtype=float)
    order = np.argsort(p)
    ranked = p[order]

    # Step up from the largest p-value, taking a running minimum.
    scaled = ranked * m / np.arange(1, m + 1)
    q_sorted = np.minimum.accumulate(scaled[::-1])[::-1]
    q_sorted = np.clip(q_sorted, 0.0, 1.0)

    q = np.empty(m, dtype=float)
    q[order] = q_sorted
    return q.tolist()


def robust_z(baseline: list[float], value: float) -> float:
    """Deviation of `value` from `baseline`, scaled by MAD rather than sigma.

    A single spike inflates the standard deviation enough to hide itself. The
    median absolute deviation does not move, so the outlier still reads as one.
    """
    if len(baseline) < 2:
        return 0.0

    arr = np.asarray(baseline, dtype=float)
    median = float(np.median(arr))
    mad = float(np.median(np.abs(arr - median)))

    if mad == 0.0:
        # Degenerate baseline: fall back to standard deviation, and report zero
        # rather than infinity when the series is genuinely constant.
        sd = float(np.std(arr))
        if sd == 0.0:
            return 0.0
        return (value - median) / sd

    return (value - median) / (MAD_TO_SIGMA * mad)


def pct_change(prior: float, current: float) -> float | None:
    """Percentage change, or None where it is undefined.

    Growth from a zero base is not "infinite percent" -- it is a quantity the
    ratio cannot express. Returning infinity lets a new product sort to the top
    of every ranking on an artefact of the arithmetic, so this returns None and
    makes callers fall back to the absolute gate.
    """
    if prior == 0:
        return 0.0 if current == 0 else None
    return (current - prior) / abs(prior) * 100.0
