# PulseBI — Governed KPI Intelligence-to-Action Engine

**Problem Track 3: BusinessIntelligence.ai** · Accenture Innovation Challenge, Round 2

> Every number in this system is produced by SQL or statistics before any sentence
> is written. Language is only ever used to *describe* numbers that already exist —
> and a machine check enforces that, rather than a promise.

---

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt

cd backend
python app/data/seed.py                              # regenerate source data
python -m uvicorn app.api:app --port 8000            # dashboard at localhost:8000
```

Open **http://localhost:8000**. Interactive API docs at `/docs`.

```bash
pytest                  # 196 tests
python run_evals.py     # 24 benchmarks
python validate.py      # anti-pattern sweep
python demo_scenarios.py  # the five required scenarios
```

No build step, no npm, no network. The dashboard is one static file served by the
same process, so the demo cannot fail on a missing dependency or a dead connection.

### API

| Endpoint | Purpose |
| :-- | :-- |
| `GET /` | Dashboard |
| `GET /docs` | Interactive OpenAPI reference |
| `GET /api/health` | Contract version, KPIs, personas, active FDR method and narrative provider |
| `GET /api/contract` | The governed semantic contract the UI renders from |
| `GET /api/scenarios` | Pre-set windows exercising each required capability |
| `GET /api/analyse` | Full pipeline. Requires `persona`, `baseline_start/end`, `event_start/end`; optional `baseline_mode`, `limit` |
| `GET /api/decomposition` | Revenue PVM waterfall and the Shapley margin bridge |
| `GET /api/cold-start` | Entities below the sparse-history threshold, discovered from data |
| `GET /api/cold-start/{entity}` | Empirical-Bayes estimate and shrinkage curve |
| `GET /api/lineage/{kpi}` | Formula, grain, sources and lineage from the contract |
| `GET`/`POST`/`DELETE` `/api/feedback` | Analyst votes; re-ranks within the contract bound, never alters a value |

Windows are always explicit parameters. There is deliberately no "analyse the
current period" convenience endpoint — every window-leakage bug this engine was
rebuilt to fix began with an implicit default.

```bash
curl "http://localhost:8000/api/analyse?persona=CFO_EXECUTIVE\
&baseline_start=2023-10-01&baseline_end=2023-11-01\
&event_start=2023-11-01&event_end=2023-12-01&baseline_mode=AS_REPORTED"
```

### Troubleshooting

| Symptom | Cause |
| :-- | :-- |
| `FileNotFoundError: Source 'pos_orders' expects ...` | Run `python app/data/seed.py` first. |
| Port already in use | `python -m uvicorn app.api:app --port 8010` |
| Dashboard loads but findings are empty | The persona may be denied that KPI. Check `access_audit` in the response, or switch persona. |
| Fonts do not load | The page uses Google Fonts and falls back cleanly offline. Nothing else needs the network. |

Requires **Python 3.11+**. Run `pytest` once after cloning to confirm your
environment before presenting.

---

## The problem

A CFO opens a dashboard on 1 December. Revenue is up 55%. Gross margin is down
3.1 points. The dashboard shows *what* happened and stops.

What happens next today: an analyst spends two or three days pulling daily POS,
weekly ad spend and a monthly ERP ledger; reconciling grains that do not align;
building a variance bridge in Excel; writing a summary. By the time it lands the
month is closed. And the CFO, the VP of Growth and the VP of Ops each needed a
different answer from the same numbers.

The obvious 2025 answer — point a language model at the warehouse — is the thing
that does not work. On **Spider 2.0**, real enterprise schemas with 1,000+ columns,
the best model reaches **17.0% execution accuracy** against **91.2%** on the older
academic benchmark. The failure mode is silent: it uses real tables and real
columns and joins them wrongly.

So the actual problem is: **automate the analyst's reasoning without letting a
language model touch the arithmetic.**

---

## Architecture

```
contracts.yaml ──────────► single source of truth
                           formulas · grains · materiality · access · FDR method
                                    │
   ┌────────────────────────────────▼─────────────────────────────────┐
   │  DETERMINISTIC LAYER                                             │
   │  reconciler · detector · decomposition · margin bridge           │
   │  cold start · confidence · contradiction                         │
   └────────────────────────────────┬─────────────────────────────────┘
                                    │ every value becomes a Fact
   ┌────────────────────────────────▼─────────────────────────────────┐
   │  EVIDENCE PACKAGE  (invariant-checked)                           │
   │  measurement · materiality · test · FDR · data quality · decision│
   └────────────────────────────────┬─────────────────────────────────┘
                                    │ narrator may cite these and nothing else
   ┌────────────────────────────────▼─────────────────────────────────┐
   │  NARRATIVE   Deterministic (default) │ Ollama (optional, free)   │
   │  ▶ FAITHFULNESS VERIFIER — fails closed on any unresolved numeral│
   └────────────────────────────────┬─────────────────────────────────┘
                                    ▼
              RBAC (allow/mask/deny) → telemetry → API → dashboard
```

Full pipeline: **data → window selection → change classification → materiality →
statistical test → FDR pool → multiplicity correction → contradiction →
confidence → decision → evidence package → narrative.**

---

## What makes this different

### 1. The faithfulness verifier

Every numeral in a rendered sentence is extracted and matched against the
evidence package that produced it. A figure resolving to nothing means the
narrator invented it, and the sentence is **withheld**, not published.

This converts *"0% numerical hallucination"* from a claim into a checked
invariant. It is provider-agnostic — the same gate covers the template renderer
and a language model — and it sidesteps the LLM-as-judge reliability problem
entirely, since judge models carry position, verbosity and self-enhancement bias
while numeral resolution is deterministic.

**It earned its place immediately.** It rejected the first template output for
quoting `-4.8%` and `+59.8 pp`: figures the contradiction engine had computed but
the package never declared citable. It then caught a second class of bug — a
value of `-4.75` rendered at one decimal place *is* faithfully `-4.8`, so
tolerance is now derived from each numeral's own display precision.

Current run: **213 numerals verified across 23 findings.**

### 2. The honest math overturned the original story

The prototype this replaces hardcoded its margin drivers. Computed properly from
the same data, the answer differs **in sign**:

| Driver | Hardcoded | Computed |
| :-- | --: | --: |
| Price | *absent* | **−3.25 pp** |
| Product mix | −1.90 pp | **+0.87 pp** |
| Unit freight | −2.40 pp | **−0.66 pp** |
| Unit COGS | *absent* | 0.00 pp |
| New product entry | *absent* | −0.07 pp |
| Volume | +1.20 pp | **0.00 pp** |
| **Total** | −3.10 pp | **−3.10 pp** |

The real cause is the **treadmill price cut** ($300 → $280), not the ocean
freight surcharge. Mix *helped*: treadmills carry a 33.3% margin rate against the
smartwatch's 25.0%. And volume contributes exactly zero, because a ratio is
scale-invariant — something the "+1.20 pp volume expansion lift" could never have
been right about.

A plausible narrative was wrong about which lever mattered. Only the arithmetic
caught it.

### 3. Gross margin needed its own engine

Gross margin is a **ratio**, and ratios do not decompose additively. The
multi-dimensional root-cause literature (Adtributor, HotSpot) explicitly targets
*additive* KPIs; CMMD exists because derived measures need separate treatment.

`margin_bridge.py` treats the movement as a cooperative game over five factors
and computes each factor's Shapley value across all 2⁵ counterfactual states.
**Efficiency** guarantees the parts sum exactly to the whole — closure is a
theorem, not a tolerance — and **order independence** removes the substitution
bias a sequential bridge would carry.

### 4. Multiplicity correction that actually bites

| | Hypotheses | Raw p ≤ 0.1 | After correction |
| :-- | --: | --: | --: |
| Benjamini-Hochberg | 30 | 12 | **10** |
| Benjamini-Yekutieli | 30 | 12 | **7** |

All six true effects survive both; only borderline cases are trimmed.

**Benjamini-Yekutieli is the default**, because BH controls the FDR only under
independence or positive regression dependence — and revenue, COGS and freight
for one product are near-deterministic functions of the same daily unit counts.
The assumption is declared in the contract and travels with every finding.

### 5. Abstention that discriminates

| Scope | Decision |
| :-- | :-- |
| November, full month | **answers** — 10 detected |
| Attribution outage window | **answers with caveats** — degraded but sufficient |
| Window with no data | **abstains** — 17 abstentions, each with a reason |

A system that always abstains proves nothing. This one answers on degraded
evidence and stops only where evidence genuinely runs out.

### 6. RBAC masks rather than walls

The previous prototype returned a blanket error to VP Operations, which produces
no narrative at all. Now absolute values are redacted while the movement, its
significance and its confidence stay visible:

```
CFO_EXECUTIVE   23 allow
DATA_ANALYST    23 allow
VP_GROWTH        8 allow ·  8 mask ·  7 deny
VP_OPERATIONS    7 allow · 15 mask ·  1 deny
```

Every decision is recorded in an access audit.

### 7. Telemetry reports only what it measured

Offline, that means `model_calls: 0` and zero cost — alongside a clearly labelled
projection computed over the *real* assembled prompt string. No literal is ever
reported as a measurement.

Enable a free local model to convert the projection into a measurement:

```bash
ollama pull llama3.2:3b
PULSEBI_NARRATIVE=ollama python -m uvicorn app.api:app --port 8000
```

Token counts then come back from the runtime, and the same verifier gates the
output unchanged.

---

## Evidence

| Check | Result |
| :-- | :-- |
| Unit and property tests | **196 passing** |
| Evaluation benchmarks | **24 passing** |
| Anti-pattern sweep | **0 failures, 0 warnings** |
| Numerals verified end to end | **213 across 23 findings** |
| Randomised closure proofs | 250 PVM · 150 Shapley |

`validate.py` sweeps the codebase for hardcoded KPI or entity names in algorithm
code, unguarded division, arbitrary confidence assignment, and decisions taken on
a raw p-value after FDR.

---

## Statistical assumptions

| Component | Method | Assumes |
| :-- | :-- | :-- |
| Significance | Mann-Whitney U on daily observations | Two independent samples; no distributional form (daily revenue is skewed and bounded below) |
| Multiplicity | Benjamini-Yekutieli (BH available) | BY: arbitrary dependence. BH: independence or PRDS |
| Effect size | Rank-biserial correlation | Companion to the U statistic; bounded in [−1, 1] |
| Margin bridge | Shapley over 5 factors | Efficiency guarantees exact closure |
| Cold start | Hierarchical empirical Bayes | Exchangeability within the prior pool; approximate normality of the daily mean |
| Confidence | Weighted rubric, 6 measured signals | **Not calibrated.** A governance rubric, not a probability |

**Deliberately not claimed:** causality. Unobserved confounding is empirically
untestable, parallel trends break under time-varying confounders, and synthetic
control is brittle with one treated unit. Every fact carries an evidence tier
instead, and narratives say "association, not established causation".

---

## Documentation

| Document | Contents |
| :-- | :-- |
| [`DEMO.md`](DEMO.md) | Five-minute demo script with timings and prepared Q&A |
| [`docs/01-review-of-original-prototype.md`](docs/01-review-of-original-prototype.md) | What was wrong with the prototype this replaces |
| [`docs/02-research-and-plan.md`](docs/02-research-and-plan.md) | Prior-art survey and the build plan followed |
| [`docs/03-parameters.md`](docs/03-parameters.md) | Every tunable value, where it lives, and why it holds that value |
| [`docs/04-bottlenecks.md`](docs/04-bottlenecks.md) | Bottlenecks from the literature, and the twelve defects found during this build |

## Layout

```
backend/
├── app/
│   ├── contracts.yaml       single source of truth
│   ├── change.py            explicit change classification
│   ├── materiality.py       business gate, separate from significance
│   ├── fdr.py               BH + BY, identity-preserving
│   ├── timeseries.py        gap-aware, window-scoped observations
│   ├── series.py            data adapter (the only KPI-name mapping)
│   ├── pipeline.py          end-to-end orchestration
│   ├── evidence_package.py  the LLM boundary, invariant-checked
│   ├── engines/             reconciler · detector · decomposition ·
│   │                        margin_bridge · cold_start · confidence ·
│   │                        contradiction
│   ├── narrative/           provider · verifier · actions
│   ├── governance/rbac.py   allow / mask / deny
│   ├── telemetry.py         measured stages, labelled projections
│   └── api.py               REST + dashboard
├── static/index.html        no-build dashboard
└── tests/                   196 tests
```

---

## Conventions

- **Nothing hardcodes a formula, threshold or permission.** It lives in
  `contracts.yaml` or it is derived from data.
- **All SQL is parameterised.** Sources load through pandas and register as
  views; no path is interpolated into a query.
- **Windows are always explicit.** There is no "current period" default —
  every leakage bug this engine was rebuilt to fix started with one.
- **Decompositions assert closure and raise on failure.** A bridge that does not
  close is a bug, and surfacing it beats shipping a residual.
- **Decisions never consult a raw p-value.**
