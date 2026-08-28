"""REST API over the detection pipeline.

Windows are always explicit query parameters. There is deliberately no "analyse
the current period" convenience endpoint, because every window-leakage bug this
engine was rebuilt to fix started with an implicit default somewhere.

The dashboard is served from the same process as a single static file, so a demo
needs one command and no build step.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.contracts import get_contract_store
from app.engines.cold_start import ColdStartEngine
from app.engines.decomposition import decompose_revenue
from app.engines.margin_bridge import build_margin_bridge, freight_rate_change
from app.engines.detector import BaselineMode, Decision
from app.engines.reconciler import DataReconciler
from app.feedback import FeedbackStore, Rating
from app.governance.rbac import RBACManager
from app.narrative.actions import ActionRecommender
from app.narrative.provider import Narrative, get_provider
from app.narrative.verifier import UnfaithfulNarrative
from app.pipeline import DetectionPipeline
from app.telemetry import TelemetryTracker

# The frontend is a sibling of backend/, not buried inside it: it is a
# first-class part of the project and a reviewer should find it without
# hunting. Served from this process so the demo stays a single command.
FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

app = FastAPI(
    title="PulseBI",
    description="Governed KPI intelligence-to-action engine",
    version="3.0.0",
)

# Explicit origins rather than a wildcard. Browsers reject wildcard-plus-
# credentials anyway, and the previous prototype combined the two.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "PULSEBI_ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
    ).split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Persona"],
)

store = get_contract_store()
reconciler = DataReconciler(store)
pipeline = DetectionPipeline(reconciler=reconciler, store=store)
rbac = RBACManager(store)
recommender = ActionRecommender(store)
cold_start = ColdStartEngine(reconciler)
feedback_store = FeedbackStore(store)


def _parse_window(start: str, end: str, label: str) -> tuple[date, date]:
    try:
        parsed = (date.fromisoformat(start), date.fromisoformat(end))
    except ValueError as exc:
        raise HTTPException(400, f"Invalid {label} window: {exc}") from exc
    if parsed[1] <= parsed[0]:
        raise HTTPException(400, f"{label} window must be non-empty and forward in time.")
    return parsed


def _require_persona(persona: str) -> str:
    if persona not in store.persona_names:
        raise HTTPException(403, f"Unknown persona '{persona}'.")
    return persona


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "contract_version": store.contract.version,
        "kpis": sorted(store.kpi_names),
        "personas": sorted(store.persona_names),
        "fdr_method": store.detection.fdr_method.value,
        "narrative_provider": get_provider().name,
    }


@app.get("/api/contract")
def contract() -> dict:
    """The governed semantic contract, so the UI renders from it rather than
    from its own copy of the metric definitions."""
    return {
        "kpis": {
            name: {
                "label": kpi.label,
                "unit": kpi.unit,
                "grain": kpi.grain,
                "formula": kpi.formula,
                "sources": kpi.sources,
                "relationship": kpi.metric_tree.relationship.value,
                "materiality": kpi.materiality.model_dump(),
                "lineage": store.lineage_for(name),
            }
            for name, kpi in store.contract.kpis.items()
        },
        "personas": {
            name: {"title": p.title, "levers": p.levers}
            for name, p in store.contract.personas.items()
        },
        "detection": store.detection.model_dump(mode="json"),
        "confidence": store.confidence.model_dump(mode="json"),
    }


@app.get("/api/scenarios")
def scenarios() -> dict:
    """Pre-set windows that exercise each required capability."""
    return {
        "scenarios": [
            {
                "id": "multi_factor",
                "name": "Multi-factor movement",
                "description": "Revenue up sharply while gross margin falls. Five KPIs, "
                "three sources, three grains.",
                "baseline": ["2023-10-01", "2023-11-01"],
                "event": ["2023-11-01", "2023-12-01"],
                "baseline_mode": "AS_REPORTED",
            },
            {
                "id": "abstention",
                "name": "Attribution outage",
                "description": "A webhook failure drops 85% of tags for one week. The "
                "engine abstains where evidence is insufficient.",
                "baseline": ["2023-10-01", "2023-11-01"],
                "event": ["2023-11-01", "2023-11-08"],
                "baseline_mode": "MATCHED_LENGTH",
            },
            {
                "id": "cold_start",
                "name": "New product launch",
                "description": "A SKU with no prior history. No growth rate is invented; "
                "shrinkage borrows strength from siblings.",
                "baseline": ["2023-10-01", "2023-11-01"],
                "event": ["2023-11-01", "2023-12-01"],
                "baseline_mode": "AS_REPORTED",
            },
            {
                "id": "entitlement",
                "name": "Role-based masking",
                "description": "The same analysis seen by four personas, with values "
                "masked rather than blocked.",
                "baseline": ["2023-10-01", "2023-11-01"],
                "event": ["2023-11-01", "2023-12-01"],
                "baseline_mode": "AS_REPORTED",
            },
        ]
    }


class FindingResponse(BaseModel):
    evidence: dict
    access: dict
    narrative: str | None
    narrative_verified: bool
    numerals_checked: int
    action: dict | None
    feedback: dict | None


@app.get("/api/analyse")
def analyse(
    persona: str = Query(..., description="Persona requesting the analysis"),
    baseline_start: str = Query(...),
    baseline_end: str = Query(...),
    event_start: str = Query(...),
    event_end: str = Query(...),
    baseline_mode: str = Query("MATCHED_LENGTH"),
    limit: int = Query(12, ge=1, le=100),
) -> dict:
    """Run the pipeline and return findings as this persona may see them."""
    _require_persona(persona)
    baseline = _parse_window(baseline_start, baseline_end, "baseline")
    event = _parse_window(event_start, event_end, "event")

    try:
        mode = BaselineMode(baseline_mode)
    except ValueError:
        raise HTTPException(400, f"Unknown baseline_mode '{baseline_mode}'.") from None

    tracker = TelemetryTracker()
    provider = get_provider()

    with tracker.stage("detect_and_reconcile", tier="deterministic_sql"):
        result = pipeline.analyse(baseline, event, baseline_mode=mode)

    with tracker.stage("access_control", tier="business_rule"):
        visible, audit = rbac.filter_all(result.packages, persona)

    # Analyst feedback reorders findings within the bound the contract
    # declares. It never touches a computed value -- only emphasis moves.
    with tracker.stage("feedback_reranking", tier="business_rule"):
        visible.sort(
            key=lambda m: (
                m.package.decision.is_reportable,
                m.package.confidence
                * feedback_store.multiplier_for(m.package.kpi, m.package.entity),
            ),
            reverse=True,
        )

    findings: list[FindingResponse] = []
    narrative_tier = "model" if provider.name == "ollama" else "template_render"

    with tracker.stage("narrative", tier=narrative_tier):
        for masked in visible[:limit]:
            narrative: Narrative | None = None
            try:
                narrative = provider.generate(masked.package, persona)
            except UnfaithfulNarrative:
                # Fail closed: an unverifiable sentence is never returned.
                narrative = None

            if narrative and narrative.prompt_tokens is not None:
                tracker.record_model_call(
                    model=narrative.model or "unknown",
                    prompt_tokens=narrative.prompt_tokens,
                    completion_tokens=narrative.completion_tokens,
                )
            else:
                # No model was called, so report what one would have cost --
                # computed over the real prompt string and labelled a projection.
                tracker.record_projected_prompt(provider.build_prompt(masked.package, persona))

            action = recommender.recommend(masked.package, persona)
            findings.append(
                FindingResponse(
                    evidence=masked.package.model_dump(mode="json"),
                    access=masked.access.model_dump(mode="json"),
                    narrative=narrative.text if narrative else None,
                    narrative_verified=bool(narrative and narrative.verification.ok),
                    numerals_checked=narrative.verification.numerals_found if narrative else 0,
                    action=action.model_dump(mode="json") if action else None,
                    feedback=(
                        adjustment.model_dump(mode="json")
                        | {"describe": adjustment.describe()}
                        if (
                            adjustment := feedback_store.adjustments().get(
                                f"{masked.package.kpi}/{masked.package.entity}"
                            )
                        )
                        else None
                    ),
                )
            )

    counts = result.summary()
    return {
        "persona": persona,
        "baseline_window": [baseline[0].isoformat(), baseline[1].isoformat()],
        "event_window": [event[0].isoformat(), event[1].isoformat()],
        "baseline_mode": mode.value,
        "summary": counts,
        "detection": {
            "candidates_evaluated": result.detection.candidates_evaluated,
            "hypotheses_tested": result.detection.fdr.m_tested,
            "excluded_from_pool": result.detection.fdr.n_excluded,
            "fdr_method": result.detection.fdr.method.value,
            "dependence_assumption": result.detection.fdr.dependence_assumption,
            "alpha": result.detection.fdr.alpha,
            "raw_significant": result.detection.fdr.n_raw_significant,
            "significant_after_correction": result.detection.fdr.n_significant,
            "overturned_by_correction": result.detection.fdr.changed_by_correction,
        },
        "reconciliation": {
            "status": result.contradictions.status.value,
            "evidence_coverage": result.contradictions.evidence_coverage,
            "checks": [c.model_dump(mode="json") for c in result.contradictions.checks],
        },
        "freshness": [f.to_dict() for f in reconciler.freshness()],
        "access_audit": [a.model_dump(mode="json") for a in audit],
        "findings": [f.model_dump(mode="json") for f in findings],
        "telemetry": tracker.report().model_dump(mode="json"),
    }


@app.get("/api/cold-start")
def cold_start_candidates() -> dict:
    """Entities with little enough history that shrinkage does real work.

    Discovered from the data rather than named, so the dashboard never hardcodes
    a product id -- the same rule the algorithm code follows.
    """
    from app.engines.cold_start import COLD_START_DAY_LABEL_THRESHOLD

    candidates = []
    for entity in reconciler.products():
        days = len(reconciler.daily_units_series(entity))
        if 0 < days < COLD_START_DAY_LABEL_THRESHOLD:
            candidates.append({"entity": entity, "days_observed": days})
    candidates.sort(key=lambda c: c["days_observed"])
    return {"threshold_days": COLD_START_DAY_LABEL_THRESHOLD, "candidates": candidates}


@app.get("/api/cold-start/{entity}")
def cold_start_estimate(entity: str) -> dict:
    """Empirical-Bayes estimate and the shrinkage curve behind it."""
    try:
        estimate = cold_start.estimate_daily_units(entity)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "estimate": estimate.model_dump(mode="json"),
        "shrinkage_curve": cold_start.shrinkage_curve(entity),
    }


@app.get("/api/lineage/{kpi}")
def lineage(kpi: str) -> dict:
    try:
        contract_kpi = store.kpi(kpi)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "kpi": kpi,
        "label": contract_kpi.label,
        "formula": contract_kpi.formula,
        "grain": contract_kpi.grain,
        "sources": [
            {"name": s, **store.source(s).model_dump(mode="json")} for s in contract_kpi.sources
        ],
        "lineage": store.lineage_for(kpi),
        "relationship": contract_kpi.metric_tree.relationship.value,
    }


class FeedbackRequest(BaseModel):
    persona: str
    kpi: str
    entity: str
    rating: Rating
    comment: str = ""


@app.post("/api/feedback")
def submit_feedback(req: FeedbackRequest) -> dict:
    """Record an analyst vote. Adjusts ranking only, within the contract bound."""
    _require_persona(req.persona)
    try:
        entry = feedback_store.record(
            persona=req.persona,
            kpi=req.kpi,
            entity=req.entity,
            rating=req.rating,
            comment=req.comment,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    adjustment = feedback_store.adjustments()[entry.target]
    return {
        "recorded": entry.model_dump(mode="json"),
        "adjustment": adjustment.model_dump(mode="json") | {"describe": adjustment.describe()},
        "guarantee": (
            "Feedback reorders findings within the contract's declared bound. "
            "No computed value is altered."
        ),
    }


@app.get("/api/feedback")
def list_feedback() -> dict:
    adjustments = feedback_store.adjustments()
    return {
        "entries": [e.model_dump(mode="json") for e in feedback_store.entries()],
        "adjustments": {
            k: v.model_dump(mode="json") | {"describe": v.describe()}
            for k, v in adjustments.items()
        },
        "bound": feedback_store.bound,
    }


@app.delete("/api/feedback")
def clear_feedback() -> dict:
    feedback_store.clear()
    return {"cleared": True}


@app.get("/api/decomposition")
def decomposition(
    prior_period: str = Query("2023-10", description="YYYY-MM"),
    current_period: str = Query("2023-11", description="YYYY-MM"),
) -> dict:
    """Revenue PVM waterfall and the Shapley margin bridge.

    Two decompositions, because the KPIs need different treatment: revenue is
    additive and takes a price/volume/mix bridge with explicit new-product and
    discontinued terms; gross margin is a ratio and does not decompose
    additively at all, so it takes a Shapley decomposition whose efficiency
    axiom guarantees the parts sum to the whole.
    """
    try:
        prior = reconciler.period_summary(prior_period)
        current = reconciler.period_summary(current_period)
    except Exception as exc:
        raise HTTPException(400, f"Could not summarise periods: {exc}") from exc

    pvm = decompose_revenue(prior, current)
    bridge = build_margin_bridge(prior, current)

    return {
        "periods": {"prior": prior_period, "current": current_period},
        "revenue_waterfall": {
            "prior": pvm.prior_revenue,
            "current": pvm.current_revenue,
            "total_variance": pvm.total_variance,
            "terms": [t.model_dump(mode="json") for t in pvm.ranked_terms()],
            "residual": pvm.residual,
            "closes": pvm.closes,
            "entering_products": pvm.entering_products,
            "exiting_products": pvm.exiting_products,
            "method": "controller_akademie_pvm",
        },
        "margin_bridge": {
            "prior_pct": bridge.prior_margin_pct,
            "current_pct": bridge.current_margin_pct,
            "delta_pp": bridge.delta_pp,
            "contributions": [c.model_dump(mode="json") for c in bridge.ranked()],
            "residual_pp": bridge.residual_pp,
            "closes": bridge.closes,
            "method": bridge.method,
            "primary_driver": bridge.primary_driver().model_dump(mode="json"),
            "note": (
                "Gross margin is a ratio and is not additively decomposable. "
                "Shapley efficiency guarantees closure; order independence removes "
                "the substitution bias a sequential bridge would carry."
            ),
        },
        "freight_rate_change_pct": freight_rate_change(prior, current),
    }


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    index = FRONTEND_DIR / "index.html"
    if not index.exists():
        raise HTTPException(
            404, f"Frontend not found at {index}. Expected pulsebi/frontend/index.html."
        )
    return FileResponse(index)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
