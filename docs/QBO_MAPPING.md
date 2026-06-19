# QBO → canonical mapping

Implemented by `sync/transforms.py`. Keep this document in lockstep with
`_BUILDERS` there — it is the human-readable contract for how QBO payloads
become canonical rows.

## What gets pulled

| QBO entity | Destination |
|---|---|
| Account | staging + canonical `accounts` |
| Customer | staging + canonical `entities` (kind=customer); `Job=true` also → `jobs` |
| Vendor | staging + canonical `entities` (kind=vendor) |
| Item | **staging only** — reference data: item-based lines carry only `ItemRef`, so the item's `IncomeAccountRef`/`ExpenseAccountRef` is needed to resolve lines to accounts |
| Invoice, Bill, Payment, BillPayment, Purchase, JournalEntry, Deposit, CreditMemo, VendorCredit | staging + canonical `transactions` + `journal_lines` |

Employee sync is deferred to P8 (payroll). A `Purchase` whose `EntityRef`
is an employee therefore gets `entity_id = NULL` until then.

## Header fields

- `transactions.qbo_id` ← `Id`; `txn_type` ← QBO entity name;
  `txn_date` ← `TxnDate`; `amount` ← `TotalAmt` (JournalEntry: sum of
  debit lines, QBO sends no `TotalAmt`).
- `transactions.entity_id` ← header ref: `CustomerRef` (Invoice, Payment,
  CreditMemo), `VendorRef` (Bill, BillPayment, VendorCredit),
  `EntityRef` + its `Type` (Purchase). JournalEntry and Deposit have none.
- `transactions.qbo_synced_at` ← payload `MetaData.LastUpdatedTime`;
  `transactions.qbo_created_at` ← `MetaData.CreateTime`;
  `transactions.doc_number` ← `DocNumber`. All content-derived (NOT
  wall-clock) so identical payloads re-transform to identical rows.
- `doc_status` and `review_tier` are curated columns: sync sets the
  default on insert and never overwrites them. Same for
  `jobs.contract_amount`.

## Jobs (QBO sub-customers)

Every Customer payload (jobs included) becomes an `entities` row so any
transaction header can resolve. Customers with `Job=true` additionally
get a `jobs` row with `entity_id` → the parent customer (`ParentRef`).
Lines inherit `job_id` from the header customer when it is a job
(invoices/credit memos to a job) or from a line-level `CustomerRef`
(billable expenses on bills/purchases).

## Journal-line conventions (debit/credit per type)

Amounts are stored positive; a negative QBO amount flips the posting side.
Zero-amount lines are skipped. `line_no` is the build-order ordinal
(0 = the balancing header line) — a stable identity so re-transforms
upsert in place instead of delete+reinsert.

| Type | Debit | Credit |
|---|---|---|
| Invoice | A/R for `TotalAmt` (also discount lines) | income account per sales line (via Item); sales tax to GlobalTaxPayable |
| CreditMemo | income per sales line (reversal); sales tax to GlobalTaxPayable | A/R for `TotalAmt` |
| Bill | expense account per line | A/P for `TotalAmt` |
| VendorCredit | A/P for `TotalAmt` | expense account per line (reversal) |
| Payment | `DepositToAccountRef`, else Undeposited Funds | A/R |
| BillPayment | A/P | bank (`CheckPayment.BankAccountRef`) or card (`CreditCardPayment.CCAccountRef`) by `PayType` |
| Purchase | expense per line (`Credit=true` flips the whole entry) | payment `AccountRef` |
| Deposit | `DepositToAccountRef` for `TotalAmt` | per `DepositLineDetail.AccountRef`; a line with a `LinkedTxn` and no AccountRef (batched customer payments) credits the Undeposited Funds account |
| JournalEntry | explicit `PostingType` per line | explicit `PostingType` per line |

Line account resolution: `AccountBasedExpenseLineDetail.AccountRef`
directly; `ItemBasedExpenseLineDetail`/`SalesItemLineDetail` via the
staged Item's expense/income account. A/R / A/P fall back from explicit
`ARAccountRef`/`APAccountRef` to the client's single account of type
"Accounts Receivable"/"Accounts Payable" (ambiguity → warning, see below).

## Sales tax

`TxnTaxDetail.TotalTax` maps to the client's single
`acct_subtype='GlobalTaxPayable'` account: credited on Invoices, debited
on CreditMemos (reversal), appended after the item lines so its line_no
is stable. Zero or multiple candidate accounts → transform_warning,
never guessed.

## Line-level job tags (all shapes)

`AccountBasedExpenseLineDetail`/`ItemBasedExpenseLineDetail` via
`CustomerRef`; `JournalEntryLineDetail` via `Entity.EntityRef`;
`DepositLineDetail` via `Entity` — all resolve through the same jobs
lookup. Header-customer inheritance for Invoices/CreditMemos unchanged.

## Repair path

`uv run python -m sync.retransform --realm <id>` rebuilds canonical from
staging with no QBO calls. A transaction that now builds clean
auto-resolves its open transform_warning
(`repaired by re-transform on <date>`).

## Known gaps (flagged, not silent)

- **`SubTotalLineDetail` / statement-charge invoices** — an invoice whose
  only income line is a `SubTotalLineDetail` (a legacy QBO statement charge,
  linked to a `StatementCharge`) does not balance: the subtotal line carries
  an `Amount` but no `ItemRef`/`AccountRef`, so its income account is only
  resolvable by following the linked `StatementCharge`, a deprecated entity
  outside the sync set. Sales tax posts normally (curated GlobalTaxPayable);
  the income line does not, so the txn retains a correct open
  `transform_warning`. Deliberately unsupported. (Sandbox: Invoice 42.)

## Warnings / never silently wrong

Anything unresolvable (unknown item, missing A/R, unhandled `PayType`,
unbalanced totals) writes what it can and creates ONE open flag per
transaction: `rule_code='transform_warning'`, `source_ref='qbo:<Type>:<Id>'`,
details concatenated. The phase-1 gate accepts a transaction only if its
lines net to zero OR it carries such a flag.

## Idempotency mechanics

- Staging (`qbo_raw`) is an append-only log; transforms read the latest
  payload per `(entity_type, qbo_id)`, so re-transforms never need QBO.
- Canonical upserts key on `(client_id, qbo_id[, kind/txn_type])` with
  `ON CONFLICT ... DO UPDATE ... WHERE (old columns) IS DISTINCT FROM
  (new columns)` — unchanged data writes zero rows, verified by the gate.
- Flags insert only when no open flag with the same
  `(client_id, rule_code, source_ref)` exists.
