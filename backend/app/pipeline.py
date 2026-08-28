"""End-to-end detection pipeline.

    data
      -> series provider          (window-scoped access, no KPI names above here)
      -> measurement              (baseline window vs event window)
      -> change classification    (is the ratio defined at all?)
      -> materiality              (business bar, from the contract)
      -> statistical test         (non-parametric, on daily observations)
      -> FDR pool                 (validly tested hypotheses only)
      -> multiplicity correction  (BH or BY, per the contract)
      -> contradiction            (structural reconciliation, same window)
      -> confidence               (six measured signals, missing lowers)
      -> decision                 (DETECTED / LOW_CONFIDENCE / ABSTAIN / ...)
      -> evidence package         (invariant-checked, LLM-safe)

The narrative layer consumes the last stage and nothing earlier.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from app.contracts import ContractStore, get_contract_store
from app.engines.confidence import ConfidenceEngine
from app.engines.contradiction import ContradictionDetector, ContradictionReport
from app.engines.detector import BaselineMode, Decision, DetectionResult, MovementDetector
from app.engines.reconciler import DataReconciler
from app.evidence_package import EvidencePackage, build_evidence_package
from app.series import DuckDBSeriesProvider, SeriesKey


class AnalysisResult(BaseModel):
    baseline_window: tuple[date, date]
    event_window: tuple[date, date]
    detection: DetectionResult
    contradictions: ContradictionReport
    packages: list[EvidencePackage]

    @property
    def detected(self) -> list[EvidencePackage]:
        return [p for p in self.packages if p.decision is Decision.DETECTED]

    @property
    def low_confidence(self) -> list[EvidencePackage]:
        return [p for p in self.packages if p.decision is Decision.LOW_CONFIDENCE]

    @property
    def abstained(self) -> list[EvidencePackage]:
        return [p for p in self.packages if p.decision is Decision.ABSTAIN]

    @property
    def reportable(self) -> list[EvidencePackage]:
        return [p for p in self.packages if p.decision.is_reportable]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for package in self.packages:
            counts[package.decision.value] = counts.get(package.decision.value, 0) + 1
        return counts


class DetectionPipeline:
    def __init__(
        self,
        reconciler: DataReconciler | None = None,
        store: ContractStore | None = None,
    ) -> None:
        self.store = store or get_contract_store()
        self.reconciler = reconciler or DataReconciler(self.store)
        self.provider = DuckDBSeriesProvider(self.reconciler.conn, self.store)
        self.detector = MovementDetector(self.provider, self.store)
        self.contradiction_detector = ContradictionDetector(self.provider, self.store)
        self.confidence_engine = ConfidenceEngine(self.provider, self.store)

    def analyse(
        self,
        baseline_window: tuple[date, date],
        event_window: tuple[date, date],
        keys: list[SeriesKey] | None = None,
        entity_filter: str | None = None,
        baseline_mode: BaselineMode = BaselineMode.MATCHED_LENGTH,
    ) -> AnalysisResult:
        """Run the full pipeline over an explicit pair of windows.

        Both windows are required and neither defaults, because every
        window-leakage bug this pipeline was rebuilt to fix began with an
        implicit "the whole period" default somewhere in the chain.
        """
        detection = self.detector.detect(
            baseline_window, event_window, keys=keys, baseline_mode=baseline_mode
        )

        # Reconciliation is scoped to the same windows as the detection, so a
        # narrow analysis cannot inherit a wider period's consistency signal.
        contradictions = self.contradiction_detector.evaluate(baseline_window, event_window)
        freshness = self.reconciler.freshness()
        stale = [f.source for f in freshness if f.is_stale]

        packages: list[EvidencePackage] = []
        for hypothesis in detection.hypotheses:
            confidence = self.confidence_engine.assess(
                hypothesis, contradictions, freshness, entity_filter=entity_filter
            )
            packages.append(
                build_evidence_package(
                    hypothesis,
                    confidence,
                    contradictions,
                    detection.fdr,
                    stale_sources=stale,
                )
            )

        packages.sort(
            key=lambda p: (p.decision.is_reportable, p.confidence, p.materiality.exceedance),
            reverse=True,
        )
        return AnalysisResult(
            baseline_window=baseline_window,
            event_window=event_window,
            detection=detection,
            contradictions=contradictions,
            packages=packages,
        )
