# Rules catalog

Deterministic SQL checks over canonical tables — no LLM anywhere in this
layer (principle 1). Every finding points at the canonical row it is
about via `source_ref` (principle 2). Implemented in `rules/`, run by
`rules/engine.py`, surfaced as rows in `flags`.

## Severities

- **critical** — money is leaving or already left wrongly; act now.
- **warn** — the books are lying somewhere; fix during the close.
- **info** — pattern worth eyes in review; no action required by itself.

## Flag lifecycle (engine-managed)

Natural key `(client_id, rule_code, source_ref)`. Re-runs never duplicate
open flags; findings that stop firing auto-resolve with
`condition cleared on <date>`; manually dismissed or manually resolved
keys are never reopened (auto-resolved keys may re-fire as a new
occurrence). Sync-owned flags (`transform_warning`, `qbo_deleted`) keep
their own lifecycle and are not touched by the engine.

## Catalog

| Code | Severity | What it catches | Why a construction controller cares |
|---|---|---|---|
| R000 | warn | Journal lines that don't net to zero (or amount with no lines) | Books violating double-entry can't be trusted for ANY downstream number |
| R001 | warn | Lines posted to QBO-deleted or inactive accounts | Balances accumulating where no report looks |
| R010 | critical | Same vendor + same amount (> $100) within 10 days, different txns (Bill+BillPayment pairs excluded as normal flow) | Classic duplicate payment — cash out the door twice |
| R011 | critical | Same vendor + same DocNumber on multiple Bills | Vendor's own invoice number twice = near-certain double entry or re-submitted invoice |
| R012 | warn | JE lines ≥ $1,000 in exact multiples of 1,000 | Round numbers are the signature of plugs and made-up adjustments |
| R013 | warn | Suspense/clearing/Ask-My-Accountant/uncategorized balances aged > 30 days | Parked dollars whose true account nobody knows; compounds into year-end mess |
| R014 | warn | Expense accounts net CREDIT for the close month | Misposted refunds/reversals distorting P&L and job costs |
| R015 | warn | Uncategorized* transactions older than 14 days | Bank-feed activity nobody coded; the P&L is wrong by exactly these amounts |
| R016 | warn | Entries created > 45 days after their stated txn_date (QBO CreateTime vs TxnDate) | History being rewritten into reviewed periods |
| R017 | info | JournalEntries dated Sat/Sun | Out-of-rhythm manual entries; a known fraud-casework marker |
| R020 | warn | Vendor month spend > 2.5× its trailing-6-month average AND > $2,500 | Budget busts, scope creep, duplicate invoicing, vendor fraud — caught against the vendor's own baseline |
| R021 | warn | First-ever vendor transaction ≥ $5,000 | Fake-vendor schemes open big; also catches split vendor records breaking 1099s |
| R022 | info | BillPayments linked to no Bill | A/P misstated; expense may be missing entirely |
| R023 | warn | One customer > 40% of open A/R and > $10,000 | One slow payer away from missing payroll; progress billing makes this chronic |
| R024 | warn | Purchases/Bills ≥ $500 with no vendor | Unattributable spend breaks 1099s, vendor reports, and duplicate detection |
| R030 | warn | COGS debit lines ≥ $250 with no job tag | Every untagged cost dollar corrupts every job's margin — the #1 construction bookkeeping failure |
| R031 | warn | Costs posted to completed/closed jobs | Final margins being rewritten after someone relied on them; hides losses on the job actually paying |
| R032 | critical | Job-to-date costs exceed income for jobs with billings | The job is underwater NOW, while a change order can still fix it |
| R033 | warn | Customer Payments unapplied > 30 days | Deposit/retainage hygiene: unraised invoices, phantom A/R, customers dunned after paying |

## Sync-owned flags (for completeness)

| Code | Severity | Source |
|---|---|---|
| transform_warning | warn | sync/transforms.py — unmappable/unbalanced payload content |
| qbo_deleted | warn | sync/incremental.py — entity deleted in QBO, canonical row soft-flagged |

## Docpipe-owned flags (emitted by the bank rec verifier)

| Code | Severity | What it catches | Why it matters |
|---|---|---|---|
| R040 | critical | Statement lines with no matching QBO transaction (unrecorded bank activity) | Money moved that the books don't know about — THE bank-side fraud/error catch; one flag per line, fuzzy date candidates annotated |
| R041 | warn | QBO bank activity absent from the statement and > 30 days old at period end | Stale uncleared checks and phantom entries inflating the book balance |

## Known scope notes

- R031 relies on curated `jobs.status` ('completed'/'closed' set by
  Carlos) — QBO has no native completion state.
- R033 sees Payment application links only; unapplied CreditMemos await
  link-graph extraction.
- R014/R020 period = calendar month of `as_of`.
