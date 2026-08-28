# PulseBI Next.js Frontend

A modern, high-performance Next.js application built with **React**, **TypeScript**, **Tailwind CSS**, and **Lucide Icons** for the **PulseBI Governed KPI Engine**.

---

## How to Run

### 1. Start the FastAPI Backend Server (Port 8000)
```bash
cd ../backend
source .venv/bin/activate
python -m uvicorn app.api:app --port 8000
```

### 2. Start the Next.js Frontend Dev Server (Port 3000)
```bash
cd ../frontend
npm run dev
```

Open **[http://localhost:3000](http://localhost:3000)** in your browser.

---

## Features & Components

* **Glassmorphic Theme**: Sleek dark-mode aesthetic with custom design tokens, backdrop filters, and HSL tailored indicators.
* **Interactive Workbench**: Scenario presets, persona switcher (`CFO_EXECUTIVE`, `VP_GROWTH`, `VP_OPERATIONS`, `DATA_ANALYST`), date window controls, and baseline mode (`MATCHED_LENGTH` / `AS_REPORTED`).
* **Multi-Factor Waterfall Visualizers**:
  * **PVM Waterfall**: Price, Volume, Mix, Entering Products, Exiting Products with mathematical closure checks.
  * **Shapley Gross Margin Bridge**: Ratio decomposition across 32 counterfactual states.
* **Persona Findings & Machine-Verified Insights**: Filter by decision (`DETECTED`, `LOW_CONFIDENCE`, `ABSTAIN`), view RBAC masked fields, expandable JSON evidence ledger, and structured actionable recommendations.
* **Analyst Feedback Loop**: Interactive upvote/downvote re-ranking controls.
* **Reconciliation & Freshness**: Interactive cross-source reconciliation and SLA freshness monitor.
* **Cold Start Visualizer**: Empirical Bayes shrinkage curve for newly launched SKUs.
* **API Proxy**: API requests to `/api/*` on port 3000 are automatically proxied to `http://localhost:8000` via `next.config.ts`.
