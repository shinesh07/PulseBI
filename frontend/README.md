# PulseBI frontend

One file: `index.html`. No build step, no npm, no framework.

## Why a single file

A hackathon demo fails on the boring things — a missing `node_modules`, a
version mismatch, a dead network on the presenting laptop. This has no install
step and no runtime dependencies, so it cannot fail that way. Open the backend
and the page is there.

## Run it

It is served by the backend, so there is nothing separate to start:

```bash
cd ../backend
python -m uvicorn app.api:app --port 8000
```

Then open <http://localhost:8000>.

To edit, change `index.html` and refresh. No rebuild.

## What it renders

| Section | Source endpoint |
| :-- | :-- |
| Scenario and persona pickers | `/api/scenarios`, `/api/health` |
| Summary stat row | `/api/analyse` |
| Finding cards, evidence drawers, action cards | `/api/analyse` |
| Upvote / downvote controls | `/api/feedback` |
| Cross-source reconciliation table | `/api/analyse` |
| Source freshness table | `/api/analyse` |
| Revenue waterfall · margin bridge | `/api/decomposition` |
| Cold-start shrinkage curve | `/api/cold-start`, `/api/cold-start/{entity}` |
| Runtime telemetry · FDR detail | `/api/analyse` |

## Design notes

- **It hardcodes no KPI, product or entity id.** Everything is discovered from
  the API, which is the same rule the analysis code follows. The cold-start
  entity comes from `/api/cold-start`, not from a literal in the JavaScript.
- **Theme-aware.** Light and dark are both defined at token level and both are
  verified.
- **Semantic colour is separate from the accent.** Green, amber and red mean
  detected, low-confidence and abstained — not decoration.
- The only external request is Google Fonts, which falls back cleanly offline.
