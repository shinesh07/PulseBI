"""Analyst feedback that changes ranking, and nothing else.

Requirement 7 of the brief is that the system learns from analyst and
business-user feedback. The prototype this replaces satisfied it cosmetically:
feedback appended a string to a narrative and changed no behaviour.

The hard constraint is that feedback must never touch arithmetic. A computed
value is a computed value; an analyst's opinion that a driver is over-weighted
is a statement about *relevance*, not about the number. So feedback adjusts
**presentation ranking** within a bound declared in the contract, and the
adjustment is visible on every finding it touches.

Deliberately not a learned model. The principled version is preference learning
or a contextual bandit over analyst judgements, which needs far more feedback
than a prototype will ever see and would be unfalsifiable at this scale. A
bounded, auditable weight store is the honest approximation, and it is described
as one rather than dressed up as learning.
"""

from __future__ import annotations

import time
from collections import defaultdict
from enum import Enum

from pydantic import BaseModel, field_validator

from app.contracts import ContractStore, get_contract_store


class Rating(str, Enum):
    UPVOTE = "UPVOTE"
    DOWNVOTE = "DOWNVOTE"

    @property
    def direction(self) -> float:
        return 1.0 if self is Rating.UPVOTE else -1.0


class FeedbackEntry(BaseModel):
    id: str
    timestamp: str
    persona: str
    kpi: str
    entity: str
    rating: Rating
    comment: str = ""

    @property
    def target(self) -> str:
        return f"{self.kpi}/{self.entity}"


class RankAdjustment(BaseModel):
    target: str
    multiplier: float
    votes: int
    upvotes: int
    downvotes: int
    bound: float

    @field_validator("multiplier")
    @classmethod
    def _within_unit_range(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("A rank multiplier must be positive.")
        return v

    def describe(self) -> str:
        shift = (self.multiplier - 1.0) * 100.0
        return (
            f"Ranking adjusted {shift:+.1f}% by {self.votes} analyst vote(s) "
            f"({self.upvotes} up, {self.downvotes} down); capped at "
            f"±{self.bound * 100:.0f}%."
        )


class FeedbackStore:
    """In-process store of analyst votes and the bounded ranking effect they have.

    In-process on purpose: persisting it would imply the adjustments survive and
    compound across sessions, which is a claim about learning this design does
    not make.
    """

    def __init__(self, store: ContractStore | None = None) -> None:
        self.store = store or get_contract_store()
        self._entries: list[FeedbackEntry] = []

    @property
    def bound(self) -> float:
        return self.store.feedback.max_rank_weight_delta

    def record(
        self,
        *,
        persona: str,
        kpi: str,
        entity: str,
        rating: Rating,
        comment: str = "",
    ) -> FeedbackEntry:
        if persona not in self.store.persona_names:
            raise ValueError(f"Unknown persona '{persona}'.")
        if kpi not in self.store.kpi_names:
            raise ValueError(f"Unknown KPI '{kpi}'.")

        entry = FeedbackEntry(
            id=f"FB-{len(self._entries) + 1:04d}",
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            persona=persona,
            kpi=kpi,
            entity=entity,
            rating=rating,
            comment=comment,
        )
        self._entries.append(entry)
        return entry

    def entries(self) -> list[FeedbackEntry]:
        return list(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def adjustments(self) -> dict[str, RankAdjustment]:
        """Per-target ranking multipliers, saturating at the declared bound.

        Net votes drive the multiplier through a saturating function rather than
        a linear one, so a brigade of votes on one finding cannot dominate the
        queue: the tenth downvote moves the ranking far less than the first.
        """
        tallies: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for entry in self._entries:
            index = 0 if entry.rating is Rating.UPVOTE else 1
            tallies[entry.target][index] += 1

        out: dict[str, RankAdjustment] = {}
        for target, (up, down) in tallies.items():
            net = up - down
            # net/(|net|+2) saturates toward 1 without ever reaching it.
            saturation = net / (abs(net) + 2.0) if net else 0.0
            out[target] = RankAdjustment(
                target=target,
                multiplier=1.0 + self.bound * saturation,
                votes=up + down,
                upvotes=up,
                downvotes=down,
                bound=self.bound,
            )
        return out

    def multiplier_for(self, kpi: str, entity: str) -> float:
        adjustment = self.adjustments().get(f"{kpi}/{entity}")
        return adjustment.multiplier if adjustment else 1.0

    def comments_for(self, kpi: str, entity: str) -> list[str]:
        return [
            f"[{e.timestamp[:10]} {e.persona}] {e.comment}"
            for e in self._entries
            if e.target == f"{kpi}/{entity}" and e.comment
        ]
