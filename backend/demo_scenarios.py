"""The five scenarios the hardening brief requires, run against real engines."""
from datetime import date
import duckdb
from app import fdr as fdr_mod
from app.contracts import get_contract_store
from app.data.wide_scenario import EffectClass, build_catalogue, generate
from app.engines.detector import BaselineMode, MovementDetector
from app.pipeline import DetectionPipeline
from app.series import DuckDBSeriesProvider, SeriesKey

BASE = (date(2023, 10, 1), date(2023, 11, 1))
FULL = (date(2023, 11, 1), date(2023, 12, 1))
p = DetectionPipeline()

def head(t): print(f"\n{'='*78}\n{t}\n{'='*78}")

head("SCENARIO A - Strong signal")
r = p.analyse(BASE, FULL, baseline_mode=BaselineMode.AS_REPORTED)
top = r.detected[0]
print(f"  {top.kpi}/{top.entity}: {top.observed_change.describe()}  [{top.observed_change.change_type.value}]")
print(f"  decision   : {top.decision.value}   confidence {top.confidence:.3f} ({top.confidence_scale})")
print(f"  materiality: {top.materiality.exceedance:.1f}x threshold via {top.materiality.gates_passed}")
print(f"  test       : {top.statistical_test.test} p={top.statistical_test.p_value:.2e} "
      f"effect={top.statistical_test.effect_size:+.3f} n={top.statistical_test.baseline_n}/{top.statistical_test.event_n}")
print(f"  fdr        : {top.fdr.method} adj_p={top.fdr.adjusted_p_value:.2e} sig={top.fdr.significant_after_fdr}")
print(f"  summary    : {r.summary()}")

head("SCENARIO B - Contradictory evidence")
print(f"  reconciliation status: {r.contradictions.status.value}")
for c in r.contradictions.checks:
    print(f"    [{c.status.value:<22}] {c.label}")
    print(f"        {c.detail[:100]}")
    print(f"        affects: {c.affected_kpis or 'all KPIs'}")
cac = next(x for x in r.packages if x.kpi == "blended_cac")
rev = next(x for x in r.packages if x.kpi == "revenue" and x.entity == "ALL")
print(f"\n  blended_cac (attribution-dependent): {cac.decision.value}, confidence {cac.confidence:.3f}")
print(f"  revenue     (attribution-independent): {rev.decision.value}, confidence {rev.confidence:.3f}")
print("  -> the attribution contradiction penalises only the KPI it bears on")

head("SCENARIO C - New product, zero baseline")
for pkg in [x for x in r.packages if x.observed_change.change_type.value == "NEW_ACTIVITY"][:3]:
    print(f"  {pkg.kpi}/{pkg.entity}")
    print(f"    baseline={pkg.baseline_value}  current={pkg.current_value:,.2f}")
    print(f"    type={pkg.observed_change.change_type.value}  relative={pkg.observed_change.relative_change_pct}  (no Infinity)")
    print(f"    decision={pkg.decision.value}  confidence={pkg.confidence:.3f}")
    print(f"    reason: {pkg.decision_reason[:90]}")

head("SCENARIO D - Multiple testing")
cat = build_catalogue(); pos, erp = generate(cat)
conn = duckdb.connect(":memory:"); conn.register("_p", pos); conn.register("_e", erp)
conn.execute("CREATE TABLE pos_orders AS SELECT * FROM _p")
conn.execute("CREATE TABLE erp_financials AS SELECT * FROM _e")
conn.execute("CREATE TABLE marketing_spend(week_start VARCHAR, channel VARCHAR, spend DOUBLE, impressions BIGINT, clicks BIGINT, new_customers BIGINT)")
store = get_contract_store()
res = MovementDetector(DuckDBSeriesProvider(conn, store), store).detect(
    BASE, FULL, keys=[SeriesKey("revenue", "product", s.product_id) for s in cat])
pool = {h.key: h.test.p_value for h in res.hypotheses if h.test.tested}
truth = {s.product_id: s.effect_class for s in cat}
a = store.detection.fdr_alpha
def cls(keys):
    out = {}
    for k in keys:
        lab = truth[k.split("/")[-1]].value
        out[lab] = out.get(lab, 0) + 1
    return out
print(f"  {len(pool)} hypotheses tested, alpha={a}")
for name, m in (("BH", fdr_mod.FDRMethod.BENJAMINI_HOCHBERG), ("BY", fdr_mod.FDRMethod.BENJAMINI_YEKUTIELI)):
    c = fdr_mod.correct(pool, alpha=a, method=m)
    raw = [k for k, v in c.corrected.items() if v.raw_p_value <= a]
    adj = [k for k, v in c.corrected.items() if v.significant]
    print(f"  {name}: raw rejects {len(raw)} {cls(raw)}")
    print(f"  {name}: adj rejects {len(adj)} {cls(adj)}  overturned={len(c.changed_by_correction)}")

head("SCENARIO E - Date-window isolation")
for label, win in (("1-7 Nov", (date(2023,11,1), date(2023,11,8))),
                   ("full Nov", FULL)):
    rr = p.analyse(BASE, win)
    rv = next(x for x in rr.packages if x.kpi == "revenue" and x.entity == "ALL")
    print(f"  {label:<10} days={rv.event_window.days:<3} current=${rv.current_value:>12,.2f} "
          f"scale={rv.baseline_scale:.4f} conf={rv.confidence:.3f} {rv.decision.value}")
print("  -> a window change alters the conclusion; data outside it does not (see test_window_isolation.py)")
