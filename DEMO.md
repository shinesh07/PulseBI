# Demo script — 5 minutes

**Setup, once, before you present:**

```bash
cd backend
python -m uvicorn app.api:app --port 8000
```

Open `http://localhost:8000`. Have a second terminal ready in `backend/`.

Do not enable Ollama for the live demo unless you have rehearsed it on the
presenting machine. The deterministic path is the default for exactly this
reason.

---

## 0:00 — The problem (30s)

> "Revenue is up 55%. Gross margin is down 3.1 points. Every BI tool shows you
> that and stops. The analyst then spends two days reconciling three systems to
> explain it."
>
> "The obvious answer is to point an LLM at the warehouse. On Spider 2.0 — real
> enterprise schemas — the best model gets **17% execution accuracy**. And it
> fails silently: real tables, real columns, wrong joins."
>
> "So we automate the reasoning without letting the model touch the arithmetic."

---

## 0:30 — Scenario 1: Multi-factor movement (75s)

Scenario **Multi-factor movement**, persona **CFO EXECUTIVE**.

Point at the stat row:

> "Ten findings detected, seven abstained. Sixteen hypotheses tested out of
> twenty-three candidates. And **156 numerals verified** — I'll come back to that."

Expand **Evidence** on the top card.

> "Every finding carries the window, the test, its assumptions, the adjusted
> p-value, the dependence assumption, and data-quality state. This is the object
> the narrator receives — and it is the *only* thing it receives."

**The moment to land:**

> "The prototype we started from hardcoded its margin drivers as mix −1.9 and
> freight −2.4. Computed properly, the answer is different **in sign**: price is
> −3.25 points, and mix actually *helped*, because treadmills carry a higher
> margin rate than smartwatches. Volume contributes exactly zero, because a ratio
> is scale-invariant."
>
> "A plausible story was wrong about which lever mattered. Only the arithmetic
> caught it."

---

## 1:45 — Scenario 2: Abstention (60s)

Switch to **Attribution outage**.

> "A webhook drops 85% of attribution tags for one week. Notice what the engine
> does **not** do: it doesn't invent a channel story."

Point at an abstained card.

> "It abstains, and it says why, and it says what would unblock it. Critically it
> still answers where evidence is sufficient — a system that always abstains
> proves nothing."

Show the reconciliation table.

> "Total revenue moved +55%, attributed revenue −4.8%. Both count the same
> orders. That's a 59.8-point divergence, and it's tag loss, not demand. The
> engine flags it as a contradiction and scopes the penalty to the KPIs that
> actually depend on attribution — revenue itself is unaffected."

---

## 2:45 — Scenario 3: New product (45s)

Switch to **New product launch**.

> "A SKU with no prior history. Percentage growth from zero is undefined — most
> systems emit infinity or silently substitute one. We classify it as
> NEW_ACTIVITY and report absolute impact, with no growth rate invented."
>
> "Then empirical Bayes borrows strength from siblings. At one day of history the
> estimate leans 62% on the category prior; by twelve days, 7%. The model hands
> control back to the data exactly as fast as the evidence justifies."

---

## 3:30 — Scenario 4: Role-based masking (45s)

Keep the scenario, switch persona to **VP OPERATIONS**.

> "Same analysis, different entitlements. Operations sees fifteen findings
> **masked**, seven allowed, one denied."

Point at a masked card.

> "Not blocked — masked. They see that revenue moved, how significant it is, how
> confident we are. They don't see the absolute figures. Every decision is in the
> access audit."

---

## 4:15 — The verifier (45s)

Second terminal:

```bash
python run_evals.py
```

> "Twenty-four benchmarks. The one that matters:"

Point at *A fabricated figure is caught rather than published*.

> "We feed it a sentence claiming revenue rose $999,999.99 and margin improved
> 12.5%. Neither number was computed. The verifier rejects it and the sentence is
> withheld."
>
> "That's what makes 'the model doesn't produce quantitative truth' a **checked
> property** rather than a promise. Same gate covers a language model — enabling
> one changes the prose and nothing about the guarantee."

Point at telemetry:

> "And we report `model_calls: 0` honestly, with the token figure labelled a
> projection. We'd rather report zero than fabricate a number."

---

## 5:00 — Close (15s)

> "196 tests, 24 benchmarks, and a sweep that fails the build on hardcoded KPI
> names, unguarded division, or any decision taken on a raw p-value after
> correction."
>
> "Everything you saw is computed, and everything computed is checkable."

---

## If asked

**"Why Benjamini-Yekutieli rather than Benjamini-Hochberg?"**
BH controls the FDR under independence or positive regression dependence. Revenue,
COGS and freight for one product are near-deterministic functions of the same
daily units, so that assumption isn't established. BY holds under arbitrary
dependence. On our wide scenario: raw 12 → BH 10 → BY 7, with all six true
effects surviving both.

**"Is the confidence score a probability?"**
No, and we're explicit about it — it's labelled `governance_rubric_0_1` with
`is_calibrated: false` in every response. Calibrating it would mean labelling
outcomes and fitting the mapping. Calling a weighted sum a probability is exactly
the false precision the system exists to avoid.

**"Where's the AI?"**
The architecture is provider-ready and the model path is wired. We demo the
deterministic path because no API keys were available and a demo shouldn't depend
on a network call. The interesting claim isn't that we call a model — it's that
the faithfulness gate makes it *safe* to.

**"Can it prove causality?"**
No, and we don't claim it. Unobserved confounding is empirically untestable, and
parallel trends break under time-varying confounders. Every fact carries an
evidence tier and narratives say "association, not established causation".

**"Why no cuboid search across all dimensions?"**
Chaos Genius shipped exactly that and disabled it by default after user
feedback — unconstrained multi-dimensional search produces more noise than
signal. We use a contract-declared driver set with FDR gating instead.
