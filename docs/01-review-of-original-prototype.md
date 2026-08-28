# Code Review: shinesh07/PulseAI

Reviewed at commit `70d3713` (branch `main`), a hackathon submission for the
Accenture Innovation Challenge — "PulseBI: Governed Neuro-Symbolic KPI
Intelligence-to-Action Engine" (FastAPI + DuckDB backend, Next.js frontend).

## Summary

The repo is well-documented and the demo scenarios (margin paradox, safe
abstention, cold-start, RBAC) are cleanly modeled end-to-end. The main risk is
that several pieces the README markets as "computed" or "governed" are
actually hardcoded constants tuned to reproduce the demo narrative, and the
one component the whole pitch is built around — an LLM — is never called.

## Findings

1. **No LLM is actually invoked.** `openai` is in `requirements.txt`, and the
   README/eval output describe an "LLM economics" split (94% deterministic /
   6% LLM, 850 tokens, $0.00054/insight), but `prompt_engine.py` only picks
   between four hardcoded if/elif narrative strings per persona
   (`backend/core/narrative/prompt_engine.py:52-76`). `tracker.py` fabricates
   the token count and cost whenever `source="LLM"`
   (`backend/core/telemetry/tracker.py:15-18`) rather than measuring a real
   call. The "0% hallucination" claim is true only because there's no
   generation happening — the telemetry numbers reported are simulated, not
   measured.

2. **The headline PVM margin breakdown is a fixed dict, not a calculation.**
   `WaterfallEngine.calculate_revenue_pvm` returns
   `margin_drivers": {"volume_expansion_lift": 1.20, "sku_mix_shift_drag":
   -1.90, "ocean_freight_surcharge_drag": -2.40}` as literal constants
   (`backend/core/engines/waterfall.py:98-102`), regardless of what the query
   above it computes. Only `price_effect`/`volume_effect`/`total_variance`
   come from DuckDB; the per-driver margin attribution shown in the "Margin
   Paradox" scenario is baked-in to match the pitch deck, not derived.

3. **Implemented PVM math doesn't match the documented formula.** The README
   advertises a 3-term Price/Volume/Mix decomposition
   (`ΔP·Q0 + P0·ΔQ·S0 + P0·Q1·ΔS`), but the SQL only computes a price effect
   and a volume effect and assigns everything else to a residual
   `mix_and_other_effect = total_variance - (price + volume)`
   (`backend/core/engines/waterfall.py:74-75`). There's no mix-share term at
   all, so "mix" isn't decomposed, it's whatever is left over.

4. **`ColdStartEngine` never reads real data.** Despite accepting a
   `reconciler` in its constructor, `calculate_bayes_shrinkage` takes
   `observed_mean`, `observed_n`, `category_prior_mean`, etc. as keyword
   arguments with hardcoded defaults (65.0, 8, 72.0, 25.0, 120.0) and never
   queries `self.conn` (`backend/core/engines/cold_start.py:13-21`). The
   Empirical-Bayes math itself is implemented correctly, but it's running on
   constants tuned to reproduce the demo's "67.6 units/day, [62.59, 72.66]"
   output, not on the SKU's actual order history.

5. **`contracts.yaml` is defined but not loaded.** `RBACManager.__init__`
   takes a `contracts_path` argument and the comment says "We will load from
   contracts in the future. Hardcoding the permissions for the prototype."
   (`backend/core/semantic/rbac.py:11-13`). The governed-semantic-layer
   pitch — one YAML file as the single source of truth for KPI visibility —
   isn't actually wired to the access-control code path; the two can drift
   independently.

6. **CORS + SQL string interpolation are fragile patterns worth fixing before
   this goes anywhere near production**, even though neither is exploitable
   today:
   - `allow_origins=["*"]` combined with `allow_credentials=True`
     (`backend/main.py:22-28`) is a combination browsers reject per the CORS
     spec, and wildcard origins are a bad default regardless.
   - `reconciler.py` and `waterfall.py` build SQL with f-string
     interpolation of `target_month`/`current_month` directly into `LIKE`
     clauses (`backend/core/engines/reconciler.py:30,44,47`,
     `backend/core/engines/waterfall.py:26,36`). Today those values are
     hardcoded in `main.py`, not user input, so it's not currently
     injectable — but the moment a month/date filter is exposed on the API,
     this becomes a SQL injection vector. Parameterize now while it's cheap.

7. **No authentication on any endpoint.** `/api/analyze` and `/api/feedback`
   (a write endpoint) are open to any caller. Fine for a local demo, but
   worth flagging since RBAC is pitched as a governance feature — RBAC only
   restricts which KPI a *stated* persona can see, it doesn't verify the
   caller actually holds that role.

8. **Feedback persistence is best-effort and silently degrades.**
   `FeedbackStore` keeps everything in an in-process list; `mongo_client` is
   never constructed anywhere (`pymongo`/`python-dotenv` in
   `requirements.txt` are otherwise unused), and if it *were* wired up, a
   Mongo write failure is caught and just printed
   (`backend/core/feedback/feedback_store.py:40-45`) — feedback silently
   evaporates on process restart today, and would silently drop the DB write
   on any Mongo failure if that path were ever exercised.

9. **The eval harness checks that the demo hits its own scripted numbers,
   not general correctness.** `eval-harness/run_evals.py` asserts
   `margin_delta_pp == -3.1` and other exact values that `seed_data.py`
   explicitly manufactured to produce ("Costs designed to hit EXACTLY -3.1%
   margin drop", `backend/data/seed_data.py:107`). That's a reasonable
   regression check for the fixed demo dataset, but it's not exercising the
   engines against varied inputs, so it won't catch a real correctness bug
   in the PVM/confidence/Bayes math.

10. **Frontend backend URL is hardcoded.** `actions.ts` calls
    `http://localhost:8000` directly with no environment variable
    (`frontend/src/app/actions.ts:5`), so the Next.js app can't point at a
    deployed backend without a code change.

## Suggested priority if this moves past hackathon stage

1. Either wire up a real LLM call (even a cheap one) or drop the "LLM
   economics" telemetry claims — right now they're fabricated numbers, which
   undercuts the "0% hallucination" pitch once someone checks.
2. Make `margin_drivers` and `ColdStartEngine`'s inputs actually derive from
   the reconciled data instead of hardcoded constants, or clearly label them
   as illustrative placeholders in the API response.
3. Load RBAC visibility from `contracts.yaml` instead of a parallel hardcoded
   dict, since drift between the two defeats the "single governed source of
   truth" premise.
4. Parameterize the SQL date filters before any endpoint exposes
   date/month as a request parameter.
