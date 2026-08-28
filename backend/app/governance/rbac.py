"""Role-based access, applied as masking rather than refusal.

The previous prototype returned a blanket error to VP Operations. That is a
worse answer than it looks: the brief asks for row-, column- and domain-level
security *with differentiated narratives*, and a wall produces no narrative at
all. Graceful degradation is both the better demo and the more realistic
control -- an operations lead should see that margin moved without seeing
supplier COGS.

Three levels, all declared per KPI per persona in the contract:

    allow  full value visible
    mask   movement and direction visible, absolute values redacted
    deny   the KPI is not returned at all

Every response carries an audit trail naming each decision, which is the
auditability evidence the brief asks for.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.contracts import AccessLevel, ContractStore, get_contract_store
from app.evidence_package import EvidencePackage

REDACTED = "REDACTED"


class AccessDecision(BaseModel):
    kpi: str
    entity: str
    persona: str
    level: AccessLevel
    masked_fields: list[str] = []
    reason: str


class MaskedPackage(BaseModel):
    """An evidence package with restricted fields removed, plus its audit record."""

    package: EvidencePackage
    access: AccessDecision

    @property
    def is_masked(self) -> bool:
        return self.access.level is AccessLevel.MASK


# Absolute monetary values. Under a mask these are removed while the movement's
# direction, significance and confidence remain visible.
_SENSITIVE_FIELDS = ("baseline_value", "current_value")


class RBACManager:
    def __init__(self, store: ContractStore | None = None) -> None:
        self.store = store or get_contract_store()

    def level_for(self, kpi: str, persona: str) -> AccessLevel:
        return self.store.access_for(kpi, persona)

    def apply(self, package: EvidencePackage, persona: str) -> MaskedPackage | None:
        """Return the package as this persona may see it, or None if denied."""
        level = self.level_for(package.kpi, persona)

        if level is AccessLevel.DENY:
            return None

        if level is AccessLevel.ALLOW:
            return MaskedPackage(
                package=package,
                access=AccessDecision(
                    kpi=package.kpi,
                    entity=package.entity,
                    persona=persona,
                    level=level,
                    reason=f"{persona} has full visibility of {package.kpi}.",
                ),
            )

        masked = package.model_copy(deep=True)
        redacted: list[str] = []
        for field in _SENSITIVE_FIELDS:
            if getattr(masked, field) is not None:
                setattr(masked, field, None)
                redacted.append(field)

        # The absolute movement is a monetary quantity too; the relative change
        # and the direction are what survive a mask.
        if masked.observed_change.absolute_change is not None:
            masked.observed_change = masked.observed_change.model_copy(
                update={"absolute_change": None}
            )
            redacted.append("observed_change.absolute_change")

        masked.supporting_evidence = []
        masked.drivers = []

        return MaskedPackage(
            package=masked,
            access=AccessDecision(
                kpi=package.kpi,
                entity=package.entity,
                persona=persona,
                level=level,
                masked_fields=redacted,
                reason=(
                    f"{persona} may see that {package.kpi} moved and how confident the "
                    f"engine is, but not its absolute values."
                ),
            ),
        )

    def filter_all(
        self, packages: list[EvidencePackage], persona: str
    ) -> tuple[list[MaskedPackage], list[AccessDecision]]:
        """Apply access rules across a result set, recording every decision."""
        visible: list[MaskedPackage] = []
        audit: list[AccessDecision] = []

        for package in packages:
            result = self.apply(package, persona)
            if result is None:
                audit.append(
                    AccessDecision(
                        kpi=package.kpi,
                        entity=package.entity,
                        persona=persona,
                        level=AccessLevel.DENY,
                        reason=f"{persona} is not entitled to {package.kpi}.",
                    )
                )
                continue
            visible.append(result)
            audit.append(result.access)

        return visible, audit
