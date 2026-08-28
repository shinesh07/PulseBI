"""Loader for the governed semantic contract.

`contracts.yaml` is the single source of truth for metric formulas, grains,
materiality thresholds, lineage, confidence weights and access policy. Every
engine reads its parameters from here.

tests/test_contracts.py enforces conformance in both directions, so the
contract cannot silently drift away from the code.
"""

from __future__ import annotations

import functools
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from app.fdr import FDRMethod

CONTRACT_PATH = Path(__file__).with_name("contracts.yaml")


class Relationship(str, Enum):
    ADDITIVE = "additive"
    MULTIPLICATIVE = "multiplicative"
    DERIVED_RATIO = "derived_ratio"
    INFLUENCING = "influencing"


class AccessLevel(str, Enum):
    ALLOW = "allow"
    MASK = "mask"
    DENY = "deny"


class Source(BaseModel):
    file: str
    system: str
    grain: str
    refresh_cadence_minutes: int
    freshness_sla_hours: int
    date_column: str


class Persona(BaseModel):
    title: str
    levers: list[str]


class Materiality(BaseModel):
    abs_usd: float
    pct: float
    # Baseline magnitude below which a relative change stops being informative.
    # Declared per KPI because "near zero" is a property of the metric's scale:
    # a $5 baseline is negligible for revenue and enormous for a per-unit rate.
    baseline_floor: float = 0.0

    def is_material(self, *, abs_change: float, pct_change: float | None) -> bool:
        """A movement counts if it clears EITHER gate.

        Absolute impact catches large moves in small-percentage terms; the
        percentage gate catches large relative moves on small bases.

        A threshold of zero means "this gate does not apply" -- otherwise a
        ratio KPI like gross margin, whose absolute change is in percentage
        points rather than currency, would pass the currency gate trivially and
        every movement would read as material. A pct_change of None means the
        ratio is undefined (growth from a zero base), so only the absolute gate
        can speak.
        """
        by_abs = self.abs_usd > 0 and abs(abs_change) >= self.abs_usd
        by_pct = self.pct > 0 and pct_change is not None and abs(pct_change) >= self.pct
        return by_abs or by_pct

    def exceedance(self, *, abs_change: float, pct_change: float | None) -> float:
        """How many times over the materiality threshold this movement sits.

        Unit-free, so movements in dollars and movements in percentage points
        can be ranked against each other on one scale.
        """
        ratios = []
        if self.abs_usd > 0:
            ratios.append(abs(abs_change) / self.abs_usd)
        if self.pct > 0 and pct_change is not None:
            ratios.append(abs(pct_change) / self.pct)
        return max(ratios) if ratios else 0.0


class MetricTree(BaseModel):
    relationship: Relationship
    children: list[str] = Field(default_factory=list)


class LineageNode(BaseModel):
    node: str
    type: str


class KPI(BaseModel):
    label: str
    unit: str
    grain: str
    formula: str
    sources: list[str]
    materiality: Materiality
    metric_tree: MetricTree
    lineage: list[LineageNode]
    access: dict[str, AccessLevel]


class DetectionConfig(BaseModel):
    fdr_alpha: float
    fdr_method: FDRMethod
    min_baseline_points: int
    robust_scale: str

    @field_validator("fdr_alpha")
    @classmethod
    def _alpha_in_range(cls, v: float) -> float:
        if not 0.0 < v < 1.0:
            raise ValueError("fdr_alpha must be strictly between 0 and 1")
        return v


class ConfidenceConfig(BaseModel):
    weights: dict[str, float]
    abstain_threshold: float
    low_confidence_threshold: float
    action_threshold: float
    staleness_penalty_per_day: float

    @field_validator("weights")
    @classmethod
    def _weights_sum_to_one(cls, v: dict[str, float]) -> dict[str, float]:
        total = sum(v.values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"confidence weights must sum to 1.0, got {total}")
        return v


class FeedbackConfig(BaseModel):
    max_rank_weight_delta: float


class Contract(BaseModel):
    version: int
    meta: dict[str, Any]
    sources: dict[str, Source]
    personas: dict[str, Persona]
    kpis: dict[str, KPI]
    detection: DetectionConfig
    confidence: ConfidenceConfig
    feedback: FeedbackConfig


class ContractStore:
    """Read-only accessor over the parsed contract."""

    def __init__(self, path: Path | str = CONTRACT_PATH) -> None:
        self.path = Path(path)
        raw = yaml.safe_load(self.path.read_text())
        self.contract = Contract.model_validate(raw)
        self._validate_referential_integrity()

    # -- integrity ---------------------------------------------------------

    def _validate_referential_integrity(self) -> None:
        """Catch contract bugs at load time rather than mid-demo."""
        persona_names = set(self.contract.personas)
        source_names = set(self.contract.sources)
        kpi_names = set(self.contract.kpis)

        for name, kpi in self.contract.kpis.items():
            missing_sources = set(kpi.sources) - source_names
            if missing_sources:
                raise ValueError(f"KPI '{name}' references unknown sources: {sorted(missing_sources)}")

            missing_personas = persona_names - set(kpi.access)
            if missing_personas:
                raise ValueError(
                    f"KPI '{name}' has no access rule for: {sorted(missing_personas)}"
                )

            unknown_personas = set(kpi.access) - persona_names
            if unknown_personas:
                raise ValueError(
                    f"KPI '{name}' declares access for unknown personas: {sorted(unknown_personas)}"
                )

            missing_children = set(kpi.metric_tree.children) - kpi_names
            if missing_children:
                raise ValueError(
                    f"KPI '{name}' metric tree references undeclared KPIs: {sorted(missing_children)}"
                )

            if kpi.metric_tree.relationship is Relationship.DERIVED_RATIO and not kpi.metric_tree.children:
                raise ValueError(f"KPI '{name}' is a derived ratio but declares no children")

    # -- accessors ---------------------------------------------------------

    @property
    def kpi_names(self) -> set[str]:
        return set(self.contract.kpis)

    @property
    def persona_names(self) -> set[str]:
        return set(self.contract.personas)

    def kpi(self, name: str) -> KPI:
        try:
            return self.contract.kpis[name]
        except KeyError:
            raise KeyError(f"KPI '{name}' is not declared in {self.path.name}") from None

    def source(self, name: str) -> Source:
        try:
            return self.contract.sources[name]
        except KeyError:
            raise KeyError(f"Source '{name}' is not declared in {self.path.name}") from None

    def persona(self, name: str) -> Persona:
        try:
            return self.contract.personas[name]
        except KeyError:
            raise KeyError(f"Persona '{name}' is not declared in {self.path.name}") from None

    def access_for(self, kpi_name: str, persona_name: str) -> AccessLevel:
        """The access decision for one persona against one KPI.

        Defaults to DENY for an unrecognised persona -- fail closed, never open.
        """
        kpi = self.kpi(kpi_name)
        return kpi.access.get(persona_name, AccessLevel.DENY)

    def levers_for(self, persona_name: str) -> list[str]:
        return self.persona(persona_name).levers

    @property
    def detection(self) -> DetectionConfig:
        return self.contract.detection

    @property
    def confidence(self) -> ConfidenceConfig:
        return self.contract.confidence

    @property
    def feedback(self) -> FeedbackConfig:
        return self.contract.feedback

    def lineage_for(self, kpi_name: str) -> list[dict[str, str]]:
        return [n.model_dump() for n in self.kpi(kpi_name).lineage]


@functools.lru_cache(maxsize=1)
def get_contract_store() -> ContractStore:
    """Process-wide singleton. Cheap to call from anywhere."""
    return ContractStore()
