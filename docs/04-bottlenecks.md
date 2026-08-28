# Bottlenecks

Two kinds, kept separate because they carry different weight in a review.

**Part A** is what the literature and comparable products say goes wrong with
systems of this shape — the bottlenecks we designed *around* before writing code.

**Part B** is what actually broke during this build. Every entry is a defect that
existed, was found by a specific mechanism, and has a regression test. These are
more useful than Part A, because a system that only lists other people's problems
has not yet met its own.

---

## Part A — bottlenecks known from prior art

### A1. Combinatorial explosion in dimensional search

Multi-dimensional root-cause methods search a cuboid lattice, and the number of
attribute combinations grows exponentially with dimensions. HotSpot needs
Monte-Carlo Tree Search plus hierarchical pruning to make it tractable at all.

**The decisive evidence is not the complexity, it is the adoption.** Chaos Genius
shipped exactly this feature — *DeepDrills*, multidimensional drill-down with
waterfall analysis — and after user feedback made it **optional and disabled by
default**, refocusing on anomaly detection. Real users switched it off because
unconstrained search produced more noise than signal.

**Our position:** we do not search a lattice. Drivers come from a
contract-declared set gated by FDR. Fewer drivers, each defensible. Stated as a
deliberate cut in the plan, not an omission.

### A2. Derived measures do not decompose additively

The additive-KPI literature (Adtributor, HotSpot) explicitly targets sums —
page views, revenue, error counts. Gross margin is a *ratio*. CMMD exists
precisely because derived measures need separate treatment.

**Where this bit the original prototype:** its `margin_drivers` were a hardcoded
dict, because the honest computation is genuinely hard. Computed properly the
answer differs **in sign** — price −3.25 pp dominates, and mix *helps* rather
than hurts.

**Our position:** a Shapley decomposition over five factors across all 2⁵
counterfactual states. Efficiency guarantees closure; order independence removes
the substitution bias a sequential bridge carries.

### A3. Text-to-SQL fails silently on real schemas

On **Spider 2.0** — enterprise schemas with 1,000+ columns — the best model
reaches **17.0%** execution accuracy against **91.2%** on the older academic
benchmark. The failure mode is worse than a syntax error: real tables, real
columns, wrong joins, plausible output.

**Our position:** the model never writes SQL and never computes. The evidence
package is the boundary, and the faithfulness verifier enforces it.

### A4. Multiple comparisons manufacture findings

Every automated-insight system tests many hypotheses at once. At a raw 5%
threshold with *m* tests you expect ~*m*/20 false positives by construction.

**Our position:** Benjamini-Yekutieli across the batch, with the dependence
assumption declared and travelling with every finding.

### A5. Alert fatigue

Fixed thresholds miss gradual degradation and fire on noise. Too low produces
fatigue; too high misses real events.

**Our position:** three independent gates — statistical significance, FDR
correction, and business materiality from the contract. A statistically clean
0.4% move never reaches an executive.

### A6. LLM-as-judge is not a reliable check

Judge models show position, verbosity and self-enhancement bias. Human–human
agreement on evaluation benchmarks runs only ~63–66%.

**Our position:** the faithfulness check is deterministic numeral resolution, not
a model. It has none of those failure modes.

---

## Part B — bottlenecks hit during this build

Ordered by severity. Each was found by a specific mechanism, which is recorded
because *how* a defect surfaced determines whether the next one will.

### B1. Window leakage — CRITICAL

**Defect.** `ConfidenceEngine.assess(window=...)` scoped two of its three
components but not the third. The contradiction report had no window parameter
at all and always measured the full period.

**How it surfaced.** A deliberate probe: run a one-day assessment and compare its
consistency component against the thirty-day figure. They were byte-identical at
`0.0594`. Nothing in the test suite would have caught it, because every test used
the same window.

**Why it mattered.** A user asking about a specific outage week would have
received a confidence score computed over thirty days of unrelated data.

**Fix.** Every value now flows through an explicit half-open `[start, end)`
window. Baseline and event windows are separate parameters and neither defaults.
Reconciliation is recomputed per window.

**Regression test.** `test_window_isolation.py` — add unrelated observations
outside the event window, recompute, assert the event result is unchanged; and
the complement, asserting the test can detect a real difference.

### B2. DuckDB thread-safety race — CRITICAL

**Defect.** A DuckDB connection object is not safe to share across threads.
Parameter binding is per-connection state, so concurrent queries interleave their
bindings and one receives the other's parameters.

**How it surfaced.** Only by opening the dashboard in a real browser. The page
fires several fetches on load; one returned a 400 with:

```
ConversionException: Could not convert string 'Bulky' to INT32
... WHERE CAST(p.date AS DATE) >= ? ... AND p.category = ?
```

A category name had arrived in a date slot.

**Why it mattered most.** Load-dependent, intermittent, and **invisible to a
serial test run**. It would have appeared during the live demo and nowhere else.

**Fix.** `dbaccess.ThreadSafeConnection` gives each thread its own cursor over
one database. Source loading stays on the root connection, because a registered
Python frame is visible only to the connection that registered it.

**Verification.** 32 concurrent requests across four endpoints, zero failures.

### B3. Missing evidence inflated confidence — CRITICAL

**Defect.** `consistency_score()` returned a perfect `1.0` for an empty signal
list. A total measurement failure scored as flawless corroboration.

**How it surfaced.** Probing the confidence model with an emptied report, asking
directly: does absence of evidence raise or lower the score?

**Fix.** Reconciliation now has three outcomes —
`CONSISTENT` / `CONTRADICTORY` / `INSUFFICIENT_EVIDENCE`. An unmeasurable signal
contributes zero rather than default credit. A *third* state was later needed:
`NOT_APPLICABLE`, for a signal that does not bear on a hypothesis at all
(attribution coverage says nothing about whether total revenue moved). Those are
excluded and the remaining weights renormalise.

### B4. FDR pool contaminated by untested hypotheses — HIGH

**Defect.** Every candidate contributed to *m*, including those never tested.
Inflating *m* enlarges every adjusted p-value and weakens genuine findings.

**Compounding defect.** KPIs without a daily series received a neutral `p = 1.0`,
which then failed the FDR gate — **silently suppressing the headline −3.10 pp
gross-margin movement entirely.**

**Fix.** Only hypotheses with a valid p-value in [0, 1] enter the pool. Untested
candidates are excluded and labelled with *why*. `GRAIN_TOO_COARSE` is a
structural limitation permitting materiality-only reporting; the others force an
abstention.

### B5. Unstated dependence assumption — HIGH

**Defect.** BH applied with no dependence assumption stated, though revenue, COGS
and freight for one product are near-deterministic functions of the same daily
unit counts — strongly positively dependent by construction.

**Fix.** Benjamini-Yekutieli is the contract default, holding under arbitrary
dependence. The assumption is declared and travels with every finding.

### B6. Near-zero baselines producing astronomical ratios — HIGH

**Defect.** A baseline of `5e-324` moving to 100 produced a relative change of
1e16 percent, whose materiality exceedance reached **4×10¹⁵** and dominated every
ranking.

**How it surfaced.** Twice, and the second time is the instructive one. First by
an adversarial probe. Then, after the `baseline_floor` fix, a **property test
found a case the floor missed**: a denormal baseline overflows the division even
at floor 0.

**Fix.** Explicit change classification, plus a finiteness guard: any ratio that
is not representable is classified `UNSTABLE_BASELINE` rather than emitted.

**Lesson.** A declared threshold is not the same as a guarantee. The property
test found what the fix did not cover.

### B7. Materiality gate always passing — HIGH

**Defect.** `abs(change) >= 0.0` is always true, so `gross_margin` with
`abs_usd: 0` was material for every movement, however small.

**Fix.** A zero threshold now means "this gate does not apply".

### B8. Ratio conflated with "no activity" — MEDIUM

**Defect.** `pct_change` returned `None` for a zero base, conflating "the ratio
is undefined" with "nothing happened". A new product's launch read as an
unmeasurable event rather than a move from a zero baseline.

**Fix.** Nine explicit change types. A launch is `NEW_ACTIVITY`: material on
absolute impact, with no growth rate invented, and capped at `LOW_CONFIDENCE`
because there is no prior period to test against.

### B9. Unequal windows compared as raw totals — MEDIUM

**Defect.** A 7-day event against a 31-day baseline read as a catastrophic
collapse, purely from the length difference.

**Fix.** An explicit `BaselineMode` — `MATCHED_LENGTH` scales the baseline;
`AS_REPORTED` compares raw totals for like-for-like calendar periods. The caller
declares intent rather than the engine guessing from a threshold.

### B10. Global contradiction penalising unrelated findings — MEDIUM

**Defect.** An attribution outage depressed confidence in *every* finding,
including total revenue — which POS counts identically regardless of tagging.
Everything sat at 0.622 and nothing reached `DETECTED`.

**Fix.** Checks declare `affected_kpis`, derived from the contract's declared
sources. An attribution failure now penalises only KPIs that consume attribution
data.

### B11. Verifier rejecting correct prose — MEDIUM

**Defect.** A computed `-4.75` rendered at one decimal place is faithfully
`-4.8`, but a flat 0.01 tolerance rejected it.

**How it surfaced.** The verifier rejected the *first* template output it ever
saw. Two real defects behind one symptom: figures the contradiction engine
computed were never registered as citable, **and** the tolerance was wrong.

**Fix.** Tolerance derives from each numeral's own display precision — half of
the last significant digit, plus an epsilon for float representation at the
rounding boundary. A fabricated figure must still land within half a display
unit of a real computed value.

### B12. Silent failures in the UI — LOW

**Defect.** A `catch` block set the container to empty string, so a broken
shrinkage curve looked like an empty section.

**Fix.** Errors surface in the hint line. Also fixed: the dashboard hardcoded a
product id, violating the same rule the algorithms follow. The entity is now
discovered from the data.

---

## What this suggests about finding the next one

Three of the four most severe defects — B1, B2, B3 — were **invisible to the
test suite as written**, because every test exercised one window, one thread, and
a fully-populated report.

They were found by:

| Mechanism | Found |
| :-- | :-- |
| Adversarial probing against a written spec | B1, B3, B6, B7 |
| Running the real UI in a real browser | B2, B12 |
| Property tests over randomised inputs | B6 (the case the fix missed) |
| The system's own runtime checks | B11 |

The last row is worth noting: the faithfulness verifier caught a defect that no
test targeted, on its first ever execution. A runtime invariant that fails closed
finds things a test suite has to be told to look for.

## Known limitations

Honest gaps, not defects:

- **Confidence is uncalibrated.** A governance rubric, labelled as such. Split
  conformal prediction over a labelled scenario set is the right next step.
- **No causal claims.** Unobserved confounding is empirically untestable.
- **No seasonality.** Two months of history — it is not identifiable, and
  claiming it would be false precision.
- **Prior pool is small.** The cold-start category has fewer than two siblings,
  so it falls back to the whole catalogue and the estimate flags the weakened
  exchangeability assumption.
- **Feedback is a bounded weight store, not learning.** Preference learning at
  this feedback volume would be unfalsifiable.
