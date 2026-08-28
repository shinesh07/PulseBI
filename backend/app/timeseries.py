"""Normalised daily observation series.

The detector previously consumed bare `list[float]` produced by a SQL GROUP BY.
Dropping the dates made three problems invisible:

  * a missing trading day silently shortened the series instead of being a gap,
  * two series of different lengths were compared positionally by the test,
  * callers could not slice a window, detect duplicates, or verify ordering.

A TimeSeries carries its dates, so windowing is exact and coverage is knowable.
Construction is the single place where non-finite values are rejected, which is
what keeps NaN and Infinity out of everything downstream.

Windows are half-open [start, end) throughout the codebase. Half-open intervals
compose without double-counting the boundary day, which matters as soon as one
window abuts another.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from math import isfinite
from typing import Iterable, Iterator, Sequence

import numpy as np


class DuplicatePolicy(str, Enum):
    """What to do when the same day appears more than once.

    SUM is right for additive quantities (two batches of orders on one day).
    LAST is right for snapshots (an end-of-day balance restated).
    ERROR refuses to guess.
    """

    SUM = "sum"
    LAST = "last"
    ERROR = "error"


@dataclass(frozen=True, order=True)
class Observation:
    day: date
    value: float


class TimeSeries:
    """An ordered, de-duplicated, gap-aware series of daily observations."""

    __slots__ = ("_observations", "name", "duplicate_policy")

    def __init__(
        self,
        observations: Iterable[Observation],
        *,
        name: str = "",
        duplicate_policy: DuplicatePolicy = DuplicatePolicy.SUM,
    ) -> None:
        self.name = name
        self.duplicate_policy = duplicate_policy
        self._observations = self._normalise(observations, duplicate_policy, name)

    # -- construction ------------------------------------------------------

    @staticmethod
    def _normalise(
        observations: Iterable[Observation],
        policy: DuplicatePolicy,
        name: str,
    ) -> tuple[Observation, ...]:
        buckets: dict[date, float] = defaultdict(float)
        seen: set[date] = set()

        for obs in observations:
            if not isfinite(obs.value):
                raise ValueError(
                    f"Non-finite observation in series '{name}' on {obs.day}: {obs.value!r}. "
                    "NaN and Infinity are rejected at construction so they cannot reach "
                    "business-facing output."
                )
            if obs.day in seen:
                if policy is DuplicatePolicy.ERROR:
                    raise ValueError(f"Duplicate observation for {obs.day} in series '{name}'")
                if policy is DuplicatePolicy.LAST:
                    buckets[obs.day] = obs.value
                    continue
            seen.add(obs.day)
            buckets[obs.day] += obs.value

        # Sorting here means callers never depend on input ordering.
        return tuple(Observation(day, buckets[day]) for day in sorted(buckets))

    @classmethod
    def from_pairs(
        cls,
        pairs: Iterable[tuple[date | str, float]],
        *,
        name: str = "",
        duplicate_policy: DuplicatePolicy = DuplicatePolicy.SUM,
    ) -> TimeSeries:
        observations = [
            Observation(day if isinstance(day, date) else date.fromisoformat(str(day)), float(value))
            for day, value in pairs
        ]
        return cls(observations, name=name, duplicate_policy=duplicate_policy)

    @classmethod
    def empty(cls, name: str = "") -> TimeSeries:
        return cls([], name=name)

    # -- access ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._observations)

    def __iter__(self) -> Iterator[Observation]:
        return iter(self._observations)

    def __repr__(self) -> str:
        if not self._observations:
            return f"TimeSeries('{self.name}', empty)"
        return (
            f"TimeSeries('{self.name}', n={len(self)}, "
            f"{self.first_day.isoformat()}..{self.last_day.isoformat()})"
        )

    @property
    def observations(self) -> tuple[Observation, ...]:
        return self._observations

    @property
    def is_empty(self) -> bool:
        return not self._observations

    @property
    def n(self) -> int:
        """Number of observed days. This is the sample size for any test."""
        return len(self._observations)

    @property
    def first_day(self) -> date:
        return self._observations[0].day

    @property
    def last_day(self) -> date:
        return self._observations[-1].day

    def values(self) -> list[float]:
        return [o.value for o in self._observations]

    def days(self) -> list[date]:
        return [o.day for o in self._observations]

    def total(self) -> float:
        return float(sum(o.value for o in self._observations))

    def mean(self) -> float | None:
        return float(np.mean(self.values())) if self._observations else None

    def median(self) -> float | None:
        return float(np.median(self.values())) if self._observations else None

    # -- windowing ---------------------------------------------------------

    def window(self, start: date, end: date) -> TimeSeries:
        """Observations in [start, end). The only supported way to scope a series.

        Every calculation that claims to describe an event window routes through
        here, so a window can never silently widen to the whole dataset.
        """
        if end < start:
            raise ValueError(f"Window end {end} precedes start {start}")
        return TimeSeries(
            [o for o in self._observations if start <= o.day < end],
            name=f"{self.name}[{start.isoformat()}:{end.isoformat()})",
            duplicate_policy=self.duplicate_policy,
        )

    def before(self, cutoff: date) -> TimeSeries:
        """Observations strictly before `cutoff` -- the historical baseline pool.

        Kept distinct from window() so the difference between "what happened in
        the event" and "what normally happens" is explicit at every call site.
        """
        return TimeSeries(
            [o for o in self._observations if o.day < cutoff],
            name=f"{self.name}(<{cutoff.isoformat()})",
            duplicate_policy=self.duplicate_policy,
        )

    # -- coverage ----------------------------------------------------------

    def expected_days(self, start: date, end: date) -> int:
        return max(0, (end - start).days)

    def missing_days(self, start: date, end: date) -> list[date]:
        """Calendar days in [start, end) with no observation."""
        present = {o.day for o in self._observations}
        span = self.expected_days(start, end)
        return [d for i in range(span) if (d := start + timedelta(days=i)) not in present]

    def coverage(self, start: date, end: date) -> float:
        """Fraction of the window's calendar days that carry an observation."""
        span = self.expected_days(start, end)
        if span == 0:
            return 0.0
        observed = sum(1 for o in self._observations if start <= o.day < end)
        return observed / span

    def has_internal_gaps(self) -> bool:
        """True if any calendar day between the first and last observation is absent."""
        if self.n < 2:
            return False
        span = (self.last_day - self.first_day).days + 1
        return self.n < span

    def densify(self, start: date, end: date, fill: float = 0.0) -> TimeSeries:
        """Fill absent days in [start, end) with an explicit value.

        Never applied implicitly. A gap in a revenue series may mean "no sales"
        (fill 0) or "the feed failed" (leave absent), and only the caller knows
        which. Guessing wrong turns an outage into a demand collapse.
        """
        present = {o.day: o.value for o in self._observations}
        span = self.expected_days(start, end)
        return TimeSeries(
            [
                Observation(day, present.get(day, fill))
                for i in range(span)
                if (day := start + timedelta(days=i)) is not None
            ],
            name=f"{self.name}(densified)",
            duplicate_policy=self.duplicate_policy,
        )


def series_from_rows(
    rows: Sequence[tuple],
    *,
    name: str = "",
    duplicate_policy: DuplicatePolicy = DuplicatePolicy.SUM,
) -> TimeSeries:
    """Build a series from (date, value) database rows, tolerating NULL values."""
    return TimeSeries.from_pairs(
        ((day, float(value)) for day, value in rows if value is not None),
        name=name,
        duplicate_policy=duplicate_policy,
    )
