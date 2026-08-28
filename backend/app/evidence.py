"""Evidence ledger.

Every number the analytical layer produces becomes a Fact. The narrative layer
may only reference Fact ids, and the faithfulness verifier walks the rendered
text asserting that each numeral it contains resolves to a Fact in this ledger.

That is what makes "no fabricated numbers" a checked invariant rather than a
claim: if a sentence contains a number the engine never computed, the API
returns an abstention instead of the narrative.
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Any, Iterator

from pydantic import BaseModel, Field


class EvidenceTier(str, Enum):
    """How a value was arrived at.

    The problem brief asks teams to demonstrate *when* they use deterministic
    logic versus statistics versus assumption. Tagging every fact makes that
    answerable per-number rather than per-system.
    """

    DETERMINISTIC_SQL = "deterministic_sql"
    STATISTICAL_ESTIMATE = "statistical_estimate"
    BUSINESS_RULE = "business_rule"
    ASSUMPTION = "assumption"


class Unit(str, Enum):
    USD = "usd"
    PCT = "pct"
    PP = "pp"          # percentage points (a difference of two pct values)
    UNITS = "units"
    RATIO = "ratio"
    COUNT = "count"


class Fact(BaseModel):
    """One computed value, with everything needed to defend it."""

    id: str
    kpi: str
    label: str
    value: float
    unit: Unit
    method: str
    tier: EvidenceTier
    inputs: dict[str, Any] = Field(default_factory=dict)
    source_tables: list[str] = Field(default_factory=list)
    as_of: str | None = None
    confidence: float | None = None
    note: str | None = None

    def render(self) -> str:
        """Human-readable form, used by the deterministic narrative renderer."""
        if self.unit is Unit.USD:
            return f"${self.value:,.2f}"
        if self.unit is Unit.PCT:
            return f"{self.value:.1f}%"
        if self.unit is Unit.PP:
            return f"{self.value:+.2f} pp"
        if self.unit in (Unit.UNITS, Unit.COUNT):
            return f"{self.value:,.0f}"
        return f"{self.value:,.4f}"


class EvidenceLedger:
    """An append-only store of Facts for a single analysis run."""

    def __init__(self) -> None:
        self._facts: dict[str, Fact] = {}

    # -- writing -----------------------------------------------------------

    def add(
        self,
        *,
        kpi: str,
        label: str,
        value: float,
        unit: Unit,
        method: str,
        tier: EvidenceTier,
        inputs: dict[str, Any] | None = None,
        source_tables: list[str] | None = None,
        as_of: str | None = None,
        confidence: float | None = None,
        note: str | None = None,
    ) -> Fact:
        fact_id = self._mint_id(kpi, label, method)
        fact = Fact(
            id=fact_id,
            kpi=kpi,
            label=label,
            value=float(value),
            unit=unit,
            method=method,
            tier=tier,
            inputs=inputs or {},
            source_tables=source_tables or [],
            as_of=as_of,
            confidence=confidence,
            note=note,
        )
        self._facts[fact_id] = fact
        return fact

    @staticmethod
    def _mint_id(kpi: str, label: str, method: str) -> str:
        """Stable, content-derived id so the same computation yields the same ref."""
        digest = hashlib.sha256(f"{kpi}|{label}|{method}".encode()).hexdigest()[:6]
        return f"{kpi.upper().replace('_', '-')}-{digest}"

    # -- reading -----------------------------------------------------------

    def get(self, fact_id: str) -> Fact | None:
        return self._facts.get(fact_id)

    def all(self) -> list[Fact]:
        return list(self._facts.values())

    def __len__(self) -> int:
        return len(self._facts)

    def __iter__(self) -> Iterator[Fact]:
        return iter(self._facts.values())

    # -- verification support ---------------------------------------------

    def resolve(self, value: float, *, tolerance: float = 0.01) -> Fact | None:
        """Find a Fact whose value matches, within tolerance.

        Used by the faithfulness verifier. Tolerance is absolute for small
        magnitudes and relative for large ones, so that a currency figure
        rendered as "$1,550,000.00" still matches 1550000.0 after rounding, and
        a margin rendered as "26.9%" matches 26.900000000000002.
        """
        for fact in self._facts.values():
            if _close(fact.value, value, tolerance):
                return fact
            # A percentage may legitimately be quoted with the sign dropped
            # ("compressed by 3.10 pp" for a value of -3.10).
            if _close(abs(fact.value), abs(value), tolerance):
                return fact
        return None

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {fid: f.model_dump(mode="json") for fid, f in self._facts.items()}


def _close(a: float, b: float, tolerance: float) -> bool:
    if abs(a - b) <= tolerance:
        return True
    scale = max(abs(a), abs(b))
    if scale > 1000.0:
        return abs(a - b) / scale <= 1e-6
    return False
