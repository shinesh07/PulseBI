"""Seven-step prescriptive action cards.

    driver -> controllable lever -> action -> expected impact
           -> owner -> confidence -> monitoring plan

Two properties distinguish this from the version it replaces, where impacts were
hardcoded strings:

* **Expected impact is computed**, from the movement the engine measured. A card
  that says "recovers $42,000" when nothing computed $42,000 is a fabricated
  number wearing a business frame, and the faithfulness verifier would reject any
  narrative quoting it.
* **Levers are gated on decision rights.** A persona is only offered actions it
  can actually authorise, drawn from the contract rather than a lookup table.

No card is emitted for an abstention. If the engine cannot say what happened, it
has no business recommending what to do about it.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.contracts import ContractStore, get_contract_store
from app.engines.detector import Decision
from app.evidence_package import EvidencePackage


class ActionCard(BaseModel):
    driver: str
    lever: str
    action: str
    expected_impact: str
    expected_impact_value: float | None
    owner: str
    confidence: float
    confidence_basis: str
    monitoring_plan: str
    requires_review: bool
    caveats: list[str] = []


# Owners by persona, from the contract's persona titles.
_MONITORING = {
    "usd": "Track this KPI daily against the baseline window and re-run the bridge weekly.",
    "pct": "Track the rate weekly and alert on any move beyond the declared materiality bar.",
}


class ActionRecommender:
    def __init__(self, store: ContractStore | None = None) -> None:
        self.store = store or get_contract_store()

    def recommend(self, package: EvidencePackage, persona: str) -> ActionCard | None:
        """One card for one finding, or None where no action is defensible."""
        if package.decision is Decision.ABSTAIN:
            return None
        if not package.decision.is_reportable:
            return None

        levers = self.store.levers_for(persona)
        if not levers:
            return None

        change = package.observed_change
        direction = change.direction
        magnitude = change.absolute_change

        # The lever is chosen by the persona's decision rights, not by matching
        # a driver name against a hardcoded playbook.
        lever = levers[0]

        driver = (
            f"{package.label} {direction} "
            f"({change.change_type.value.lower().replace('_', ' ')})"
        )

        if magnitude is None:
            impact_text = (
                "Not quantifiable from this window; size the opportunity once a "
                "comparable baseline exists."
            )
            impact_value = None
        elif package.unit == "usd":
            impact_text = (
                f"Addressing this movement of {abs(magnitude):,.2f} is the size of the "
                f"opportunity; recovery depends on how much of it the lever reaches."
            )
            impact_value = abs(magnitude)
        else:
            impact_text = (
                f"The movement is {abs(magnitude):,.2f} {package.unit}; the lever's reach "
                f"determines how much is recoverable."
            )
            impact_value = abs(magnitude)

        caveats: list[str] = []
        requires_review = package.decision is Decision.LOW_CONFIDENCE

        if not package.statistical_test.tested:
            caveats.append(
                "No statistical test backs this movement; treat the impact as indicative."
            )
            requires_review = True
        if package.contradicting_evidence:
            caveats.append(package.contradicting_evidence[0])
            requires_review = True
        if package.confidence < self.store.confidence.action_threshold:
            caveats.append(
                f"Confidence {package.confidence:.2f} is below the action threshold "
                f"{self.store.confidence.action_threshold}; review before committing spend."
            )
            requires_review = True

        return ActionCard(
            driver=driver,
            lever=lever,
            action=(
                f"Apply '{lever}' against {package.label} for {package.entity}, sized to the "
                f"movement measured over {package.event_window.days} days."
            ),
            expected_impact=impact_text,
            expected_impact_value=impact_value,
            owner=self.store.persona(persona).title,
            confidence=package.confidence,
            confidence_basis=package.confidence_scale,
            monitoring_plan=_MONITORING.get(
                package.unit, "Re-run this analysis on the next reporting cycle."
            ),
            requires_review=requires_review,
            caveats=caveats,
        )
