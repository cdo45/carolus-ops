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
- `transactions.qbo_synced_at` ← payload `MetaData.LastUpdatedTime` —
  content-derived (NOT wall-clock) so identical payloads re-transform to
  identical rows.
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
| Invoice | A/R for `TotalAmt` (also discount lines) | income account per sales line (via Item) |
| CreditMemo | income per sales line (reversal) | A/R for `TotalAmt` |
| Bill | expense account per line | A/P for `TotalAmt` |
| VendorCredit | A/P for `TotalAmt` | expense account per line (reversal) |
| Payment | `DepositToAccountRef`, else Undeposited Funds | A/R |
| BillPayment | A/P | bank (`CheckPayment.BankAccountRef`) or card (`CreditCardPayment.CCAccountRef`) by `PayType` |
| Purchase | expense per line (`Credit=true` flips the whole entry) | payment `AccountRef` |
| Deposit | `DepositToAccountRef` for `TotalAmt` | per `DepositLineDetail.AccountRef` |
| JournalEntry | explicit `PostingType` per line | explicit `PostingType` per line |

Line account resolution: `AccountBasedExpenseLineDetail.AccountRef`
directly; `ItemBasedExpenseLineDetail`/`SalesItemLineDetail` via the
staged Item's expense/income account. A/R / A/P fall back from explicit
`ARAccountRef`/`APAccountRef` to the client's single account of type
"Accounts Receivable"/"Accounts Payable" (ambiguity → warning, see below).

## Known gaps (flagged, not silent)

- Sales tax (`TxnTaxDetail`) is not mapped in P1 — invoices with tax come
  out unbalanced and are flagged.
- Job attribution inside JournalEntry/Deposit lines is not mapped in P1.

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
