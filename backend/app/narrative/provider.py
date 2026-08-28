"""Narrative providers, and the gate every one of them passes through.

Two implementations behind one interface:

* `DeterministicProvider` renders persona templates from the evidence package.
  Offline, zero dependencies, and the default -- so a demo can never fail on a
  network call.
* `OllamaProvider` calls a local model over Ollama's HTTP API. Free, offline
  once the model is pulled, and it makes token counts and latency genuinely
  measured rather than projected. Off unless `PULSEBI_NARRATIVE=ollama`.

Both paths are gated by the same faithfulness verifier. That is the point of the
abstraction: the safety property does not depend on which one is running, so
enabling a model changes the prose and nothing about the guarantees.
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.engines.detector import Decision
from app.evidence_package import EvidencePackage
from app.narrative.verifier import UnfaithfulNarrative, VerificationResult, verify

OLLAMA_URL = os.environ.get("PULSEBI_OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("PULSEBI_OLLAMA_MODEL", "llama3.2:3b")


@dataclass
class Narrative:
    text: str
    persona: str
    provider: str
    model: str | None
    verification: VerificationResult
    decision: Decision
    latency_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    @property
    def is_abstention(self) -> bool:
        return self.decision is Decision.ABSTAIN


class NarrativeProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def _render(self, package: EvidencePackage, persona: str) -> str: ...

    def build_prompt(self, package: EvidencePackage, persona: str) -> str:
        """The prompt a model would receive for this finding.

        Defined on the base class so the offline path can still report an
        honest token projection: the estimate is computed over the real
        assembled string, not invented. Subclasses that actually call a model
        override this with the prompt they send.
        """
        citable = "\n".join(
            f"  {name} = {value:,.6g}" for name, value in sorted(self._citable(package).items())
        )
        rules = "\n".join(f"  - {rule}" for rule in package.narration_rules)
        return (
            f"You are writing one short paragraph for a {persona}.\n\n"
            f"RULES:\n{rules}\n\n"
            f"DECISION: {package.decision.value}\n"
            f"REASON: {package.decision_reason}\n"
            f"KPI: {package.label} ({package.kpi}) for entity {package.entity}\n"
            f"CHANGE TYPE: {package.observed_change.change_type.value}\n"
            f"WINDOW: {package.event_window.start} to {package.event_window.end}\n\n"
            f"NUMBERS YOU MAY CITE:\n{citable}\n\n"
            f"Write the paragraph now."
        )

    def generate(self, package: EvidencePackage, persona: str) -> Narrative:
        """Render, then verify. An unverifiable narrative is never returned."""
        package.assert_llm_safe()

        started = time.perf_counter()
        text = self._render(package, persona)
        latency_ms = (time.perf_counter() - started) * 1000.0

        result = verify(text, self._citable(package))
        if not result.ok:
            raise UnfaithfulNarrative(result)

        return Narrative(
            text=text,
            persona=persona,
            provider=self.name,
            model=getattr(self, "model", None),
            verification=result,
            decision=package.decision,
            latency_ms=latency_ms,
            prompt_tokens=getattr(self, "_last_prompt_tokens", None),
            completion_tokens=getattr(self, "_last_completion_tokens", None),
        )

    @staticmethod
    def _citable(package: EvidencePackage) -> dict[str, float]:
        """Every number a narrator may state, including structural counts."""
        values = dict(package.citable_values())
        values.update(
            {
                "event_window_days": float(package.event_window.days),
                "baseline_window_days": float(package.baseline_window.days),
                "baseline_n": float(package.statistical_test.baseline_n),
                "event_n": float(package.statistical_test.event_n),
                "hypotheses_in_pool": float(package.fdr.hypotheses_in_pool),
                "alpha": package.fdr.alpha,
                "event_coverage": package.data_quality.event_window_coverage,
                "confidence_pct": package.confidence * 100.0,
            }
        )
        return values


class DeterministicProvider(NarrativeProvider):
    """Persona templates rendered from the evidence package.

    Every figure interpolated here comes from the package, so the verifier is
    satisfied by construction. It still runs -- a template can drift, and the
    check is cheap.
    """

    name = "deterministic"

    _FRAMING = {
        "CFO_EXECUTIVE": "Financial impact and margin consequence",
        "VP_GROWTH": "Demand and channel read",
        "VP_OPERATIONS": "Cost, freight and fulfilment read",
        "DATA_ANALYST": "Method, evidence and caveats",
    }

    def _render(self, package: EvidencePackage, persona: str) -> str:
        if package.decision is Decision.ABSTAIN:
            return self._abstention(package)

        change = package.observed_change
        movement = change.describe()
        window = f"{package.event_window.start} to {package.event_window.end}"

        opening = (
            f"{package.label} for {package.entity} moved {movement} over {window}, "
            f"against a baseline of {package.baseline_window.days} days."
        )

        if change.change_type.value == "NEW_ACTIVITY":
            opening = (
                f"{package.label} for {package.entity} began trading in this window, "
                f"contributing {change.absolute_change:,.2f} against no prior-period "
                f"history. No growth rate is quoted because none is defined."
            )

        if package.statistical_test.tested:
            evidence = (
                f"The movement is significant after {package.fdr.method} correction across "
                f"{package.fdr.hypotheses_in_pool} simultaneous hypotheses "
                f"(adjusted p = {package.fdr.adjusted_p_value:.2e}), with an effect size of "
                f"{package.statistical_test.effect_size:+.3f} over "
                f"{package.statistical_test.event_n} observed days."
            )
        else:
            evidence = (
                "No distributional test was possible here, so this rests on business "
                "materiality alone and carries no significance claim."
            )

        materiality = (
            f"It clears the declared materiality bar by {package.materiality.exceedance:.1f}x."
        )

        caveats = []
        if package.contradicting_evidence:
            caveats.append(package.contradicting_evidence[0])
        if package.data_quality.stale_sources:
            caveats.append(
                f"Sources past their freshness SLA: {', '.join(package.data_quality.stale_sources)}."
            )
        if not package.windows_equal_length:
            # Two different situations, and saying "scaled by 1.0000" for the
            # second reads as a contradiction.
            if package.baseline_scale != 1.0:
                caveats.append(
                    f"Windows differ in length; the baseline was scaled by "
                    f"{package.baseline_scale:.4f} to compare like for like."
                )
            else:
                caveats.append(
                    "Windows differ in length and totals are compared as reported, "
                    "so part of this movement reflects the difference in window size."
                )

        confidence = (
            f"Evidence confidence {package.confidence:.2f} on a governance rubric "
            f"(not a calibrated probability). Decision: {package.decision.value}."
        )

        lens = self._FRAMING.get(persona, "Analysis")
        parts = [f"[{lens}]", opening, materiality, evidence]
        if caveats:
            parts.append("Caveats: " + " ".join(caveats))
        parts.append(confidence)
        return " ".join(parts)

    @staticmethod
    def _abstention(package: EvidencePackage) -> str:
        reasons = package.contradicting_evidence[:2]
        unblock = package.unblock_instructions[:2]
        text = (
            f"PulseBI is abstaining on {package.label} for {package.entity} over "
            f"{package.event_window.start} to {package.event_window.end}. "
            f"{package.decision_reason} "
        )
        if reasons:
            text += "Evidence problems: " + " ".join(reasons) + " "
        if unblock:
            text += "To unblock: " + " ".join(unblock)
        return text.strip()


class OllamaProvider(NarrativeProvider):
    """Local model via Ollama. Free, offline, and genuinely measurable.

    The prompt carries the evidence package and an explicit prohibition on
    computing anything. That prohibition is not trusted -- the verifier enforces
    it -- but stating it reduces how often the check has to fire.
    """

    name = "ollama"

    def __init__(self, model: str = OLLAMA_MODEL, url: str = OLLAMA_URL) -> None:
        self.model = model
        self.url = url
        self._last_prompt_tokens: int | None = None
        self._last_completion_tokens: int | None = None

    def build_prompt(self, package: EvidencePackage, persona: str) -> str:
        """Overrides the base prompt with an explicit prohibition on computing."""
        citable = "\n".join(
            f"  {name} = {value:,.6g}" for name, value in sorted(self._citable(package).items())
        )
        rules = "\n".join(f"  - {rule}" for rule in package.narration_rules)
        return (
            f"You are writing one short paragraph for a {persona}.\n\n"
            f"RULES (violating any of these invalidates the output):\n{rules}\n"
            f"  - Use ONLY the numbers listed below. Do not derive new ones.\n\n"
            f"DECISION: {package.decision.value}\n"
            f"REASON: {package.decision_reason}\n"
            f"KPI: {package.label} ({package.kpi}) for entity {package.entity}\n"
            f"CHANGE TYPE: {package.observed_change.change_type.value}\n"
            f"WINDOW: {package.event_window.start} to {package.event_window.end}\n\n"
            f"NUMBERS YOU MAY CITE:\n{citable}\n\n"
            f"Write the paragraph now. No preamble, no bullet points."
        )

    def _render(self, package: EvidencePackage, persona: str) -> str:
        import httpx

        prompt = self.build_prompt(package, persona)
        response = httpx.post(
            f"{self.url}/api/generate",
            json={"model": self.model, "prompt": prompt, "stream": False},
            timeout=120.0,
        )
        response.raise_for_status()
        payload = response.json()

        # Real counts from the runtime, not an estimate.
        self._last_prompt_tokens = payload.get("prompt_eval_count")
        self._last_completion_tokens = payload.get("eval_count")
        return payload["response"].strip()


def get_provider(name: str | None = None) -> NarrativeProvider:
    """Resolve the configured provider, defaulting to the offline one."""
    choice = (name or os.environ.get("PULSEBI_NARRATIVE", "deterministic")).lower()
    if choice == "ollama":
        return OllamaProvider()
    return DeterministicProvider()
