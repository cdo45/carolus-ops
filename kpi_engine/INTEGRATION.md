# kpi_engine — vendored QBO KPI engine

## What this is
The compute engine lifted from the **qbo-kpi-dashboard** tool (branch
`claude/charming-babbage-2bjif2`, v1.0.2). It classifies a QBO chart of
accounts onto a canonical taxonomy, computes liquidity / receivables / revenue
/ disbursement KPIs plus a 13-week cash forecast, and can render a
self-contained HTML dashboard. Computation is driven by taxonomy **category
codes**, never by hard-coded account numbers — so it generalizes across any
company's chart of accounts.

## Why it's here
carolus-ops will feed this engine from its **canonical Postgres** (populated by
the QBO **API** sync) through an adapter, replacing the engine's CSV-export
ingestion. The engine's own account classifier and AR payment-pairing already
solve carolus's two open analytics gaps (chart-of-accounts generalization and
the AR sub-ledger). The build plan is in **`ADAPTER_PROMPT.md`** (a ready-to-run
Claude Code prompt).

## What was copied (and what wasn't)
Vendored as-is: `core/`, `data/`, `dashboard/`, `app/`, `tests/`, `docs/`,
`run.py`, `README.md`, `requirements.txt`, `VERSION`.

Excluded: `scripts/` (PyInstaller packaging) and `.github/` (desktop-app build
CI) — the desktop-delivery shell, which the carolus portal replaces. `app/` and
`run.py` are kept **only** for standalone/manual validation; they are not the
carolus runtime path.

## Status
Vendored intact — the full engine test suite passes (**209 passed**) from this
folder.

## Running the engine's tests
```
cd kpi_engine
pip install -r requirements.txt   # openpyxl, pytest
pytest
```
carolus-ops' root `pytest` skips this subtree via `../conftest.py`
(`collect_ignore = ["kpi_engine"]`), because the engine's tests import its own
top-level `core` package. Run them with the commands above.

## Target runtime path in carolus
Per client and period, the adapter (to be built) populates the engine's ~7
input tables — `accounts`, `transactions`, `ar_aging_snapshots`/`rows`,
`ap_aging_snapshots`/`rows`, `invoice_payments`, `bills_payments` — plus opening
balances, from canonical Postgres; the engine computes; results are written
back into `kpi_values` / `facts` and gated by the review queue before anything
reaches the portal. **Do not** wire `core/parsers` or `core/importer` as the
runtime feed.
