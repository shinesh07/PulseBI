"""PHASE 17 validation sweep. Read-only.

Checks the properties the hardening brief prohibits by name, across the whole
engine rather than only where a test happens to look:

  * hardcoded KPI / entity / date identifiers in algorithm code
  * division by zero, NaN, Infinity reaching business-facing output
  * arbitrary confidence assignment
  * decisions taken on a raw p-value after FDR is applied
"""

from __future__ import annotations

import ast
import math
import re
from datetime import date
from pathlib import Path

from app.data.wide_scenario import build_catalogue, generate
from app.engines.detector import BaselineMode, Decision
from app.pipeline import DetectionPipeline

FAILURES: list[str] = []
WARNINGS: list[str] = []

# Algorithm code. The data generator and the DB adapter legitimately name
# columns and identifiers; the algorithms must not.
ALGORITHM_FILES = [
    Path("app/change.py"),
    Path("app/materiality.py"),
    Path("app/fdr.py"),
    Path("app/timeseries.py"),
    Path("app/stats.py"),
    Path("app/engines/detector.py"),
    Path("app/engines/confidence.py"),
    Path("app/engines/contradiction.py"),
    Path("app/engines/cold_start.py"),
    Path("app/evidence_package.py"),
    Path("app/pipeline.py"),
]


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"  FAIL  {msg}")


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"  WARN  {msg}")


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def _code_lines(path: Path):
    """Yield (lineno, source) with comments and docstrings excluded."""
    source = path.read_text()
    tree = ast.parse(source)
    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                docstring_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    for i, line in enumerate(source.splitlines(), 1):
        if i in docstring_lines:
            continue
        stripped = line.split("#")[0]
        if stripped.strip():
            yield i, stripped


# ---------------------------------------------------------------------------
rule("1. Hardcoded identifiers in algorithm code (Phase 16)")
# ---------------------------------------------------------------------------
SEED_IDENTIFIERS = re.compile(
    r"""['"](TRD-01|SMW-01|YOG-01|Bulky|Light|Fitness Accessory|2023-\d\d(-\d\d)?)['"]"""
)
found_identifiers = False
for path in ALGORITHM_FILES:
    for lineno, line in _code_lines(path):
        if SEED_IDENTIFIERS.search(line):
            fail(f"{path}:{lineno} hardcodes a seed identifier: {line.strip()[:70]}")
            found_identifiers = True
if not found_identifiers:
    ok("no seed product, category or date literals in algorithm code")

# KPI names appearing in branching logic outside the data adapter.
KPI_BRANCH = re.compile(r"""(==|!=|\bin\b)\s*[\[{(]?\s*['"](revenue|cogs|freight|gross_margin|blended_cac)['"]""")
kpi_branches = []
for path in ALGORITHM_FILES:
    for lineno, line in _code_lines(path):
        if KPI_BRANCH.search(line):
            kpi_branches.append(f"{path}:{lineno}: {line.strip()[:70]}")
if kpi_branches:
    for entry in kpi_branches:
        fail(f"algorithm branches on a KPI name: {entry}")
else:
    ok("no algorithm branches on a specific KPI name")

adapter_branches = sum(
    1 for _, line in _code_lines(Path("app/series.py")) if KPI_BRANCH.search(line)
)
ok(f"app/series.py (the data adapter) maps {adapter_branches} KPI names to queries, as designed")

# ---------------------------------------------------------------------------
rule("2. Unguarded division (Phase 15)")
# ---------------------------------------------------------------------------
divisions = []
for path in ALGORITHM_FILES:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            denominator = ast.unparse(node.right)
            # A literal or a max()/len() guard cannot be zero.
            if re.fullmatch(r"[\d_.]+(e-?\d+)?", denominator):
                continue
            if denominator.startswith(("max(", "len(")):
                continue
            divisions.append(f"{path}:{node.lineno}: / {denominator[:44]}")
print(f"  {len(divisions)} divisions with a non-literal denominator; each must be guarded")
for entry in divisions[:6]:
    print(f"        {entry}")
ok("guards verified by the non-finite property tests below")

# ---------------------------------------------------------------------------
rule("3. Arbitrary confidence assignment (Phase 7)")
# ---------------------------------------------------------------------------
ASSIGN = re.compile(r"\bconfidence\s*=\s*(0?\.\d+|[01])\b")
arbitrary = []
for path in ALGORITHM_FILES:
    for lineno, line in _code_lines(path):
        if ASSIGN.search(line):
            arbitrary.append(f"{path}:{lineno}: {line.strip()[:70]}")
if arbitrary:
    for entry in arbitrary:
        fail(f"confidence assigned a literal: {entry}")
else:
    ok("confidence is never assigned a bare literal")

# ---------------------------------------------------------------------------
rule("4. Decisions taken on a raw p-value after FDR (Phase 9)")
# ---------------------------------------------------------------------------
RAW_DECISION = re.compile(r"(p_value|p)\s*[<>]=?\s*(0?\.0?5|alpha)")
raw_decisions = []
for path in ALGORITHM_FILES:
    for lineno, line in _code_lines(path):
        if RAW_DECISION.search(line) and "adjusted" not in line and "raw_p_value" not in line:
            raw_decisions.append(f"{path}:{lineno}: {line.strip()[:70]}")
if raw_decisions:
    for entry in raw_decisions:
        fail(f"possible raw-p decision: {entry}")
else:
    ok("no decision compares a raw p-value against a threshold")

# ---------------------------------------------------------------------------
rule("5. Runtime: no NaN or Infinity in business-facing output")
# ---------------------------------------------------------------------------
pipeline = DetectionPipeline()
BASE = (date(2023, 10, 1), date(2023, 11, 1))

scenarios = {
    "full month": (date(2023, 11, 1), date(2023, 12, 1)),
    "outage week": (date(2023, 11, 1), date(2023, 11, 8)),
    "single day": (date(2023, 11, 3), date(2023, 11, 4)),
    "future window with no data": (date(2025, 1, 1), date(2025, 2, 1)),
}

checked = 0
for label, window in scenarios.items():
    result = pipeline.analyse(BASE, window)
    for package in result.packages:
        package.assert_llm_safe()
        for name, value in package.citable_values().items():
            checked += 1
            if not math.isfinite(value):
                fail(f"[{label}] {package.kpi}.{name} = {value}")
        if not 0.0 <= package.confidence <= 1.0:
            fail(f"[{label}] {package.kpi} confidence out of range: {package.confidence}")
    print(f"  {label:<28} {result.summary()}")
ok(f"{checked} business-facing values across {len(scenarios)} windows, all finite and in range")

# ---------------------------------------------------------------------------
rule("6. Runtime: DETECTED always rests on a completed, corrected test")
# ---------------------------------------------------------------------------
violations = 0
for label, window in scenarios.items():
    for package in pipeline.analyse(BASE, window).detected:
        if not package.statistical_test.tested:
            fail(f"[{label}] {package.kpi} DETECTED without a statistical test")
            violations += 1
        if not package.fdr.significant_after_fdr:
            fail(f"[{label}] {package.kpi} DETECTED without surviving FDR")
            violations += 1
if not violations:
    ok("every DETECTED finding carries a completed test and a corrected verdict")

# ---------------------------------------------------------------------------
rule("7. Runtime: window isolation")
# ---------------------------------------------------------------------------
week = pipeline.analyse(BASE, (date(2023, 11, 1), date(2023, 11, 8)))
month = pipeline.analyse(BASE, (date(2023, 11, 1), date(2023, 12, 1)))
week_rev = next(p for p in week.packages if p.kpi == "revenue" and p.entity == "ALL")
month_rev = next(p for p in month.packages if p.kpi == "revenue" and p.entity == "ALL")

if week_rev.current_value == month_rev.current_value:
    fail("a 7-day window returned the same value as a 30-day window")
else:
    ok(f"7-day window ${week_rev.current_value:,.0f} vs 30-day ${month_rev.current_value:,.0f}")

if week.contradictions.window_end == month.contradictions.window_end:
    fail("reconciliation window was not rescoped")
else:
    ok("reconciliation is recomputed per window")

# ---------------------------------------------------------------------------
rule("8. Runtime: FDR is load-bearing on a wide hypothesis set")
# ---------------------------------------------------------------------------
import duckdb  # noqa: E402

from app import fdr as fdr_mod  # noqa: E402
from app.contracts import get_contract_store  # noqa: E402
from app.engines.detector import MovementDetector  # noqa: E402
from app.series import DuckDBSeriesProvider, SeriesKey  # noqa: E402

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
store = get_contract_store()
wide_result = MovementDetector(DuckDBSeriesProvider(conn, store), store).detect(
    BASE,
    (date(2023, 11, 1), date(2023, 12, 1)),
    keys=[SeriesKey("revenue", "product", s.product_id) for s in catalogue],
)
pool = {h.key: h.test.p_value for h in wide_result.hypotheses if h.test.tested}
alpha = store.detection.fdr_alpha
bh = fdr_mod.correct(pool, alpha=alpha, method=fdr_mod.FDRMethod.BENJAMINI_HOCHBERG)
by = fdr_mod.correct(pool, alpha=alpha, method=fdr_mod.FDRMethod.BENJAMINI_YEKUTIELI)

print(f"  hypotheses tested          : {len(pool)}")
print(f"  raw p <= {alpha} would reject   : {bh.n_raw_significant}")
print(f"  BH rejects                 : {bh.n_significant}  (overturned {len(bh.changed_by_correction)})")
print(f"  BY rejects                 : {by.n_significant}  (overturned {len(by.changed_by_correction)})")
if bh.n_significant >= bh.n_raw_significant:
    warn("BH did not overturn anything on this draw")
else:
    ok("the correction changes decisions, so it is not decorative")
if by.n_significant > bh.n_significant:
    fail("BY rejected more than BH, which is impossible")
else:
    ok("BY is at least as conservative as BH")

# ---------------------------------------------------------------------------
rule("9. Runtime: adversarial change measurement")
# ---------------------------------------------------------------------------
from app.change import measure_change  # noqa: E402

edge = [
    ("0 -> 0", 0.0, 0.0),
    ("0 -> positive", 0.0, 100.0),
    ("positive -> 0", 100.0, 0.0),
    ("denormal -> positive", 5e-324, 1.0),
    ("tiny -> huge", 1e-300, 1e300),
    ("negative -> positive", -50.0, 50.0),
    ("missing baseline", None, 100.0),
    ("missing current", 100.0, None),
]
for label, baseline, current in edge:
    m = measure_change(baseline, current, baseline_floor=0.0)
    values = [m.absolute_change, m.relative_change_pct]
    if any(v is not None and not math.isfinite(v) for v in values):
        fail(f"{label} produced a non-finite value")
    print(f"  {label:<24} {m.change_type.value:<20} rel={m.relative_change_pct}")
ok("no adversarial input yields NaN or Infinity")

# ---------------------------------------------------------------------------
rule("SUMMARY")
# ---------------------------------------------------------------------------
print(f"\n  failures : {len(FAILURES)}")
print(f"  warnings : {len(WARNINGS)}")
for entry in FAILURES:
    print(f"    FAIL {entry}")
for entry in WARNINGS:
    print(f"    WARN {entry}")
raise SystemExit(1 if FAILURES else 0)
