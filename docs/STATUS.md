# Build Status

As of 2026-10-03 · code state: `main` at `518f805` (2026-10-03) and the
unmerged branch `analysis/kpi-engine-gl-feed` (2026-06-24).

This is the repo copy of the build-status doc; a shareable copy with diagrams
lives at <https://claude.ai/code/artifact/8b27fe95-f6f0-48a1-b25e-3a5f27c8e73a>.
Update both when a phase gate passes or a Phase 5 item changes status.

## Summary

The foundation is complete and certified, the Phase 5 operating loop is partly
built, and nothing client-facing exists yet. Phases 0–4 have all passed their
executable gates. Phase 5 has its deterministic backbone; its scheduler,
Claude's analysis step, email intake, bookkeeping automation and reporting pack
are still ahead.

No client is taken on until the Phase 7 dress rehearsal passes.

## What's been built

Phases 0–4 form a deterministic foundation with no AI in it, so every number it
produces can be checked.

| Phase | What it does | What it achieves |
| --- | --- | --- |
| P0 Rails | Repo, operating principles, migration runner, and CI that runs the full suite against Postgres on every push | A fresh clone builds and tests itself |
| P1 Sync | Connects to QuickBooks with encrypted, self-healing tokens; logs raw payloads; translates them into accounts, vendors, customers, jobs, transactions and journal lines | An exact, rebuildable copy of each client's books; re-running a sync writes zero rows |
| P2 Rules | 26 controller-grade checks, each pointing at the row behind it: duplicate payments, round-number plugs, closed-period edits, stale receivables and payables, job costs outrunning billings; plus a monthly close checklist | Senior-accountant review of every client's books, every night, at no marginal cost |
| P3 Knowledge | Append-only fact store; every fact must cite a real source; a four-stage validator rejects fabricated sources, duplicates and prompt injection; byte-identical client profile and issues log | A safe landing zone for Claude's findings — though nothing calls Claude yet |
| P4 Documents | Duplicate-aware intake; statement extraction that must tie to the penny; bank reconciliation; receipt matching that refuses to guess; a "we need backup for these" list | Documents enter the books without ever being silently wrong |

Phase 5 has added the operating loop's deterministic backbone:

- **Database-enforced client separation.** Postgres row-level security hides
  each client's data from every other client, including journal lines, which
  are scoped through their parent transaction. A blank client setting denies
  cleanly instead of erroring.
- **One review queue for Carlos.** Open flags flow in automatically and close
  themselves when they clear.
- **Triage.** Dismissing or resolving a flag closes it at the source; the
  nightly run never reopens it.
- **`run_nightly`.** One call runs a client's cycle — sync, rules, queue,
  close — inside the client wall, and records the outcome.
- **`gate_phase5`.** A live end-to-end check that plants a known error and
  follows it through the real loop.
- **Two production bugs fixed:** batched deposits that didn't balance, and a
  connection leak that would have hung the second client in every nightly
  batch.

The **KPI engine** — the standalone QBO KPI Dashboard, v1.0.2 — was vendored on
2026-06-23 as the FP&A compute engine. It computes liquidity, receivables,
revenue and disbursement KPIs plus a 13-week cash forecast in three scenarios.
It still reads CSV exports; feeding it from the synced database is under way,
with the first part (chart of accounts, general ledger, a trial-balance
reconciliation check) on the unmerged branch `analysis/kpi-engine-gl-feed`. See
[`kpi_engine/INTEGRATION.md`](../kpi_engine/INTEGRATION.md).

**By the numbers:** 17 database migrations, 26 rules ([catalog](RULES.md)),
5 phase gates, 273 tests in the main suite and 209 in the KPI engine, all
passing on `main`.

## What it achieves today

The system can watch a client's books, but it cannot yet act, think or talk to
clients. It pulls each client's books exactly, checks them the way a controller
would, ties documents to the penny, remembers what it learns, and routes
anything needing judgment into one queue — with client separation enforced by
the database.

What it can't do yet:

- Run on its own — nothing schedules `run_nightly`
- Analyze with Claude — the analysis step is not in the loop
- Read client email or post entries back to QuickBooks
- Produce the monthly reporting pack
- Show a client anything — the portal is Phase 6

## What's left

Three phases stand between the build and the first client: finish Phase 5,
build the portal, and pass the dress rehearsal.

| Phase | Status | Gate |
| --- | --- | --- |
| P0–P4 Foundation | Certified | Zero-duplicate sync, seeded errors caught, only sourced facts, statements tie to the penny |
| P5 Routines + review queue | In progress | Five unattended nights — blocked until a scheduler runs the loop |
| P6 Client portal (`carolus-advisory` repo) | Not started | A cross-tenant attack fails closed; passes on mobile |
| P7 Dress rehearsal | Not started | Outputs verifiably correct with under three hours of Carlos's time |
| **First paying client** | Sales gate | Sell only after the dress rehearsal passes |
| P8 Payroll | After first clients | Totals tie to Gusto to the penny; WH-347 output matches a known-good sample |

Full phase plan: [ROADMAP.md](ROADMAP.md).

**Finishing Phase 5.** The roadmap's gate is five unattended nights, so the
scheduler comes first. The specification also scopes email, bookkeeping and the
reporting pack into Phase 5.

| Item | Status | Detail |
| --- | --- | --- |
| Live `gate_phase5` run, `PASS (4/4)` | Needs live run | Built; needs an operator run against the sandbox |
| Scheduler: GitHub Actions cron running `run_nightly` for every client | Not started | Unblocks the phase gate |
| Five unattended nights, the roadmap's Phase 5 gate | Blocked | Needs the scheduler |
| Monitoring and alerting (Sentry, UptimeRobot) | Not started | Failures page Carlos instead of passing silently |
| Claude analysis step in the nightly | Not started | Read context and open issues, analyze, write facts, draft a brief and client emails |
| Agent action log | Not started | Every action, why, confidence and which run, append-only |
| Email intake | Not started | One `books@` address, automatic sender-to-client matching, drafted replies |
| Bookkeeping automation | Not started | Categorize transactions; auto-post, suggest or escalate; post back to QuickBooks |
| FP&A monthly pack | In progress | KPI engine vendored; database feed part 1 on a branch; AR/AP detail, write-back and orchestration to do |
| WIP schedule and gross-profit fade by job | Not started | The construction moat; the KPI engine has no WIP logic |
| Fractional-CFO call prep | Not started | A pre-call brief with scenario models |
| Per-client rule thresholds | Not started | Tune a messy new client and a clean mature one separately |
| Row-backed record for Carlos's own notes | Not started | An annotations table, so `carlos:` provenance resolves to a row |

The WIP schedule is the gap to watch. Two rules touch its data, but the report
banks and bonding agents ask for does not exist yet.

**Phases 6 and 7.** The portal lives in the separate `carolus-advisory` repo.
It also needs the document-storage backend (only its interface exists), logins
for the two restricted database roles, and transactional email. Phase 7 adds a
read-only shadow run on one real client's books before any write-to-QuickBooks
path is trusted; so far the system has seen only sandbox data.

**Before the first client:**

- [ ] E&O insurance with a cyber rider
- [ ] AI-disclosure clauses in the engagement letter
- [ ] One-page client security summary
- [ ] Stripe billing, ACH only
- [ ] Pricing validated against the local construction market

## The goal once complete

One construction-finance expert carries 10–15 clients at controller-grade
quality, each mature client taking two hours or less of Carlos's time a month.

Every night, for every client, the loop syncs books and email, checks
everything and updates what it knows. Claude then analyzes what changed and
drafts what's needed into one queue. Carlos approves; clients see the portal,
and nothing leaves the building without his click.

| Stage of the nightly loop | Status |
| --- | --- |
| QuickBooks sync, documents, rules + close checklist, knowledge layer | Built |
| Client email intake | Not built |
| Claude analysis, nightly | Not built — today, flags go straight from the rules to the queue |
| Review queue, where Carlos approves | Built |
| Outputs: client portal, drafted client emails, entries posted to QuickBooks | Not built |

The target client is a construction company doing $1–10M a year on QuickBooks
Online. The edge is construction-specific: WIP schedules, gross-profit fade by
job, and certified payroll.

| Tier | Includes | Indicative monthly fee |
| --- | --- | --- |
| Tier 1 | Bookkeeping and the monthly reporting pack | $600–900 |
| Tier 2 | Tier 1 plus FP&A, the WIP schedule and a 13-week forecast | $1,200–1,800 |
| Tier 3 | Tier 2 plus fractional-CFO calls | $2,500–4,000 |
| Add-on | Certified payroll, per client | $200–400 |

Pricing is indicative and still to be validated against the local market.
Selling starts only after the Phase 7 dress rehearsal passes; from then on, the
binding constraint is client acquisition, not software. Full specification:
[Carolus_Advisory_Product_Specification.md](../Carolus_Advisory_Product_Specification.md).

## Open housekeeping

- **CI is green again.** PR #2 (merged 2026-10-03) fixed the lint step and
  three date-dependent tests that had kept `main` red since 2026-06-24.
- **Docs have drifted.** The product specification (last updated 2026-06-18)
  still lists Phase 5 as only specified, with ~238 tests, 12 migrations and
  row-level security as future work. The roadmap's Phase 2 section still says
  15 seeded errors (21 since rules v2) and has no run instructions for Phase 5.
  The README's repo map omits `kpi_engine/`.
- **One unmerged branch:** `analysis/kpi-engine-gl-feed`, 3 commits ahead of
  `main` and 4 behind.
