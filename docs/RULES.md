# Rules catalog — v2 (controller audit, 2026-06-11)

Deterministic SQL checks over canonical tables — no LLM anywhere in this
layer (principle 1). Every finding points at the canonical row it is
about via `source_ref` (principle 2). Implemented in `rules/`, run by
`rules/engine.py`, surfaced as rows in `flags`.

v2 recalibrates thresholds against a working controller's noise budget:
every dollar floor and ratio below was tuned so that a flag is worth the
review minutes it costs. Thresholds are global constants today;
**per-client overrides are a Phase 5+ direction** (the constants are
already module-level for exactly that reason).

## Severities

- **critical** — money is leaving or already left wrongly; act now.
- **warn** — the books are lying somewhere; fix during the close.
- **info** — pattern worth eyes in review; no action required by itself.

Rules may grade per finding (R032): the `Finding.severity` override is
validated against the same vocabulary; an open flag keeps the severity it
was born with until it resolves.

## Flag lifecycle (engine-managed)

Natural key `(client_id, rule_code, source_ref)`. Re-runs never duplicate
open flags; findings that stop firing auto-resolve with
`condition cleared on <date>`; manually dismissed or manually resolved
keys are never reopened (auto-resolved keys may re-fire as a new
occurrence). Sync-owned and docpipe-owned flags keep their own lifecycle
(`transform_warning` additionally auto-resolves as
`repaired by re-transform on <date>` when a re-transform rebuilds the
transaction clean).

## Catalog

| Code | Severity | What it catches (v2 thresholds) | Why / recalibration rationale |
|---|---|---|---|
| R000 | warn | Journal lines that don't net to zero (or amount with no lines) | Books violating double-entry can't be trusted for ANY downstream number |
| R001 | warn | Lines posted to QBO-deleted or inactive accounts | Balances accumulating where no report looks |
| R010 | critical | Same vendor + amount **> $500** within 10 days, different txns; Bill+BillPayment pairs excluded; vendors with **≥3 identical amounts/12mo suppressed** | Duplicate payment = cash out twice. v2: sub-$500 was small-purchase noise; 3+ identical amounts is a subscription, not a duplicate |
| R011 | critical | Same vendor + same DocNumber on multiple Bills; **doc numbers ≤2 chars or na/n-a/none/-/. ignored** | Vendor's own reference twice = near-certain double entry. v2: placeholder doc numbers matched constantly |
| R012 | warn | JE lines **≥ $5,000** in exact multiples of 1,000 | Round numbers are plug/estimate signatures. v2: $1k–$4k band was legitimate small accruals |
| R013 | warn | Aged (>30d) balances in suspense/clearing/Ask-My-Accountant/uncategorized/**Opening Balance Equity/Reconciliation Discrepanc%** accounts | Parked dollars nobody owns. v2: added QuickBooks' own two parking spots |
| R014 | warn | Expense accounts net CREDIT **> $250** for the close month | Misposted refunds/reversals distort P&L and job costs. v2: floor keeps rounding/timing noise out |
| R015 | warn | Uncategorized* transactions older than 14 days | Bank-feed activity nobody coded |
| R016 | warn | Entries created > 45 days after stated txn_date; **Bill/BillPayment exempt** | History rewriting. v2: vendor invoices legitimately arrive weeks late — that's mail, not manipulation |
| R017 | info | JournalEntries dated Sat/Sun | Out-of-rhythm manual entries; fraud-casework marker |
| R018 | critical | Transaction in a **green-closed period** modified after the close was evaluated | NEW v2: a green close is a promise; edits after it invalidate verified numbers |
| R020 | warn | Vendor month spend **> 3×** trailing-6-month average AND **> $5,000**, with **activity in ≥3 trailing months** | Budget busts/vendor fraud vs the vendor's own baseline. v2: one prior purchase isn't a baseline (new-vendor money is R021's job) |
| R021 | warn | First-ever vendor transaction ≥ $5,000 | Fake-vendor schemes open big; split vendor records break 1099s |
| R022 | info | BillPayments linked to no Bill | A/P misstated; expense may be missing |
| R023 | **info** | One customer **> 50%** of open A/R and **> $25,000** | v2: concentration is advisory-call context, not a close defect — downgraded and thresholds raised |
| R024 | warn | Purchases/Bills ≥ $500 with no vendor; **employee-payee Purchases exempt** (raw EntityRef read from staging) | Unattributable spend breaks 1099s. v2: employee reimbursements have a payee in QBO, just not canonical until P8 |
| R025 | warn | Invoices **>90 days** old with QBO Balance **≥ $1,000** | NEW v2: receivables age like fish; lien deadlines pass. Balance read from staged payload (documented approximation until the link graph lands) |
| R026 | warn | Bills **>60 days** old with QBO Balance **≥ $1,000** | NEW v2: aged payables cost sub/supplier relationships and invite liens. Same staged-Balance approximation |
| R027 | info | ACTIVE vendor name pairs ≥0.7 trigram-similar; one finding per anchor (lesser uuid), partners aggregated | NEW v2: split vendor history breaks 1099 totals and every vendor-keyed rule |
| R028 | critical | Bank account book balance **< 0** at month-end | NEW v2: either actually overdrawn (cash emergency) or the books lie — both same-day issues |
| R030 | warn | COGS debit lines **≥ $500** with no job tag | The #1 construction bookkeeping failure. v2: floor raised; the sub-$500 aggregate leak is R034's job |
| R031 | warn | Costs posted to completed/closed jobs; **14-day grace after `jobs.completed_at`** (null completed_at = no grace) | Final margins being rewritten. v2: punch-list costs within two weeks are close-out, not rewriting |
| R032 | critical/info | Negative job margin (billed jobs only). **Critical** when costs >110% of billings AND first cost ≥45 days old; **info** otherwise | THE number an owner pays for. v2: deep-and-mature = loss forming now; merely-negative on a young job = billing lag, watch only |
| R033 | warn | Customer Payments unapplied > 30 days, **≥ $500** | Deposit/retainage hygiene. v2: sub-$500 remnants are partial-payment rounding |
| R034 | critical | Month's untagged COGS **> 5%** of total COGS (total ≥ $5,000); ONE summary flag per client+month | NEW v2: the slow bleed R030's floor can't see — fifty small untagged purchases corrupt every margin. Fix is process, hence one flag |
| R035 | warn | Job with cost activity in trailing 30d, JTD costs ≥ $10,000, zero invoices in trailing 30d | NEW v2: underbilling = financing the customer by accident; catch the undrafted progress bill THIS month |

## Sync-owned flags (for completeness)

| Code | Severity | Source |
|---|---|---|
| transform_warning | warn | sync/transforms.py — unmappable/unbalanced payload content; auto-resolves when a re-transform rebuilds the txn clean |
| qbo_deleted | warn | sync/incremental.py — entity deleted in QBO, canonical row soft-flagged |

## Docpipe-owned flags (emitted by the bank rec verifier)

| Code | Severity | What it catches | Why it matters |
|---|---|---|---|
| R040 | critical | Statement lines with no matching QBO transaction (unrecorded bank activity) | Money moved that the books don't know about — THE bank-side fraud/error catch; one flag per line, fuzzy date candidates annotated |
| R041 | warn | QBO bank activity absent from the statement and > 30 days old at period end | Stale uncleared checks and phantom entries inflating the book balance |

## Known scope notes

- R025/R026 read per-document open balances from the latest STAGED
  payload's `Balance` (QBO's own number) — an explicit approximation,
  bounded by one sync cycle, until payment-application links are
  extracted into canonical.
- R024's employee exemption likewise reads the raw `EntityRef` type from
  staging (employees are canonical only from P8).
- R031 relies on curated `jobs.status` + `jobs.completed_at` (set by
  Carlos; sync never writes either).
- R014/R020/R028/R034 period = calendar month of `as_of` (R028 measures
  at month-end).
- Per-client threshold overrides: Phase 5+ — constants are module-level
  and named for exactly that migration.
