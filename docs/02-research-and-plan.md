# PulseBI — Research Analysis & Final Build Plan

**Scope:** Problem Track 3 (BusinessIntelligence.ai), Accenture Innovation Challenge Round 2.
**Baseline reviewed:** `shinesh07/PulseAI` @ `70d3713`.
**Constraints set by the team:** under one week of build time, no LLM API keys (fully offline demo),
keep `contracts.yaml` as the semantic layer (enforced, not decorative), full rebuild of the analytics core.

---

## 0. The headline judgement

Your architectural thesis — *"the LLM is not the source of quantitative truth"* — is **correct and now
strongly evidence-backed**. Recent benchmarks make it the defensible position, not the conservative one.
Section 1 gives you the citations to say that out loud.

The problem is not the thesis. It is that **the current implementation does not yet honour it**. Several
components the README describes as computed are constants tuned to reproduce the demo narrative, the LLM
layer does not exist, and the telemetry numbers are fabricated rather than measured. A judge who opens
three files will find this. The rebuild below closes that gap and — importantly — the honest version is a
*stronger* submission than the current one, because "100% deterministic, zero LLM spend, machine-verified
numerics" is a better story than an unverifiable "0% hallucination" claim.

**The single highest-leverage change in this document** is the *evidence ledger + faithfulness verifier*
(§5.3). It converts your central claim from an assertion into a property the test suite checks
mechanically. Nothing else you can build in a week differentiates you as much.

---

## 1. Prior art: how comparable systems actually do this, and where they break

### 1.1 The academic lineage of automated KPI root-cause

| System | Venue | Core idea | Known limitation |
| :-- | :-- | :-- | :-- |
| **Adtributor** | NSDI '14 (Microsoft) | Ranks candidate explanations by *explanatory power*, *surprise* (Jensen–Shannon divergence against the forecast distribution), and *succinctness*. Reports >95% accuracy on a large ad system, cutting troubleshooting time by an order of magnitude. | Assumes the root cause lives in a **single dimension**. Breaks on interacting drivers. |
| **HotSpot** | IEEE Access '18 | Introduces the *ripple effect* (when a root-cause element moves, all its descendants move predictably) and a *potential score* measuring expected-vs-actual deviation. Uses Monte-Carlo Tree Search plus hierarchical pruning to survive the combinatorial cuboid lattice. 95% of cases reach F-score >90%, versus 15% for prior approaches. | Restricted to **additive** KPIs (page views, revenue, error counts). |
| **Squeeze / PSqueeze** | ISSRE '19, JSS '23 | Generalises to a *generalized ripple effect* and a *Generalized Potential Score* robust to both large and small anomalies. Bottom-up probabilistic clustering, then top-down heuristic search within each cluster. | Still fundamentally a dimensional-search method; needs a good forecast baseline. |
| **CMMD** | KDD '22 | Explicitly handles **derived measures** (ratios) by modelling the relationship between fundamental and derived metrics. | Heavier; needs cross-metric structure declared. |

**Why this matters to you directly:** your hero scenario is **gross margin %**, which is a *ratio* — a
derived measure. The entire additive-KPI literature (Adtributor, HotSpot) does **not** apply to it
without adaptation, and CMMD exists specifically because of that gap. This is precisely why your
`margin_drivers` ended up hardcoded: the honest computation is genuinely the hard part. §4.2 solves it.

### 1.2 What production systems learned

- **LinkedIn / StarTree ThirdEye** pairs detection with *dimension exploration*, *top contributors*, and
  — critically — an **Events** feed that contextualises anomalies with external factors (holidays,
  config changes, outages). Their stated lesson is that the system improves through *incremental user
  feedback* and an accumulating knowledge graph of metric dependencies. Automated statistics alone were
  not sufficient.
- **Chaos Genius** (open source, ML analytics engine for outlier detection and RCA) built exactly the
  feature you are building — *DeepDrills*, multidimensional drill-down plus waterfall analysis of top
  KPI drivers. **After user feedback they made it optional and disabled it by default**, refocusing on
  anomaly detection. This is the most important single datapoint in this review: unconstrained
  multi-dimensional driver search produces more noise than signal, and real users switched it off.

  → **Design consequence:** do not build a broad dimensional search. Build a *narrow, contract-declared*
  driver set with statistical gating. Fewer, defensible drivers beat an exhaustive lattice.

- **Commercial tools** (ThoughtSpot SpotIQ change analysis, Sisu, Tellius) all converge on the same
  shape: compare two points, rank contributing attributes by statistical significance, present with
  confidence scores. None of them claim causality.

### 1.3 The bottleneck nobody advertises: multiple comparisons

Every automated-insight system tests many hypotheses at once (each KPI × each dimension × each segment).
Testing many hypotheses inflates the family-wise error rate, so a fixed per-test threshold guarantees
spurious "insights". The standard remedy is **Benjamini–Hochberg FDR control** (1995), which bounds the
expected proportion of false discoveries among reported findings and is more powerful than
Bonferroni-style FWER control.

Your current `anomaly.py` applies a raw `|z| >= 2.0` threshold with no correction — and is never called
by the API at all. §4.3 fixes both.

---

## 2. Why "the LLM writes no SQL" is the right call — with the numbers to prove it

This is your strongest differentiator and you should lead with it.

- **Spider 2.0** (ICLR 2025) evaluates LLMs on *real enterprise* text-to-SQL: databases with 1,000+
  columns, queries often exceeding 100 lines. The best model (o1-preview) reached **17.0%** execution
  accuracy, against **91.2%** for the same class of models on the older Spider 1.0. Most agent
  approaches land in the **5–25%** band.
- **BIRD** state of the art sits around **71.8%** execution accuracy (Arctic-Text2SQL-R1-32B, May 2025).
  But the **FLEX** analysis found BIRD's execution-accuracy metric agrees with human experts only
  **62%** of the time — so even that number is softer than it appears.
- The enterprise failure mode is worse than a syntax error: the model uses *real tables and real columns*
  and still joins them wrongly. The output looks plausible and is silently incorrect.

**The industry answer is the semantic layer** — Cube, dbt MetricFlow, Snowflake Semantic Views, AtScale.
A code-defined model (usually YAML) compiles structured queries into warehouse SQL deterministically, so
the LLM *selects from certified definitions* instead of re-deriving joins and metric logic on each
prompt. That is exactly what `contracts.yaml` is supposed to be. Right now it is loaded by nobody.

**Talking point for the deck:** *"On enterprise schemas, frontier models score 17% on text-to-SQL. We
score 100% on arithmetic, because we never ask a model to do arithmetic. Every number in every sentence
resolves to a SQL fact with a method, a source, and a timestamp — and our test suite fails the build if
one doesn't."*

### 2.1 Semantic-layer decision (confirmed)

Keeping a hand-rolled, rigorously-enforced `contracts.yaml` is the right call for this deadline:

- **Cube** has the strongest governance story (row-, column- and member-level security via access
  policies and a JWT security context) but adds a Node service plus auth plumbing you cannot justify in
  a week.
- **dbt MetricFlow** is Apache-2.0 with YAML metrics and DuckDB support, but ties your repo layout to
  dbt conventions.
- **Sidemantic** is DuckDB-native Python and reads Cube/MetricFlow/LookML formats, but is young (~117
  stars) and **AGPL-3.0**, which is a licensing consideration for a submission you may publish.

Cite them as prior art in the deck ("our contract mirrors the Cube/MetricFlow model"), adopt none.

---

## 3. Gap analysis: current repo vs the problem statement

The Round 2 brief lists eight capabilities and a minimum-prototype checklist. Honest status:

| # | Requirement | Status today | Verdict |
| :-- | :-- | :-- | :-- |
| 1 | Detects and **prioritises material** KPI movements | `anomaly.py` exists but **is never called** by any endpoint | ❌ Missing |
| 2 | Reconciles across heterogeneous sources | `reconciler.py` works, but weekly ad data is loaded and **never used**; no calendar-boundary handling | ⚠️ Partial |
| 3 | Identifies and **ranks** drivers with appropriate methods | PVM computes price/volume only; margin drivers are a **hardcoded dict**; `causal.py` was planned, never built | ❌ Largely absent |
| 4 | Persona narratives with **traceable evidence** | 4 hardcoded if/elif strings; citation tags are decorative constants | ⚠️ Cosmetic |
| 5 | Communicates uncertainty, abstains | Works, but weights (0.40/0.35/0.25) and threshold (0.50) are invented and uncalibrated | ⚠️ Partial |
| 6 | Actions grounded in levers, constraints, decision rights | 7-step frame is good; impacts are hardcoded strings, not computed | ⚠️ Partial |
| 7 | **Learns from feedback** | Feedback appends a string to a narrative; changes nothing | ❌ Cosmetic |
| 8 | Realistic security, cost, latency, scalability | No auth; CORS `*` + credentials; **telemetry numbers fabricated** | ❌ Fails on telemetry |
| — | **3–5 connected KPIs** | Only revenue + gross margin computed. `blended_cac`, `on_time_delivery`, `cold_start_sales` declared in YAML, never computed | ❌ 2 of 5 |
| — | Semantic contract covering definitions, lineage, access | YAML exists; **never loaded**; RBAC is a parallel hardcoded dict; lineage is a literal in `main.py` | ❌ Decorative |
| — | Evidence showing **source freshness** | Not tracked anywhere | ❌ Missing |
| — | Clear LLM vs non-LLM breakdown | No LLM exists; split is invented | ❌ Fails |

**Three things a judge will catch in under five minutes:**

1. `requirements.txt` includes `openai`; nothing imports it. The "94% deterministic / 6% LLM" split,
   850 tokens, and $0.00054/insight in `tracker.py` are literals assigned whenever `source="LLM"`.
2. `waterfall.py` returns `margin_drivers` as a fixed dict (`-1.90`, `-2.40`, `+1.20`) regardless of the
   data — these are the exact numbers the README presents as the analytical result.
3. `eval-harness/run_evals.py` asserts `margin_delta_pp == -3.1`, a value `seed_data.py` explicitly
   engineered ("Costs designed to hit EXACTLY -3.1% margin drop"). The suite proves the demo reproduces
   itself, not that the engines are correct.

**Additional design critique:** the RBAC scenario is self-defeating. `VP_OPERATIONS` receives a blanket
`{"error": "Unauthorized to view Revenue KPI"}`. The brief asks for *row-, column- and domain-level*
security and *differentiated narratives*. A wall is a worse demo than graceful degradation — VP Ops
should see the same movement with margin and COGS **masked** and ops-relevant actions surfaced.

---

## 4. The analytics rebuild: what to compute and how

### 4.1 Price-Volume-Mix, done properly

The FP&A standard three-term decomposition is:

```
Price  Effect = (P₁ − P₀) · Q₁
Volume Effect = (Q₁ − Q₀) · P₀
Mix    Effect = (S₁ − S₀) · TotalRevenue₀        where S = unit share
```

Your README advertises this. Your code computes price and volume only, then dumps everything else into
a residual it labels "mix". That residual is not mix — it is mix *plus everything unexplained*.

**Critically, your dataset has a new-product launch.** `YOG-01` (Yoga Mat) has zero October units and
1,000 November units. The FP&A literature is explicit that the plain mix-change method *does not handle
new and discontinued products* and will distort results. Two accepted remedies:

- **Residual method:** `Mix = Total Variance − Price − Volume − New Products − Discontinued Products`
- **Controller-Akademie method:** treat new and discontinued products as separate, explicitly reported terms.

**Use the Controller-Akademie form.** It gives you a five-bar waterfall (Price, Volume, Mix, New Product,
Discontinued) where the New Product bar is *the Yoga Mat*, which is also your cold-start SKU. The same
chart then tells two of your four scenarios. That is a better demo *and* it is correct.

**Order-dependence caveat to state in the deck:** sequential decompositions are sensitive to the order in
which effects are introduced. The Shapley value resolves this by averaging over all orderings, and its
*efficiency* axiom guarantees the parts sum exactly to the whole. With 3–5 terms, exact Shapley is
cheap (≤120 permutations). **Recommendation:** ship the classical decomposition as primary (it is what
FP&A audiences expect), and add a Shapley cross-check that asserts the two agree within tolerance. That
single test is a strong credibility signal in Q&A.

**Non-negotiable:** assert closure. `|total_variance − Σ(terms)| < 0.01` in code, and property-test it
against randomised inputs — not just the seeded demo values.

### 4.2 The margin bridge — the derived-measure problem

This is the hard one, and solving it honestly is your biggest credibility win.

Gross margin % is a **ratio**, so contributions do not decompose additively. Do not fake it. Decompose
`Δmargin_pp` explicitly:

```
margin% = (Revenue − COGS − Freight) / Revenue

Δmargin_pp = price_contribution        (price moves change both numerator and denominator)
           + cogs_rate_contribution    (unit COGS changes at constant mix)
           + freight_rate_contribution (unit freight changes at constant mix — your ocean surcharge)
           + mix_contribution          (shift in unit share toward lower-margin SKUs)
           + new_product_contribution  (YOG-01 entering the basket)
```

Compute each by holding the others at period-0 values and taking the delta, then **assert the sum equals
the observed `margin_delta_pp` within 0.01 pp**. If it does not close, you have a bug — which is exactly
the point, and exactly what the hardcoded dict was hiding.

Reference CMMD (KDD '22) in the deck as the published treatment of derived-measure RCA, and note that
you implement a closed-form bridge because your metric tree is declared rather than discovered.

### 4.3 Detection and prioritisation (currently missing entirely)

Requirement #1 has no implementation. Build `detector.py`:

1. Robust deviation per KPI per dimension slice — median/MAD rather than mean/σ, so a single spike does
   not inflate the baseline.
2. **Benjamini–Hochberg FDR correction** across the full set of tests (KPI × dimension × slice). Report
   the adjusted q-value alongside the raw p-value.
3. Business materiality gate from `contracts.yaml` (absolute impact and % thresholds, per KPI).
4. Rank by `|business_impact| × significance`, return a prioritised queue.

**Say this in the deck:** *"We run N tests. Uncorrected, a 5% threshold yields roughly N/20 spurious
findings by construction. We apply Benjamini–Hochberg and report q-values, so the ranked list has a
bounded false-discovery rate."* Very few hackathon submissions will say this.

**Explicitly out of scope, and say why:** no seasonality decomposition. You have two months of data;
seasonality is not identifiable from two months. Stating that limitation is worth more than an STL call
that cannot possibly work.

### 4.4 Cold start

The empirical-Bayes shrinkage math in `cold_start.py` is correct. The problem is that every input is a
hardcoded default and `self.conn` is never queried.

Fix: estimate `category_prior_mean` and `category_prior_var` **from the sibling category in DuckDB**, and
`observed_mean` / `sample_var` from the SKU's actual order history. The posterior then moves from the
pooled prior toward the item's empirical statistics as `n` grows — the behaviour the method exists for.
Report the shrinkage weight `B` in the UI so viewers can see how much is prior versus observed.

### 4.5 Confidence and abstention

Your rubric (0.40 completeness + 0.35 integrity + 0.25 consistency, abstain below 0.50) is defensible as
a *business rubric* but is presented as if calibrated. Two options:

- **Minimum (fits the week):** move the weights and threshold into `contracts.yaml`, label the score
  explicitly as a governance rubric rather than a probability, and publish a sensitivity table showing
  which scenarios flip at which thresholds.
- **Better (if a day is free):** hand-label ~30 synthetic scenarios as *explainable* / *not explainable*,
  hold out a calibration set, and pick the threshold by **split conformal prediction**. That buys you a
  distribution-free statement of the form *"on non-abstained cases, error ≤ 10% at 90% target coverage"*
  under the exchangeability assumption. Conformal abstention is the standard framework here and is a
  genuine differentiator.

Also build a **real** contradiction detector. Scenario 4 currently returns a hardcoded string. Compare
ad-derived conversion signal against POS orders on the reconciled grain and flag divergence beyond a
declared threshold — you already have `marketing_spend.csv` loaded and unused.

### 4.6 Causality — what to claim and what not to

The rigorous approach is Shapley attribution over causal mechanisms in a declared DAG
(Budhathoki & Janzing, *"Why did the distribution change?"*, available as `distribution_change` in
DoWhy's `gcm` module). It requires a causal graph and two datasets.

**Do not implement this in a week, and do not claim causality.** The literature is blunt about why:
unobserved confounding is empirically untestable and "in most realistic settings the threat of
unobserved confounding lurks"; difference-in-differences relies on a parallel-trends assumption that
time-varying confounders break; synthetic control is brittle with a single treated unit.

**Instead — and this directly answers the brief's demand that teams "explicitly demonstrate when they use
deterministic logic, SQL, business rules, statistics, traditional ML, causal inference, retrieval or
LLMs, and why"** — tag every fact with an **evidence tier**:

| Tier | Meaning | Example in your system |
| :-- | :-- | :-- |
| `deterministic_sql` | Exact arithmetic over source rows | Revenue, total variance, PVM terms |
| `statistical_estimate` | Inference with quantified uncertainty | Empirical-Bayes posterior, z-scores, q-values |
| `business_rule` | Declared threshold or policy | Materiality gates, abstention threshold, RBAC |
| `assumption` | Structural claim not tested by data | "Freight surcharge caused the margin drop" |

Then have the narrative *say the tier*: "the freight rate rose 14.3% (deterministic) and coincides with
the margin decline (association, not established causation)". Being the team that refuses to overclaim
causality is a scoring position, not a weakness.

---

## 5. Architecture for the rebuild

```
contracts.yaml ──► ContractStore (Pydantic-validated, single source of truth)
                        │  definitions · formulas · grain · freshness SLA · metric tree
                        │  materiality thresholds · lineage · access policy
                        ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ LAYER 1 — DETERMINISTIC ANALYTICS (DuckDB + NumPy/SciPy)       │
   │  reconciler · detector(FDR) · decomposition(PVM+new-product)   │
   │  margin_bridge · cold_start(EB from data) · confidence         │
   │  contradiction                                                 │
   └────────────────────────────┬───────────────────────────────────┘
                                │ emits Fact objects only
                                ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ LAYER 2 — EVIDENCE LEDGER                                      │
   │  Fact{id, kpi, value, unit, method, evidence_tier, inputs,     │
   │       sql_hash, source_tables, as_of, confidence}              │
   └────────────────────────────┬───────────────────────────────────┘
                                │ narrative may reference Fact ids ONLY
                                ▼
   ┌────────────────────────────────────────────────────────────────┐
   │ LAYER 3 — NARRATIVE & ACTION (offline default, LLM-ready)      │
   │  NarrativeProvider ── DeterministicProvider (default)          │
   │                    └─ LLMProvider (structured output, off)     │
   │  ▶ FaithfulnessVerifier: every numeral must resolve to a Fact  │
   │  ActionRecommender: impacts COMPUTED from the bridge           │
   └────────────────────────────┬───────────────────────────────────┘
                                ▼
   RBAC (deny│mask│allow, from contract) → Telemetry (measured) → API → UI
```

### 5.1 `contracts.yaml` becomes load-bearing

Extend it to carry everything currently hardcoded:

```yaml
kpis:
  gross_margin:
    description: "..."
    grain: monthly
    formula: "(revenue - cogs - freight) / revenue"
    sources: [pos_orders, erp_financials]
    freshness_sla_hours: 24
    materiality: { abs_usd: 10000, pct: 2.5 }
    metric_tree:
      relationship: derived_ratio          # additive | multiplicative | derived_ratio | influencing
      children: [revenue, cogs, freight]
    lineage:
      - { node: "SAP ERP GL Ledger", type: financial }
      - { node: "DuckDB Reconciler",  type: compute }
    access:
      CFO_EXECUTIVE:  allow
      DATA_ANALYST:   allow
      VP_GROWTH:      mask          # sees movement, not absolute COGS
      VP_OPERATIONS:  mask
```

The `metric_tree` block matters: the metric-tree literature distinguishes **additive**, **multiplicative**,
and **influencing** relationships, and declaring which one applies is what lets the engine pick the right
decomposition instead of guessing. It also gives the UI a real DAG to draw instead of a literal dict.

**Ship a conformance test** that fails if any engine computes a KPI absent from the contract, or if the
contract declares a KPI no engine computes. This is what makes the governance claim real.

### 5.2 Deliver all five KPIs

You currently compute two. `blended_cac` (weekly marketing ÷ new customers), `on_time_delivery`, and
`cold_start_sales` need real implementations — `blended_cac` in particular finally makes
`marketing_spend.csv` load-bearing and gives you the second heterogeneous grain the brief asks for.

**The grain problem is a feature, not an obstacle.** A marketing week straddling 31 Oct / 1 Nov must be
allocated pro-rata across both months. Your current reconciler ignores this entirely. Implementing and
*showing* the boundary allocation is a direct hit on "different refresh cadences and grains" and takes
about an hour.

Track `as_of` per source and surface freshness in every response — the brief explicitly requires
"evidence showing source freshness".

### 5.3 The evidence ledger and the faithfulness verifier ★

**This is the differentiator. Build it first, and demo it.**

Every value Layer 1 produces becomes a `Fact` with an id. The narrative layer may only reference facts.
Then, after rendering — *and identically for the LLM path if keys ever appear* — run:

```python
def verify(narrative: str, ledger: dict[str, Fact]) -> VerificationResult:
    """Every numeral in the narrative must resolve to a Fact value within tolerance."""
    claimed  = extract_numerals(narrative)          # currency, %, pp, counts
    resolved = [n for n in claimed if ledger.matches(n, tol=0.01)]
    unmatched = set(claimed) - set(resolved)
    return VerificationResult(
        ok=not unmatched,
        coverage=len(resolved) / max(len(claimed), 1),
        unmatched=unmatched,
    )
```

Fail closed: if a numeral does not resolve, the API returns the abstention envelope rather than the
narrative. Surface the result in the response and paint it in the UI.

Why this is worth more than anything else you can build this week:

- It converts "0% numerical hallucination" from a **claim** into a **checked invariant**.
- The faithfulness literature distinguishes *factuality* (contradicts the world) from *faithfulness*
  (contradicts its own source). You cannot verify factuality; you can verify faithfulness completely,
  because you own the source. Say exactly that.
- It is provider-agnostic. The day you add an LLM, the same verifier gates it unchanged — which is the
  honest answer to "how would this scale to a real model?"
- It sidesteps the LLM-as-judge reliability problem entirely. Judge models exhibit position, verbosity
  and self-enhancement bias, and human-human agreement on evaluation benchmarks is only ~63–66%. A
  deterministic numeral-resolution check has none of those failure modes.

### 5.4 Narrative and actions

Keep the `NarrativeProvider` interface with two implementations. `DeterministicProvider` (offline
default) renders persona templates from Facts. `LLMProvider` exists, is fully wired with a Pydantic
structured-output schema, and is **off by default** — so the code path is demonstrable and the
architecture claim is true, without the demo depending on a network call.

Action cards keep the 7-step frame but must **compute** expected impact from the margin bridge rather
than hardcoding `+$42,000`. Gate each candidate on: (a) decision rights from the contract, (b) the
driver actually appearing in the ranked top-k, (c) confidence above the action threshold.

### 5.5 RBAC: mask, don't wall

Three modes from the contract: `allow`, `mask`, `deny`. VP Ops sees the movement with margin and COGS
redacted and gets ops-relevant actions. Every response carries an `access_decisions` audit array naming
each field and the policy applied — that is your auditability evidence.

Add a `X-Persona` header + a simple signed token so the persona is *asserted and checked*, not merely
declared in the request body. Right now anyone can claim any role. Also fix `allow_origins=["*"]` with
`allow_credentials=True` (browsers reject that combination anyway) and parameterise the SQL date filters
before any endpoint exposes a date parameter.

### 5.6 Telemetry: measure it or don't report it

Given the offline constraint, report this shape:

```json
{
  "mode": "deterministic_offline",
  "stages": [
    {"name": "reconcile",     "ms": 4.1,  "tier": "deterministic_sql"},
    {"name": "decomposition", "ms": 2.8,  "tier": "deterministic_sql"},
    {"name": "detection",     "ms": 1.9,  "tier": "statistical_estimate"},
    {"name": "narrative",     "ms": 0.7,  "tier": "template_render"}
  ],
  "llm": {"model_calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0},
  "projected_llm_if_enabled": {
    "prompt_tokens_est": 812,
    "estimation_method": "chars/4 heuristic over the actual assembled prompt",
    "note": "No model was called in this run. Projection only."
  }
}
```

Three rules:

1. **Never report a fabricated number as measured.** Delete the `tokens = 850` / `cost = 0.00054`
   literals from `tracker.py`.
2. The projection is computed over the **actual prompt string** the `LLMProvider` would send —
   real characters, labelled estimation method. That is honest and still answers the brief's
   token/cost requirement.
3. Compute the deterministic-vs-LLM split from **measured stage timings**, not an assumed constant.

> ⚠️ **Flagging the constraint you set.** The brief explicitly requires "runtime telemetry covering
> latency, model calls, token usage and estimated cost" and "a clear breakdown of LLM versus non-LLM
> processing". Fully offline, your honest answer is `model_calls: 0`. That is *defensible* and arguably
> a stronger position — but it is a deliberate trade. If you can spare two hours, a small local model
> (Ollama, ~1–3B) would make token counts and latency genuinely measured for one scenario, converting
> the projection into a measurement. **My recommendation: do this if and only if Days 1–4 land on time.**

### 5.7 Feedback that actually changes something

Today `get_contextual_corrections` appends a string to a narrative. Make it change **ranking**:

- Store per-driver weight adjustments from analyst votes.
- The driver ranker applies a **bounded** multiplier (clamp to ±20%) to *presentation ordering only*.
- **Never** let feedback touch arithmetic. Facts are immutable; only ranking and emphasis move.
- Show the adjustment in the UI and record it in the fact's provenance, so a viewer sees *why* order changed.

This is demonstrable live — downvote a driver, watch it drop — which is far more convincing than a
persisted note. The bandit/preference-learning literature is the principled version; a bounded weight
store is the honest week-scale approximation, and you should describe it as such.

---

## 6. Tech stack verdict

**Keep everything.** FastAPI + DuckDB + Python and Next.js + Tailwind + Recharts are appropriate; DuckDB
in particular is genuinely the right tool for in-memory multi-grain reconciliation, and it is what the
open-source semantic layers themselves target. Changing stack now would burn the week for no scoring gain.

Changes worth making inside that stack:

| Change | Why |
| :-- | :-- |
| Use **Pydantic v2 models** for the insight/Fact schema | Already a dependency, unused for this. Gives you the structured-output contract that the LLM path needs later, for free now. |
| Actually use **SciPy/NumPy** | Both already in `requirements.txt` and unused. Needed for MAD, FDR correction, and the Bayes fit. |
| **Drop** `openai`, `pymongo`, `python-dotenv`, `statsmodels` from requirements | All unused. A requirements file listing an LLM SDK you never import is exactly what invites the "is this real?" question. Re-add `openai` only when `LLMProvider` genuinely imports it. |
| Add **`pytest` + `hypothesis`** | Property-based closure tests are the cheapest credibility you can buy. |
| Frontend: add `NEXT_PUBLIC_API_URL` | `actions.ts` hardcodes `http://localhost:8000`; you cannot deploy or demo off-laptop without it. |

**Structured-output note for later:** when you do enable an LLM, expect structured JSON output to cost
roughly 2–3× the tokens of free text and constrained decoding to add ~10–30% latency, and use prompt
caching for the static contract/schema preamble — cached reads typically bill around 10% of input price
and cut latency substantially. Design the prompt with the static block first so it is cacheable.

---

## 7. Six-day build plan

> **Reality check on your two answers.** "Full rebuild" in "under a week" is only achievable if the scope
> below is treated as fixed and the cuts in §8 hold. The ordering is deliberate: **Days 1–3 remove every
> claim a judge can falsify.** If you lose days, you ship after Day 3 with a smaller but *entirely honest*
> system — which still scores better than the current state. Do not reorder to chase UI polish.

### Day 1 — Foundation
- [ ] `contracts.yaml` v2: formulas, grain, freshness SLA, materiality, metric tree, lineage, access policy.
- [ ] `ContractStore` with Pydantic validation. **Nothing else may hardcode a formula or a permission.**
- [ ] `Fact` model + `EvidenceLedger`.
- [ ] `reconciler.py`: multi-grain load, **pro-rata week→month boundary allocation**, per-source `as_of`.
- [ ] Test: contract conformance (every computed KPI declared, every declared KPI computed).

### Day 2 — Get the math honest *(highest-risk day; protect it)*
- [ ] `decomposition.py`: 3-term PVM + **new-product** and **discontinued** terms (Controller-Akademie form).
- [ ] `margin_bridge.py`: ratio decomposition into price / COGS / freight / mix / new-product.
- [ ] Closure assertions in code (`< 0.01` tolerance) for both.
- [ ] Property tests with `hypothesis`: closure holds on randomised inputs, **not just the seeded demo**.
- [ ] Shapley cross-check on the 5-term decomposition (≤120 permutations — cheap, and a strong Q&A answer).
- [ ] **Delete the hardcoded `margin_drivers` dict.**

### Day 3 — The missing capabilities
- [ ] `detector.py`: robust MAD deviation + **Benjamini–Hochberg FDR** + materiality gate + ranked queue. Wire into `/api/analyze`.
- [ ] `cold_start.py`: fit priors from the sibling category **in DuckDB**; expose shrinkage weight.
- [ ] `contradiction.py`: real ads-vs-POS divergence signal.
- [ ] `confidence.py`: weights and threshold from contract; sensitivity table.
- [ ] Implement `blended_cac`, `on_time_delivery`, `cold_start_sales` → **five real KPIs**.

### Day 4 — Narrative, actions, governance, telemetry
- [ ] `NarrativeProvider` interface; `DeterministicProvider` default; `LLMProvider` wired but off.
- [ ] **`FaithfulnessVerifier`** — fail closed on any unresolved numeral. ★
- [ ] `ActionRecommender`: impacts computed from the margin bridge; gated on decision rights + rank + confidence.
- [ ] RBAC `allow`/`mask`/`deny` from contract + `access_decisions` audit array. **Replace the VP Ops wall with masking.**
- [ ] Telemetry: measured stages; `model_calls: 0`; labelled projection. Delete fabricated literals.
- [ ] Security hygiene: parameterised SQL, CORS fix, persona assertion.

### Day 5 — API and UI
- [ ] Endpoints: `/api/movements` (ranked queue — new), `/api/analyze`, `/api/evidence/{fact_id}` (new), `/api/lineage/{kpi}` (from contract), `/api/telemetry`, `/api/feedback`.
- [ ] UI: 5 KPI cards; five-bar waterfall; **evidence drawer** (click any number → method, inputs, source, `as_of`, tier); masked fields shown as redacted rather than hidden; telemetry panel; feedback that **visibly re-ranks**.
- [ ] `NEXT_PUBLIC_API_URL`.

### Day 6 — Proof and story
- [ ] Rewrite `run_evals.py`: closure property tests · faithfulness (every numeral in every persona narrative resolves) · RBAC no-leak fuzz over **all** persona × KPI pairs · abstention precision/recall · contract conformance · telemetry-honesty assertion (`model_calls == 0` offline, no fabricated fields).
- [ ] **Rewrite the README to match reality.** Every current claim that is not computed must go or be relabelled.
- [ ] Demo script + two full rehearsals.

---

## 8. Explicit cuts — and how to defend them

Saying "we deliberately did not build X, here is why" scores better than a shallow X.

| Cut | Defence |
| :-- | :-- |
| Multi-dimensional cuboid search (Adtributor/HotSpot/Squeeze) | Combinatorially explosive, and **Chaos Genius disabled its own drill-down by default after user feedback**. We use a contract-declared driver set with FDR gating: fewer drivers, each defensible. |
| DoWhy / causal estimation | Unobserved confounding is empirically untestable; parallel-trends and synthetic-control assumptions do not hold on two months of single-unit data. We label evidence tiers and explicitly do not claim causation. |
| Seasonality (STL/Prophet) | Two months of history. Seasonality is not identifiable. Claiming it would be false precision. |
| Cube / MetricFlow / Sidemantic | Governance is the *scored* property, not the vendor. Our contract mirrors their model; adopting one costs a day of plumbing and buys no points. (Sidemantic is also AGPL-3.0.) |
| Live LLM calls | No keys available. The architecture is provider-ready and the faithfulness verifier gates both paths identically — we demo the deterministic path and report `model_calls: 0` honestly. |

---

## 9. Definition of done

Ship only when all of these pass:

1. **No hardcoded analytical constants.** Grep the engines for magic numbers; every one is either in
   `contracts.yaml` or derived from data.
2. **Closure holds** for PVM and the margin bridge under randomised property tests.
3. **Every numeral in every persona narrative resolves to a Fact.** Verified, not asserted.
4. **Five KPIs computed** from three sources at three grains, with `as_of` freshness on each.
5. **RBAC fuzz finds no leak** across all persona × KPI pairs, and masking (not walling) is demonstrated.
6. **Telemetry contains no fabricated field**; the LLM/non-LLM split derives from measured timings.
7. **Feedback visibly changes ranking** in the live demo, and provably never changes arithmetic.
8. **README describes only what the code does.**

---

## 10. Sources

**Root-cause & anomaly localisation**
- [Adtributor: Revenue Debugging in Advertising Systems (NSDI '14)](https://www.usenix.org/conference/nsdi14/technical-sessions/presentation/bhagwan)
- [HotSpot: Anomaly Localization for Additive KPIs with Multi-Dimensional Attributes (IEEE Access '18)](https://ieeexplore.ieee.org/document/8288614/)
- [Squeeze: Generic and Robust Localization of Multi-Dimensional Root Causes (ISSRE '19) — reference implementation](https://github.com/NetManAIOps/Squeeze)
- [Generic and robust root cause localization for multi-dimensional data (PSqueeze, JSS 2023)](https://www.sciencedirect.com/science/article/abs/pii/S0164121223001437)
- [CMMD: Cross-Metric Multi-Dimensional Root Cause Analysis (KDD '22)](https://dl.acm.org/doi/10.1145/3534678.3539109)
- [Chaos Genius — open-source analytics engine for outlier detection and RCA](https://github.com/chaos-genius/chaos_genius)
- [StarTree ThirdEye — root cause analysis](https://startree.ai/resources/root-cause-analysis-thirdeye-101)
- [ThoughtSpot SpotIQ change analysis](https://docs.thoughtspot.com/cloud/latest/spotiq-change)

**Text-to-SQL and semantic layers**
- [Spider 2.0: Evaluating Language Models on Real-World Enterprise Text-to-SQL Workflows (ICLR 2025)](https://proceedings.iclr.cc/paper_files/paper/2025/file/46c10f6c8ea5aa6f267bcdabcb123f97-Paper-Conference.pdf)
- [Arctic-Text2SQL-R1: BIRD state of the art](https://www.snowflake.com/en/blog/engineering/arctic-text2sql-r1-sql-generation-benchmark/)
- [Text-to-SQL Benchmarks are Broken: An In-Depth Analysis of Annotation Errors (CIDR 2026)](https://www.vldb.org/cidrdb/papers/2026/p5-jin.pdf)
- [Why text-to-SQL fails (Omni Analytics)](https://omni.co/blog/why-text-to-sql-fails)
- [What Is a Semantic Layer? (Cube)](https://cube.dev/articles/what-is-a-semantic-layer)
- [Cube — data access policies (row / column / member-level security)](https://docs.cube.dev/docs/data-modeling/data-access-policies)
- [Sidemantic — universal open-source metrics layer](https://github.com/sidequery/sidemantic)

**Attribution & decomposition**
- [Why did the distribution change? — Budhathoki & Janzing (Amazon Science)](https://assets.amazon.science/b6/c0/604565d24d049a1b83355921cc6c/why-did-the-distribution-change.pdf)
- [DoWhy — attributing distributional changes](https://www.pywhy.org/dowhy/v0.13/user_guide/causal_tasks/root_causing_and_explaining/distribution_change.html)
- [Using the Shapley value approach to variance decomposition (Strategic Management Journal, 2021)](https://sms.onlinelibrary.wiley.com/doi/full/10.1002/smj.3236)
- [A Quantifiable Approach to Price Volume Mix Analysis (FTI Consulting)](https://www.fticonsulting.com/insights/white-papers/quantifiable-approach-price-volume-mix-analysis)
- [Price Volume Mix analysis — handling new and discontinued products](https://zebrabi.com/price-volume-mix-analysis-excel/)
- [An Introduction to Metric Trees (Count)](https://count.co/blog/intro-to-metric-trees)

**Uncertainty, abstention & multiple testing**
- [Benjamini–Hochberg procedure / false discovery rate](https://link.springer.com/rwe/10.1007/978-1-4419-9863-7_1215)
- [False Discovery Control in Multiple Testing: theories and methodologies](https://arxiv.org/html/2411.10647v1)
- [Conformal abstention framework](https://www.emergentmind.com/topics/conformal-abstention)
- [Adaptive Thresholding Heuristic for KPI Anomaly Detection](https://arxiv.org/abs/2308.10504)
- [Causal Inference With Observational Data and Unobserved Confounding Variables](https://pmc.ncbi.nlm.nih.gov/articles/PMC11750058/)

**LLM reliability & economics**
- [A review of faithfulness metrics for hallucination assessment in LLMs](https://arxiv.org/pdf/2501.00269)
- [Judging the Judges: A Systematic Study of Position Bias in LLM-as-a-Judge (IJCNLP 2025)](https://aclanthology.org/2025.ijcnlp-long.18/)
- [JSONSchemaBench: A Rigorous Benchmark of Structured Outputs for Language Models](https://arxiv.org/pdf/2501.10868)
- [LLM prompt caching guide](https://techsy.io/en/blog/llm-prompt-caching-guide)
