# Parameter reference

Every tunable value in the system, where it lives, and why it holds that value.

**Two tiers, and the distinction matters.** Tier 1 lives in `contracts.yaml` and
is *policy* — a business owner changes it without touching code, and every engine
reads it from there. Tier 2 lives in code as a named constant and is *method* —
part of how an algorithm works, not a business choice.

Nothing in the engines hardcodes a Tier 1 value. `validate.py` fails the build if
one appears.

---

## Tier 1 — governance policy (`backend/app/contracts.yaml`)

### Detection

| Parameter | Value | Why this value |
| :-- | :-- | :-- |
| `fdr_alpha` | `0.10` | Target false-discovery rate. Looser than a conventional 0.05 because the cost of a missed material movement exceeds the cost of one extra investigated finding in a review workflow. |
| `fdr_method` | `benjamini_yekutieli` | BH controls the FDR only under independence or positive regression dependence. Revenue, COGS and freight for one product are near-deterministic functions of the same daily unit counts, so that assumption is not established. BY holds under arbitrary dependence at the cost of power. |
| `min_baseline_points` | `5` | Minimum observations per window for a Mann-Whitney U test. Below this the rank statistic cannot reach significance at α = 0.10 regardless of separation, so running it would produce a p-value that is structurally incapable of rejecting. |
| `robust_scale` | `mad` | Median absolute deviation rather than standard deviation. A single spike inflates σ enough to hide itself; the median does not move. |

### Confidence weights

Six measured signals, summing to 1.0. A signal that cannot be measured
contributes zero; one that does not apply is excluded and the rest renormalise.

| Signal | Weight | Rationale |
| :-- | --: | :-- |
| `statistical_evidence` | `0.25` | Highest weight: it is the only signal that speaks to whether the movement is real rather than to how well it was observed. |
| `sample_size` | `0.15` | Observations backing the test. |
| `effect_magnitude` | `0.15` | Rank-biserial effect size. |
| `data_completeness` | `0.15` | Share of the window's days observed. |
| `attribution_integrity` | `0.15` | Tag coverage. Applies only to KPIs whose contract sources include marketing data. |
| `cross_source_consistency` | `0.15` | Agreement between independent measurements. |

> **These weights are a policy choice, not a fitted model.** The resulting score
> is labelled `governance_rubric_0_1` with `is_calibrated: false` in every
> response. Calibrating it would mean labelling outcomes and fitting the mapping.

### Thresholds

| Parameter | Value | Why |
| :-- | :-- | :-- |
| `abstain_threshold` | `0.50` | Below this the engine refuses to report a finding. |
| `low_confidence_threshold` | `0.70` | Between the two, a finding is surfaced as `LOW_CONFIDENCE` — real enough to see, not solid enough to act on unreviewed. |
| `action_threshold` | `0.70` | Below this an action card is flagged `requires_review`. |
| `staleness_penalty_per_day` | `0.05` | Confidence deduction per day a source sits past its declared SLA, capped at 0.50. |
| `max_rank_weight_delta` | `0.20` | Hard cap on how far analyst feedback may move a finding's ranking. Feedback cannot reach a computed value at all. |

`sensitivity_table()` on every assessment shows where the decision would flip at
0.30 / 0.40 / 0.50 / 0.60 / 0.70 / 0.80, so the arbitrariness of a cut point is
visible rather than hidden.

### Per-KPI materiality

A movement is material if it clears **either** gate. A threshold of `0` means
that gate does not apply.

| KPI | Grain | Relationship | `abs_usd` | `pct` | `baseline_floor` |
| :-- | :-- | :-- | --: | --: | --: |
| `revenue` | daily | additive | 10,000 | 2.5% | 1,000 |
| `cogs` | monthly | additive | 10,000 | 2.5% | 1,000 |
| `freight` | monthly | additive | 5,000 | 2.5% | 500 |
| `gross_margin` | monthly | derived_ratio | **0** | 1.0% | 0.5 |
| `blended_cac` | weekly | multiplicative | 5 | 10.0% | 1.0 |

**`gross_margin` has `abs_usd: 0` deliberately.** Its absolute change is in
percentage points, not currency, so a currency gate would pass trivially and
every margin movement would read as material. A zero means "this gate does not
apply", not "any change qualifies" — that conflation was a real bug the audit
found.

**`baseline_floor`** is the magnitude below which a ratio stops being
informative. Without it, a baseline of 1e-12 moving to 100 yields a relative
change of 1e16 percent, whose materiality exceedance then dominates every
ranking. Scale is per-KPI because "near zero" means something different for
revenue than for a per-unit freight rate.

---

## Tier 2 — method constants (code)

| Constant | Value | Location | Why |
| :-- | :-- | :-- | :-- |
| `MAX_EXCEEDANCE` | `1000.0` | `materiality.py` | Caps how far over threshold a movement can score, so no degenerate baseline dominates the ranking queue. |
| `MAD_TO_SIGMA` | `1.4826` | `stats.py` | Makes MAD a consistent estimator of σ for normally distributed data. Standard constant, not tunable. |
| `CLOSURE_TOLERANCE` (PVM) | `0.01` | `decomposition.py` | One cent. Terms must reconstruct total variance within this or the engine raises. |
| `CLOSURE_TOLERANCE` (bridge) | `1e-6` pp | `margin_bridge.py` | Far tighter, because Shapley efficiency guarantees exact closure — anything above float noise is a bug. Observed residual: ~4e-16. |
| `FACTORS` | 5 factors | `margin_bridge.py` | price, cogs, freight, mix, new. 2⁵ = 32 counterfactual states, cheap to enumerate exactly. |
| `ADDITIVITY_TOLERANCE` | `0.02` | `contradiction.py` | Slices may miss their total by 2% before it counts as a contradiction. |
| `SOURCE_DIVERGENCE_TOLERANCE_PP` | `20.0` | `contradiction.py` | Two measurements of the same quantity diverging by more than 20 percentage points of growth are not measuring the same thing. |
| `MIN_MOVEMENT_TO_RECONCILE` | `1e-9` | `contradiction.py` | Below this there is nothing to reconcile and a ratio would be rounding noise. |
| `COLD_START_DAY_LABEL_THRESHOLD` | `14` | `cold_start.py` | **A display label only.** The estimator is continuous in *n* through the shrinkage weight — nothing changes discontinuously at this line and no decision hangs on it. |
| `MIN_PRIOR_POOL_SIZE` | `2` | `cold_start.py` | Below two siblings the between-group variance cannot be estimated, so the pool falls back to the whole catalogue and the estimate says so. |
| `k` (sample-size saturation) | `10.0` | `confidence.py` | `n/(n+k)` scores ~0.5 at ten observations. Continuous by design — a cliff would make confidence jump as a window widens by one day. |
| `CHARS_PER_TOKEN` | `4.0` | `telemetry.py` | Only ever used for a figure explicitly labelled an estimate. Never presented as a measurement. |
| `Z_90` | `1.6449` | `cold_start.py` | Two-sided 90% normal quantile for the credible interval. |
| Display tolerance ε | `1e-9` | `narrative/verifier.py` | Absorbs float representation error at a rounding boundary: 128.75 rendered as "128.8" differs by exactly half a unit, which lands a few ulps over in binary. |

---

## Environment variables

| Variable | Default | Effect |
| :-- | :-- | :-- |
| `PULSEBI_NARRATIVE` | `deterministic` | Set to `ollama` to use a local model. |
| `PULSEBI_OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint. |
| `PULSEBI_OLLAMA_MODEL` | `llama3.2:3b` | Model tag. |
| `PULSEBI_ALLOWED_ORIGINS` | `localhost:8000,127.0.0.1:8000` | CORS allowlist. Explicit origins, never a wildcard. |

---

## Demo data parameters (`backend/app/data/seed.py`)

Deterministic under `SEED = 42`. These shape the scenarios, not the algorithms.

| Parameter | Value | Purpose |
| :-- | :-- | :-- |
| `COLD_START_ACTIVE_DAYS` | `12` | The new SKU trades for 12 days — under the 14-day label threshold. |
| `UTM_LOSS_RATE_CHRONIC` | `0.35` | Ongoing tag loss on bulky checkout flows. Degraded but survivable: the engine should still answer. |
| `UTM_LOSS_RATE_OUTAGE` | `0.85` | Acute webhook failure, 1–7 Nov. Scoped to that week there is genuinely not enough evidence and the engine abstains. |
| Acquisition exponent | `0.6` | `new_customers = spend^0.6` gives diminishing returns, so blended CAC actually moves under the promotion. |

Having both loss rates is the point: abstention only means something if the
system can also recognise when degradation does *not* warrant abstaining.

### Wide scenario (`wide_scenario.py`)

| Parameter | Value | Purpose |
| :-- | :-- | :-- |
| `n_true` | `6` | Products with a real 1.45–1.9× shift. |
| `n_borderline` | `10` | Products with a 1.10–1.22× shift, near the detection boundary. |
| `n_null` | `14` | No shift. Every rejection here is a false discovery. |
| `noise_cv` | `0.15–0.40` | Coefficient of variation per product. Higher noise makes a given effect harder to detect, which creates genuinely borderline cases rather than a clean split. |

Ground truth is known per product, so the suite can assert what the correction
actually bought rather than just that it ran.
