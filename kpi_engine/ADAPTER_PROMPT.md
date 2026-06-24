# Claude Code prompt — build the carolus → KPI-engine adapter

Open the **carolus-ops** repo in Claude Code and paste everything below the line.

---

You are working in the **carolus-ops** repo. A self-contained KPI engine has
been vendored at `kpi_engine/` (the QBO KPI dashboard tool, minus its desktop
shell — see `kpi_engine/INTEGRATION.md`). Your job: feed that engine from
carolus's **canonical Postgres** (populated by the QBO **API** sync) so we get
its KPIs and forecast **without** using its CSV parsers. Build the adapter, the
AR/AP sub-ledger projection it needs, and the write-back into carolus's tables.
Follow the operating principles and engineering standards in `CLAUDE.md`.

## Background — the seam (already verified)
The engine reads **only** from these SQLite tables (exact columns in
`kpi_engine/core/db.py`); the parsers' sole job is to fill them:

- `accounts` (qbo_name, account_number, qbo_type, detail_type, **category**,
  confidence, status, dormant, coa_balance, …). `category`/`confidence` are
  produced by the engine's classifier (`kpi_engine/core/classify.py`) from
  name + qbo_type + detail_type — reuse it, don't reimplement the taxonomy.
- `transactions` (account_id, txn_date, txn_type, num, name, description,
  **amount [SIGNED]**, running_balance, job_prefix)
- opening balances — written as an audit row: `audit_log` entity=`accounts`,
  field=`gl_balances`; read back by `kpi_engine/core/kpi/base.py`.
- `ar_aging_snapshots` / `ar_aging_rows` (customer, invoice_date, due_date,
  num, amount, open_balance, bucket); `ap_aging_*` is the vendor mirror.
- `invoice_payments` (customer, row_type ∈ invoice|payment|credit, date, num,
  amount, group_key); `bills_payments` is the vendor mirror.

Every KPI computes as "transactions joined to accounts, filtered by
`accounts.category`, plus opening balance + activity." AR KPIs additionally read
the latest aging snapshot and `invoice_payments`.

## carolus canonical source (verify columns against `db/migrations/`)
- `clients` (id, qbo_realm_id, …) — one per QBO company; all data is RLS-scoped
  by `client_id`.
- `accounts` (client_id, qbo_id, name, acct_type, acct_subtype, active)
- `entities` (client_id, qbo_id, kind ∈ customer|vendor|employee, name)
- `transactions` (client_id, qbo_id, txn_type, txn_date, amount, entity_id,
  doc_number, qbo_created_at, has_linked_txn, …)
- `journal_lines` (transaction_id, line_no, account_id, job_id, amount,
  posting_type ∈ debit|credit, description)
- `qbo_raw` — raw QBO payloads (source for invoice/payment fields not yet
  canonicalized)
- write targets: `kpi_values` (client_id, kpi_code, period_start, period_end,
  value, source_ref), `facts`, `review_queue`

## Deliverables

### 1. Account + GL adapter — the easy ~70%
Choose a module path consistent with the repo's flat layout (e.g. `analysis/`).
Per `client_id` and reporting period, build a fresh engine SQLite DB using
`kpi_engine/core/db.py`'s schema and populate:
- `accounts` from canonical `accounts`: qbo_name←name, qbo_type←acct_type,
  detail_type←acct_subtype, account_number←QBO AcctNum (from `qbo_raw` if
  present, else NULL). Then run the engine's classifier to fill
  category/confidence.
- `transactions` from `journal_lines` ⋈ `transactions` ⋈ `entities`: one row
  per journal line; `amount` = **signed** (debit → +, credit → −) to match the
  engine's convention (validate — see Watch-items); num←doc_number,
  name←entity.name, plus txn_date, txn_type.
- opening balances: for each account, balance as of (period_start − 1 day) =
  sum of signed journal_lines before period_start; write the `gl_balances`
  audit row in the exact shape `kpi_engine/core/kpi/base.py` reads.

### 2. AR/AP sub-ledger projection — the real ~30%
carolus's canonical schema is GL-centric and does not yet carry invoice-level
detail. Add it via a migration in `db/migrations/`, sourced from QBO payloads in
`qbo_raw` (Invoice: DueDate, Balance, TxnDate, DocNumber, CustomerRef; Bill
similarly; Payment / BillPayment: Line.LinkedTxn with applied amounts). Then in
the adapter:
- populate `ar_aging_rows`/`ar_aging_snapshots` (and AP mirror): invoice_date,
  due_date, open_balance from the projection; bucket = f(due_date, as_of_date).
- populate `invoice_payments`/`bills_payments` using the **explicit** QBO
  payment→invoice links — set `group_key` from the real link so the engine's
  amount-matching is exact. Do **not** rely on the engine's CSV grouping
  heuristic.
Every projected row carries a `source_ref` to its originating QBO id
(Principle 2).

### 3. Write-back — adapter → carolus
After the engine computes (it writes `kpi_history` / returns KPIValues), map
results into:
- `kpi_values` (client_id, kpi_code, period, value, source_ref) — idempotent
  upsert on the unique key.
- low-confidence or unmapped accounts, and any interpretive findings → `facts`
  and/or `review_queue` (Principles 3 & 4). Nothing reaches the portal without a
  review-queue approval.

### 4. Orchestration
A per-client entrypoint that runs steps 1→3 after a sync. Invoke the engine with
`kpi_engine/` on `sys.path` (or as a subprocess) and treat it as a vendored
black box — **do not** refactor its internal `core.` imports. Idempotent:
re-running produces zero duplicates and zero drift.

## Watch-items (each becomes a test)
- **Signs**: confirm debit/credit → signed amount matches what the engine
  expects (it does opening + activity and never flips by category). Reconcile
  the engine's computed trial balance against a known-good QBO Trial Balance for
  one client/period **before** trusting any KPI.
- **Per-client / RLS**: one engine DB per client (the engine assumes a single
  company per DB). Thread `client_id` through every canonical read; respect RLS.
- **Provenance**: engine drilldown citations must reference canonical
  `source_ref`/`qbo_id`, not CSV rows.
- **Opening balances**: an account with no prior activity begins at 0.0 — match
  the engine's convention.

## Standards & gate (from CLAUDE.md)
Python 3.12 + uv; type hints on all functions; a test per module; secrets via
env (Doppler); schema-validated I/O; full idempotency on sync/write. Do not wire
`kpi_engine/core/parsers` or `core/importer` as the runtime feed — they remain
only as reference/fallback. Work in sequential chunks, one concern per commit,
build/test before every push. The engine's own suite must stay green
(`cd kpi_engine && pytest` → 209 passing); add adapter tests under carolus-ops'
`tests/`. Green gates, then ship (Principle 5).

## First step
Before writing code, read `kpi_engine/core/db.py`,
`kpi_engine/core/kpi/base.py`, `kpi_engine/core/classify.py`, and
`db/migrations/0001_init.sql` (plus later migrations), then propose the adapter
module layout and the AR/AP sub-ledger migration for review.
