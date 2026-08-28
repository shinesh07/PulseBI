"""Evaluation harness.

Each benchmark checks a property the system claims, on evidence the run itself
produces. Nothing here asserts a rehearsed constant that the seed generator was
tuned to emit -- that would only prove the demo reproduces itself.

Run:  python run_evals.py
"""

from __future__ import annotations

import math
import sys
from datetime import date

import duckdb

from app import fdr as fdr_mod
from app.contracts import get_contract_store
from app.data.wide_scenario import EffectClass, build_catalogue, generate
from app.engines.detector import BaselineMode, Decision, MovementDetector
from app.governance.rbac import RBACManager
from app.narrative.provider import DeterministicProvider
from app.narrative.verifier import UnfaithfulNarrative, verify
from app.pipeline import DetectionPipeline
from app.series import DuckDBSeriesProvider, SeriesKey

BASELINE = (date(2023, 10, 1), date(2023, 11, 1))
EVENT = (date(2023, 11, 1), date(2023, 12, 1))
OUTAGE = (date(2023, 11, 1), date(2023, 11, 8))

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"   PASS  {name}")
        if detail:
            print(f"         {detail}")
    else:
        FAILED.append((name, detail))
        print(f"   FAIL  {name}")
        if detail:
            print(f"         {detail}")


def banner(n: int, title: str) -> None:
    print(f"\n[{n}] {title}\n" + "-" * 74)


store = get_contract_store()
pipeline = DetectionPipeline(store=store)
provider = DeterministicProvider()
rbac = RBACManager(store)

print("=" * 74)
print(" PULSEBI EVALUATION HARNESS")
print("=" * 74)

# ---------------------------------------------------------------------------
banner(1, "Contract governs the engine")
analysis = pipeline.analyse(BASELINE, EVENT, baseline_mode=BaselineMode.AS_REPORTED)

check(
    "Every KPI the engine reports is declared in the contract",
    {p.kpi for p in analysis.packages} <= store.kpi_names,
    f"reported: {sorted({p.kpi for p in analysis.packages})}",
)
check(
    "Three or more sources at three distinct grains",
    len({s.grain for s in store.contract.sources.values()}) >= 3,
    f"grains: {sorted({s.grain for s in store.contract.sources.values()})}",
)
check(
    "Unknown personas fail closed",
    all(store.access_for(k, "INTRUDER").value == "deny" for k in store.kpi_names),
)

# ---------------------------------------------------------------------------
banner(2, "No fabricated numbers reach business-facing output")
bad: list[str] = []
for window in (EVENT, OUTAGE, (date(2025, 1, 1), date(2025, 2, 1))):
    for package in pipeline.analyse(BASELINE, window).packages:
        package.assert_llm_safe()
        for name, value in package.citable_values().items():
            if not math.isfinite(value):
                bad.append(f"{package.kpi}.{name}={value}")
        if not 0.0 <= package.confidence <= 1.0:
            bad.append(f"{package.kpi}.confidence={package.confidence}")

check("No NaN or Infinity across three windows including an empty one", not bad, "; ".join(bad[:3]))

new_activity = [p for p in analysis.packages if p.observed_change.change_type.value == "NEW_ACTIVITY"]
check(
    "A launch from a zero baseline gets no invented growth rate",
    bool(new_activity) and all(p.observed_change.relative_change_pct is None for p in new_activity),
    f"{len(new_activity)} new-activity findings, all with relative change undefined",
)

# ---------------------------------------------------------------------------
banner(3, "Every narrative numeral resolves to a computed fact")
total_numerals = 0
unverified = 0
for package in analysis.packages:
    try:
        narrative = provider.generate(package, "CFO_EXECUTIVE")
        total_numerals += narrative.verification.numerals_found
    except UnfaithfulNarrative:
        unverified += 1

check(
    "All narratives pass faithfulness verification",
    unverified == 0,
    f"{total_numerals} numerals checked across {len(analysis.packages)} findings",
)

fabricated = verify(
    "Revenue rose by $999,999.99 and margin improved 12.5%.",
    analysis.packages[0].citable_values(),
)
check(
    "A fabricated figure is caught rather than published",
    not fabricated.ok and len(fabricated.unresolved) >= 1,
    f"rejected: {[n.text for n in fabricated.unresolved]}",
)

# ---------------------------------------------------------------------------
banner(4, "Multiplicity correction is load-bearing")
catalogue = build_catalogue()
pos, erp = generate(catalogue)
conn = duckdb.connect(":memory:")
conn.register("_p", pos)
conn.register("_e", erp)
conn.execute("CREATE TABLE pos_orders AS SELECT * FROM _p")
conn.execute("CREATE TABLE erp_financials AS SELECT * FROM _e")
conn.execute(
    "CREATE TABLE marketing_spend(week_start VARCHAR, channel VARCHAR, spend DOUBLE, "
    "impressions BIGINT, clicks BIGINT, new_customers BIGINT)"
)
wide = MovementDetector(DuckDBSeriesProvider(conn, store), store).detect(
    BASELINE, EVENT, keys=[SeriesKey("revenue", "product", s.product_id) for s in catalogue]
)
pool = {h.key: h.test.p_value for h in wide.hypotheses if h.test.tested}
alpha = store.detection.fdr_alpha
truth = {s.product_id: s.effect_class for s in catalogue}
bh = fdr_mod.correct(pool, alpha=alpha, method=fdr_mod.FDRMethod.BENJAMINI_HOCHBERG)
by = fdr_mod.correct(pool, alpha=alpha, method=fdr_mod.FDRMethod.BENJAMINI_YEKUTIELI)

check(
    "Enough simultaneous hypotheses for correction to matter",
    len(pool) >= 20,
    f"{len(pool)} tested",
)
check(
    "Correction overturns hypotheses a raw threshold would accept",
    len(bh.changed_by_correction) > 0,
    f"raw {bh.n_raw_significant} -> BH {bh.n_significant} -> BY {by.n_significant}",
)
check(
    "No genuine effect is discarded by the correction",
    all(truth[k.split("/")[-1]] is not EffectClass.TRUE_EFFECT for k in bh.changed_by_correction),
)
check(
    "Benjamini-Yekutieli is at least as conservative as Benjamini-Hochberg",
    by.n_significant <= bh.n_significant,
)
check(
    "Only validly tested hypotheses enter the pool",
    bh.m_tested == len(pool) and bh.m_tested == sum(1 for h in wide.hypotheses if h.test.tested),
)
check(
    "Every decision follows the adjusted p-value",
    all(
        (h.decision is not Decision.DETECTED) or h.significant_after_fdr
        for h in wide.hypotheses
    ),
)

# ---------------------------------------------------------------------------
banner(5, "Abstention is real and discriminating")
outage = pipeline.analyse(BASELINE, OUTAGE)
empty = pipeline.analyse(BASELINE, (date(2025, 1, 1), date(2025, 2, 1)))

check(
    "The engine answers on a degraded but sufficient window",
    len(analysis.detected) > 0,
    f"{len(analysis.detected)} detected over the full month",
)
check(
    "The engine abstains where evidence runs out",
    len(empty.abstained) > 0,
    f"{len(empty.abstained)} abstentions on a window with no data",
)
check(
    "Every abstention states why",
    all(p.decision_reason for p in outage.abstained + empty.abstained),
)
check(
    "Detection always rests on a completed, corrected test",
    all(p.statistical_test.tested and p.fdr.significant_after_fdr for p in analysis.detected),
)

# ---------------------------------------------------------------------------
banner(6, "Window isolation")
week = pipeline.analyse(BASELINE, OUTAGE)
month = pipeline.analyse(BASELINE, EVENT)
week_rev = next(p for p in week.packages if p.kpi == "revenue" and p.entity == "ALL")
month_rev = next(p for p in month.packages if p.kpi == "revenue" and p.entity == "ALL")

check(
    "A narrower window yields a different answer",
    week_rev.current_value != month_rev.current_value,
    f"7-day ${week_rev.current_value:,.0f} vs 30-day ${month_rev.current_value:,.0f}",
)
check(
    "Reconciliation is recomputed per window, not inherited",
    week.contradictions.window_end != month.contradictions.window_end,
)

# ---------------------------------------------------------------------------
banner(7, "Access control masks rather than blocks")
levels: dict[str, dict[str, int]] = {}
for persona in sorted(store.persona_names):
    _, audit = rbac.filter_all(analysis.packages, persona)
    counts: dict[str, int] = {}
    for entry in audit:
        counts[entry.level.value] = counts.get(entry.level.value, 0) + 1
    levels[persona] = counts

check(
    "At least one persona sees masked rather than blocked values",
    any(c.get("mask", 0) > 0 for c in levels.values()),
    "; ".join(f"{p}: {c}" for p, c in levels.items()),
)
leaks = []
for persona in store.persona_names:
    visible, _ = rbac.filter_all(analysis.packages, persona)
    for masked in visible:
        if masked.is_masked and masked.package.current_value is not None:
            leaks.append(f"{persona}/{masked.package.kpi}")
check("Masked findings expose no absolute values", not leaks, "; ".join(leaks[:3]))

# ---------------------------------------------------------------------------
banner(8, "Telemetry reports only what it measured")
from app.telemetry import TelemetryTracker  # noqa: E402

tracker = TelemetryTracker()
with tracker.stage("compute", tier="deterministic_sql"):
    pipeline.analyse(BASELINE, EVENT)
tracker.record_projected_prompt(provider.build_prompt(analysis.packages[0], "CFO_EXECUTIVE"))
report = tracker.report()

check(
    "Offline runs report zero model calls and zero cost",
    report.llm.model_calls == 0 and report.llm.cost_usd == 0.0,
    f"mode={report.llm.mode}",
)
check(
    "Token figures are labelled a projection, not a measurement",
    report.llm.measured is False and report.projected_llm is not None,
    f"projection: {report.projected_llm.prompt_tokens_estimate} tokens, "
    f"{report.projected_llm.estimation_method}",
)
check(
    "The compute split derives from measured stage timings",
    report.total_ms > 0 and 0.0 <= report.deterministic_share <= 1.0,
    f"{report.total_ms:.1f} ms, {report.deterministic_share:.1%} deterministic",
)

# ---------------------------------------------------------------------------
print("\n" + "=" * 74)
if FAILED:
    print(f" {len(PASSED)} PASSED, {len(FAILED)} FAILED")
    for name, detail in FAILED:
        print(f"   FAILED: {name} {detail}")
else:
    print(f" ALL {len(PASSED)} BENCHMARKS PASSED")
print("=" * 74)
sys.exit(1 if FAILED else 0)
