# Carolus Advisory — Product & Technical Specification

**Document type:** Product Requirements Document (PRD) / Technical Specification
**Product:** Carolus Advisory automated accounting & advisory platform
**Owner:** Carlos D'Onofrio
**Status:** Living document — Phases 0–4 built and certified; Phases 5–8 specified, not yet built
**Last updated:** 2026-06-18

---

## 1. Executive Summary

Carolus Advisory is an automated accounting and advisory platform that lets a single construction-finance professional deliver bookkeeping, FP&A, fractional-CFO, and (later) payroll services to a book of clients at a fraction of the manual labor those services normally require. The system syncs each client's QuickBooks Online books into a normalized database, runs deterministic checks against them every night, maintains a continuously-updated knowledge file about each client, processes their documents and email, and surfaces everything needing human judgment into a single review queue.

The product's purpose is leverage: to make one expert capable of carrying 10–15 client relationships at controller-grade quality by automating the work that doesn't require judgment and concentrating the operator's time on the work that does.

**Target client.** Small-to-mid construction companies ($1–10M revenue) running their books in QuickBooks Online — general contractors and specialty subcontractors whose owners are doing their own bookkeeping badly or paying a generalist who doesn't understand construction accounting (WIP, job costing, retainage, certified payroll).

**Differentiator.** The construction-specific intelligence layer. Generic bookkeeping-automation tools don't produce controller-grade WIP schedules, gross-profit-fade analysis, or certified payroll. Carolus does, because the operator's domain expertise is encoded directly into the rules engine and reporting layer.

---

## 2. Core Design Principles

These five principles govern every design decision and are enforced in code, not by convention. They live verbatim in the project's root instruction file.

1. **Claude interprets, scripts extract, math validates, the human approves.** Judgment comes from the LLM layer; numbers come from deterministic code; nothing crosses between them without a passing check. A model reads and reasons; it never does arithmetic that matters or writes data directly.

2. **Provenance or it doesn't exist.** No fact, flag, or posted entry exists without a `source_ref` pointing at the specific row, document, or message it came from. Validators enforce this structurally — hallucinated provenance is rejected by the database layer, not discouraged by the prompt.

3. **The queue is the job.** Everything requiring the operator appears in one review queue. Anything not in the queue, the machine owns. If the operator is working outside the queue, that is a design defect.

4. **Risk-gated autonomy.** Internal analysis runs fully automatically. Posting to the books is confidence-tiered. Anything leaving the building — client emails, published reports, payments — requires a human click, always.

5. **Green gates, then sell.** No phase advances without its test gate passing as executable code. The operator begins client acquisition only after the end-to-end dress rehearsal passes.

**Two supporting product axioms:**

- **Smart at the analysis layer, bulletproof at the data layer.** Machine learning / LLM reasoning lives where judgment lives (reading flagged items, drafting narratives, extracting context), always wrapped in validators. Data collection is purely deterministic so it cannot hallucinate — a checksum can't make things up.
- **Grandpa-easy on the client side.** The client-facing experience must be usable by a non-technical contractor from a job site: magic-link login, one email address for everything, a portal with five pages and no accounting jargon.

---

## 3. System Architecture

The system is split into two planes with different runtimes and billing models.

### 3.1 Intelligence Plane

Everything the machine *does*: sync, rules, knowledge, documents, analysis, report generation. A Python pipeline operating on a canonical Postgres database. Runs on the operator's Claude Max subscription via scheduled Claude Code routines — zero marginal LLM cost.

### 3.2 Presentation Plane

What clients *see*: a Next.js web portal that reads finished outputs from the same Postgres. The portal does **not** call the LLM — it is a display layer over data the pipeline already produced. Multi-tenant isolation via Clerk organizations plus Postgres row-level security.

### 3.3 Data flow

```
QuickBooks Online (per client)
        │  OAuth2, full sync + incremental CDC
        ▼
Raw JSONB staging  ──►  Deterministic transforms  ──►  Canonical Postgres
        │                                                     │
        │                          ┌──────────────────────────┤
        ▼                          ▼                          ▼
  Document pipeline          Rules engine              Knowledge layer
  (intake, checksum,        (26 deterministic         (facts, validator,
   bank rec, matching)       checks, flags)            CONTEXT/ISSUES render)
        │                          │                          │
        └──────────────┬───────────┴──────────────────────────┘
                       ▼
              Nightly routines (Claude Code, Max plan)
                       │  analysis, narratives, draft outputs
                       ▼
              Single review queue  ◄── operator reviews & approves
                       │
                       ▼
              Client portal (read-only)  +  Drafted client emails (human sends)
```

---

## 4. Technology Stack

| Layer | Technology | Status |
|---|---|---|
| Pipeline language/runtime | Python 3.12+ (3.14 local), `uv` package manager | Built |
| Database | Neon Postgres (serverless), homegrown ordered-SQL migration runner (no ORM) | Built |
| Source accounting system | QuickBooks Online API v3 (OAuth2, sandbox + production switch) | Built |
| Core libraries | psycopg 3, pydantic, cryptography (Fernet), pypdf, reportlab, requests | Built |
| Testing/lint | pytest, ruff; full suite in CI against a Postgres service container | Built |
| Document storage | Cloudflare R2 (interface built, backend stubbed) | Interface built |
| AI runtime — automation | Claude Code cloud routines (operator's Max subscription) | Planned (P5) |
| AI runtime — in-app (future) | Anthropic API (portal client chat, v2 only) | Future |
| Web app / portal hosting | Vercel Pro | Planned (P6) |
| Scheduled deterministic jobs | GitHub Actions cron | Planned (P5) |
| Auth | Clerk (organizations, magic link, invite-only) | Planned (P6) |
| Payments | Stripe Billing, ACH-only | Planned |
| Email (intake) | Google Workspace on the domain | Planned (P5) |
| Email (transactional) | Resend | Planned (P6) |
| Payroll partner | Gusto (accountant partner program) | Planned (P8) |
| Secrets | Doppler / 1Password | In use |
| Monitoring | Sentry, UptimeRobot | Planned (P5) |
| Repos | `carolus-ops` (private pipeline), `carolus-advisory` (site + portal) | Built / Planned |

Deliberate architectural choice: **no servers to maintain** — no VPS, Docker, or Kubernetes. Everything runs on managed services that page the operator when something breaks. The operator's scarce resource is review time; infrastructure babysitting is review time with worse ROI.

---

## 5. Feature Specification by Domain

### 5.1 Data Ingestion & Sync — **BUILT**

- **OAuth2 connection flow** with a one-time browser consent per client; Fernet-encrypted token vault; automatic access-token refresh that persists the rotated refresh token *before* returning to any caller (a crash mid-refresh cannot strand a connection).
- **QBO API client** with pagination, 401-triggered single refresh + retry, and exponential backoff on rate limits and server errors.
- **Two-stage sync.** Full sync and incremental Change-Data-Capture sync land untouched QBO payloads in a raw JSONB staging table; deterministic transforms then map staging into the canonical model. Re-transforms never require re-calling QBO.
- **Idempotency by construction.** Every upsert is change-guarded (`ON CONFLICT ... DO UPDATE ... WHERE existing IS DISTINCT FROM new`), so an unchanged re-sync writes zero rows.
- **Canonical model** abstracts away QBO entirely — the rules engine, knowledge layer, and portal operate on a platform-agnostic schema. A second source system (e.g. Xero) would be a new adapter, not a rewrite.
- **Sales tax mapping** to a per-client curated tax-liability account (handles the common case of multiple candidate tax agencies without guessing).
- **Job-cost attribution** on journal-entry and deposit lines (how construction labor reaches job costs in QBO).
- **"Flagged, not silent"** — any payload the transforms can't fully resolve writes what it can and raises an explanatory warning flag rather than dropping data or guessing.

### 5.2 Rules Engine — **BUILT**

26 deterministic checks (pure SQL/Python, no LLM) over the canonical books, each emitting findings with mandatory provenance. An engine manages flag lifecycle: idempotent re-runs, auto-resolution with a dated note when a condition clears, and no reopening of manually-dismissed flags. Thresholds were calibrated in a controller audit (catalog "v2").

**Severities:** *critical* = money is leaving or already left wrongly, act now; *warn* = the books are lying somewhere, fix during the close; *info* = pattern worth eyes, no action required by itself.

| Code | Severity | What it catches |
|---|---|---|
| R000 | warn | Journal lines that don't net to zero (double-entry violations) |
| R001 | warn | Lines posted to deleted/inactive accounts |
| R010 | critical | Duplicate payment — same vendor + amount > $500 within 10 days (recurring-charge vendors exempted) |
| R011 | critical | Same vendor invoice (DocNumber) entered twice (junk doc-numbers ignored) |
| R012 | warn | Round-number journal entries ≥ $5,000 in exact multiples of 1,000 |
| R013 | warn | Suspense / clearing / Ask-My-Accountant / Opening Balance Equity / Reconciliation-Discrepancy balances aged > 30 days |
| R014 | warn | Expense accounts net credit ≥ $250 for the close month (misposted refunds/reversals) |
| R015 | warn | Uncategorized transactions older than 14 days |
| R016 | warn | Entries created > 45 days after their transaction date (Bills/BillPayments exempt — vendor paper arrives late legitimately) |
| R017 | info | Journal entries dated on weekends |
| R018 | critical | Edits to transactions in an already-closed period (rewriting reviewed history) |
| R020 | warn | Vendor monthly spend > 3× its trailing-6-month average AND > $5,000, vendor active ≥ 3 of last 6 months |
| R021 | warn | First-ever transaction with a brand-new vendor ≥ $5,000 |
| R022 | info | BillPayments linked to no Bill |
| R023 | info | One customer > 50% of open A/R and > $25,000 (concentration risk) |
| R024 | warn | Purchases/Bills ≥ $500 with no vendor (employee-paid purchases exempt) |
| R025 | warn | Invoices open > 90 days, balance ≥ $1,000 (stale A/R) |
| R026 | warn | Bills unpaid > 60 days, ≥ $1,000 (A/P aging; lien-rights exposure) |
| R027 | info | Active vendor records with near-duplicate names (defeats duplicate detection and splits 1099s) |
| R028 | critical | Bank-type account with negative book balance at month-end |
| R030 | warn | COGS lines ≥ $500 with no job tag |
| R031 | warn | Costs posted to completed/closed jobs (14-day closeout grace) |
| R032 | critical/info | Job-to-date costs exceed billings; critical when costs > 110% of billings and job is ≥ 45 days old, else info |
| R033 | warn | Customer payments unapplied > 30 days, ≥ $500 (deposit/retainage hygiene) |
| R034 | critical | Month's untagged COGS exceeds 5% of total COGS (one summary flag — job costing is going blind) |
| R035 | warn | Job accumulating costs ≥ $10,000 with zero invoices in the trailing 30 days (underbilling watch) |

Plus two sync-owned flags (`transform_warning`, `qbo_deleted`) with their own lifecycle.

**Monthly close checklist.** Per client and period, evaluates named conditions (no open critical flags, suspense zeroed, no stale uncategorized, bank reconciliations tied) and refuses to go green on any unverified condition. This is the operator's malpractice backstop — no monthly deliverable generates until the close passes.

**Per-client threshold overrides** are specified as a Phase 5+ capability — thresholds will move into client configuration so a messy new client and a clean mature one can be tuned independently.

### 5.3 Knowledge Layer — **BUILT**

The system's memory about each client. Because the model is stateless, the *database* remembers and the pipeline reads/updates it every run.

- **Fact store** — append-only and supersession-based. Facts are never edited or deleted; a changed fact supersedes the old one, which is retained, giving a queryable history of how the operator's understanding of each client evolved (also an audit/defense file).
- **Mandatory provenance.** Every fact carries a `source_ref` to a real email, transaction, or document. A fact whose source can't be resolved cannot be written.
- **Four-stage validation gate** — every proposed fact passes schema → referential-integrity → supersede-integrity → near-duplicate (trigram similarity) checks before anything is written. Nothing bypasses the gate. Hallucinated provenance is rejected structurally.
- **Deterministic rendering.** `CONTEXT.md` (the living client profile, organized by category) and `ISSUES.md` (open flags by severity + recently-resolved) are rendered byte-identically from the same database state — same rows in, identical file out.
- **Fact categories:** entity profile, operations, accounting policy, relationships, preferences, watch items, resolved history.
- **Extraction prompt (versioned).** The LLM extraction prompt enforces atomic / durable / sourced / dated criteria and includes **prompt-injection defense** — client content is treated as untrusted data, never instructions; any embedded directive is routed to an `uncertainties` field instead of acted upon. Combined with the downstream gate, a prompt injection in a client email cannot write a fact.

### 5.4 Document Pipeline — **BUILT**

- **Three intake channels:** documents attached to transactions in QBO (pulled via API), portal uploads, and email attachments — all converging on one classifier.
- **Content-addressed intake** (sha256) — duplicate uploads resolve to the same document; re-ingesting any file writes zero new rows.
- **Classification** — deterministic-first (filename, mime, keyword heuristics) with an LLM fallback for ambiguous cases, escalating when still uncertain.
- **The accuracy hierarchy** — always source from the highest-fidelity tier available: structured data (CSV/OFX, bank feed) > text-layer PDF (deterministic extraction) > scanned image / photo (LLM vision + OCR). Never OCR what can be downloaded as structured data.
- **Statement checksum (the financial-accuracy backstop).** Extracted bank statements must satisfy *beginning + credits − debits = ending* to the penny (exact `Decimal`) and match any stated transaction count. Failure escalates `checksum_failed` and **nothing** from that document enters any downstream table. Image-only statements escalate `needs_ocr`. No extracted number enters the books without passing a deterministic check.
- **Bank reconciliation** — matches statement lines against canonical transactions (exact amount, date window), produces a reconciliation run, and flags unrecorded bank activity (the fraud/error catch) and uncleared aging. A clean rec contributes a green to the close checklist.
- **Receipt/invoice matching** — validated documents match to transactions on amount + date + vendor; one candidate matches and backs the transaction, multiple candidates escalate as ambiguous (never guesses), zero candidates escalate as orphan.
- **Client request list** — undocumented transactions and unmatched documents surface as a generated "we need backup for these" list (the future portal Requests page reads exactly this), turning the document chase into client self-service.

### 5.5 Email Intake — **SPECIFIED (P5)**

- Google Workspace on the firm domain; a `books@` intake address plus the operator's personal address, both read by the pipeline.
- **Sender-to-client matching** by domain against the client roster — clients are told to send everything (receipts, statements, questions) to one address; the pipeline identifies the client automatically. No per-client setup.
- **Per message:** classify (document / question / context / action), extract durable context into the knowledge layer, and draft a response into the review queue.
- **Drafts, not sends** — every substantive reply is a draft the operator approves. The only auto-send candidate (eventually) is a "received, filed ✓" acknowledgment.
- **Disclosure** — engagement letters state that communications are processed with AI-assisted systems under the operator's supervision.
- Side benefit: every client email is timestamped, classified, and logged — an instant answer to "I sent you that in March."

### 5.6 Routines & Review Queue — **SPECIFIED (P5)**

- **Nightly per-client routine** (Claude Code cloud, Max plan): ensure schema current → sync QBO + pull scoped email → run deterministic checks → read knowledge + open issues → analyze new flags, KPI movement, and email context → write back to the knowledge layer, append to the issues log, drop a dated brief, and draft any client emails (as drafts).
- **Single review queue** — one inbox spanning all clients and all service lines: categorizations to approve, flags to triage, draft reports, draft emails. The operator's entire job becomes processing this queue.
- **Action log** — every agent action (what was done, why, confidence, which run) logged append-only. This is the audit log QBO's API doesn't expose, rebuilt for the operator's own machine — E&O defense and client trust.
- **Risk-gated autonomy** — internal analysis fully automatic; book-posting confidence-tiered; anything client-facing gated on a human click.
- **Capacity target** — each mature client ≤ 2 hours/month of review time for the bookkeeping + FP&A tiers, the design constraint that makes a 10–15 client book viable solo.

### 5.7 Bookkeeping Automation — **SPECIFIED (P5)**

Confidence-tiered transaction categorization, posting back to QBO:

- **Auto-post** — vendor + amount + account matches the client's established pattern (learned and stored per client). Posts automatically, logged.
- **Queue with suggestion** — the system proposes account + job/class with reasoning; one-click approval in the review queue.
- **Escalate** — genuinely ambiguous items, or anything touching equity, loans, payroll, or related parties, always to a human.

Thresholds tighten as history accumulates (month one: review ~60% of transactions; month six: ~10%). Backed by a learned vendor-pattern table.

### 5.8 FP&A — **SPECIFIED (P5)**

Monthly reporting pack, generated after the close passes:

- P&L vs budget and prior year; cash-flow statement; 13-week cash forecast; KPI movement.
- **WIP schedule** with under/overbilling and gross-profit-fade-by-job analysis — the construction moat; controller-grade output banks and bonding agents ask for, which generic tools don't produce.
- Drafted with narrative into the review queue; the operator reviews, edits judgment paragraphs, and publishes to the portal.

### 5.9 Fractional CFO — **SPECIFIED (P5)**

The prep is automated; the relationship is not (and shouldn't be). Before each monthly CFO call a routine assembles the brief: what changed, what's at risk, scenario models ("if you hire the second crew, cash dips below $40K in week 9"), and questions worth raising. The operator walks in having spent ~45 minutes on what used to take a day. This is the service line that justifies premium pricing and the one the machine makes *scalable* rather than replaces.

### 5.10 Payroll — **SPECIFIED (P8)**

Partner, don't build. Client payroll runs through Gusto's accountant partner program (Gusto carries tax-filing liability, provides a partner dashboard and API). The system does the parts Gusto doesn't:

- Sync payroll journal entries into QBO, correctly job-costed.
- **Certified payroll (WH-347)** and prevailing-wage compliance reports drafted from payroll data — a high-value wedge for public-works contractors, bundled rather than sold standalone.

The highest-toxicity PII (SSNs, payroll detail) lives in Gusto's vault, not the operator's database — a deliberate data-minimization decision.

### 5.11 Client Portal — **SPECIFIED (P6)**

A read-only display layer in the `carolus-advisory` repo, on a subdomain. Does not call the LLM.

- **Auth** — Clerk with organizations (each client company = one org, mapped to one client row and keyed by row-level security). Invitation-only, no self-signup. Magic-link sign-in primary (contractors aren't SaaS users), optional MFA. Roles: owner / member / viewer (the viewer role is for a client's banker or bonding agent at renewal time — a time-limited read-only seat).
- **Five pages, no more:**
  1. **Dashboard** — cash position, A/R, gross margin, cash runway, job profitability, and an "needs your attention" list.
  2. **Reports** — monthly pack archive, viewable inline + PDF download.
  3. **Documents** — drag-drop upload (feeds the intake pipeline) + status history ("filed ✓ / matched ✓").
  4. **Requests** — the agent's "need backup for these" list as a checklist the client clears by uploading.
  5. **Settings** — users, notification preferences.
- **Design** — carries the Carolus brand (Cormorant Garamond display, Jost UI, ivory/charcoal, armillary mark), tighter spacing and tabular numerals for an application. Mobile-first and genuinely usable one-handed from a job site. Numbers lead, jargon never. The portal's job is to make the client feel watched-over and make the operator indispensable — not to be self-service analytics.
- **Reads** finished outputs (KPI values, report rows, flag/request statuses, document statuses) the routines already produced; PDFs served from object storage via signed URLs.

### 5.12 Service Delivery Model — **SPECIFIED**

| Tier | Includes | Indicative monthly |
|---|---|---|
| Tier 1 | Bookkeeping + monthly reporting pack | $600–900 |
| Tier 2 | + FP&A, WIP schedule, 13-week forecast | $1,200–1,800 |
| Tier 3 | + fractional-CFO calls | $2,500–4,000 |
| Add-on | Certified payroll (per client) | $200–400 |

Pricing requires validation against the local construction market; the *structure* follows directly from the automation design (each tier maps to a capability layer).

---

## 6. Data Model (Canonical Schema)

Three zones, ~17 tables, 12 ordered migrations.

**Books (mirror of QBO in canonical form):** `clients`, `accounts`, `entities` (customers/vendors/employees unified), `jobs`, `transactions`, `journal_lines`, `vendor_patterns` (learned categorization rules), `kpi_values` (metric/period time series).

**Operations:** `documents` (lifecycle: received → classified → extracted → validated → matched / escalated), `emails`, `flags`, `runs` (every routine execution, with actions), `close_runs` (per-period close status), `rec_runs` (bank reconciliations), `sync_connections` (encrypted tokens, refresh state, sync cursors).

**Knowledge:** `facts` (append-only, supersession-linked, provenance-enforced, category-typed).

**Staging:** `qbo_raw` (append-only raw JSONB log; transforms read the latest payload per entity, so re-transforms never call QBO).

Key properties: every foreign key indexed; canonical upserts keyed on QBO IDs and change-guarded; facts and action logs append-only; multi-tenant by `client_id`, moving to database-enforced row-level security (see §8).

---

## 7. Non-Functional Requirements

- **Idempotency everywhere** — every sync and write operation is safe to re-run; unchanged data writes zero rows. Proven by gate.
- **Determinism** — rendered artifacts (CONTEXT.md, ISSUES.md) are byte-identical for identical database state. Proven by gate.
- **Exact money** — all monetary values are `Decimal`, never float; checksums use exact equality and escalate rather than tolerate drift.
- **Provenance enforcement** — structural, at the validator/database layer, not by prompt.
- **Gates as code** — each phase has an executable gate asserting end-to-end behavioral properties; no phase advances until its gate passes. Gates run against the live QBO sandbox (with a CI fixture mode).
- **LLM seams stubbed for test** — anything requiring model judgment sits behind an interface with canned outputs, so the deterministic layer is fully testable offline with zero API calls.
- **Self-healing operations** — token refresh recovers from corrupted/expired state; routines verify their own schema currency before touching books.
- **Test environments accumulate state** — anything that runs more than once must clean up after itself or scope its assertions to its own artifacts.

---

## 8. Security & Compliance

### 8.1 Encryption & secrets
- TLS in transit on every hop; database and document storage encrypted at rest; FileVault on the operator's machine; encrypted backups.
- QBO OAuth tokens Fernet-encrypted at the application layer before storage; the encryption key comes only from the environment and is never logged; the crypto module fails loudly if the key is absent.
- All secrets in a secrets manager, never in the repo; a pre-commit hook and `.gitignore` keep `.env` and client data out of git.

### 8.2 Access & isolation
- **Multi-tenant isolation** is moving from application-layer (`WHERE client_id`) to database-enforced **row-level security** as the first task of Phase 5 — so isolation fails *closed* before the portal multiplies the query surface — with a cross-tenant negative test in CI. (Per independent QA review, 2026-06-17.)
- Least privilege — the nightly agent runs read-only except on the specific tables it writes; the portal queries through RLS.
- MFA on every operator-held account; MFA available to portal users.

### 8.3 Auditability
- The `runs` log + append-only `facts` + append-only action log + git history answer "what touched this client's data and when" to the minute.

### 8.4 Data minimization
- No bank credentials ever stored (QBO OAuth means the client authorizes Intuit, never shares a bank login).
- No SSNs or payroll PII stored (lives in Gusto's vault).
- Email reads only what clients send to the firm's addresses, not their inboxes.
- Per-run context assembly sends data slices to the model, never full GL dumps.

### 8.5 AI data path
- The operator's Claude Max plan is configured with model-training **disabled** (30-day retention, no training use). Moving client processing to commercial Anthropic terms (training prohibited by contract) is planned as the client book grows.
- Anthropic holds SOC 2 Type II and ISO 27001; AES-256 at rest, TLS in transit. (Verify current certifications before publishing in client materials.)

### 8.6 Business protections
- A one-page client security summary (encryption, AI processing under supervision excluding model training, no bank credentials or SSNs stored, audit logging, deletion on offboarding) — a sales asset for diligence-minded clients.
- Engagement-letter clauses: AI-assisted processing under supervision; confidentiality and retention terms.
- E&O insurance with a cyber rider before the first client — controls reduce odds, insurance covers the tail.

### 8.7 Honest limitation
The system cannot offer end-to-end encryption on AI processing — a model must read plaintext to analyze it, true of every AI accounting tool. What it offers is encryption everywhere except the moment of processing, strict access control, and contractual clarity on data handling — the same posture as the cloud accounting tools clients already use.

---

## 9. Roadmap & Build Status

| Phase | Scope | Status | Gate |
|---|---|---|---|
| **P0** | Rails: repo, principles, CI, env, migration runner | ✅ Built | CI green on fresh clone |
| **P1** | Foundation: canonical schema, QBO OAuth, idempotent sync | ✅ Built & certified | Self-healing token; zero-dup re-sync; line integrity |
| **P2** | Rules engine: 26 deterministic checks + close checklist | ✅ Built & certified | Seeded violations detected; provenance valid; idempotent flags |
| **P3** | Knowledge layer: fact store, validator, renderer | ✅ Built & certified | Hallucinated provenance rejected; byte-identical renders |
| **P4** | Document pipeline: intake, checksum, bank rec, matching | ✅ Built & certified | Statements tie to the penny or escalate; self-scoping gate |
| **P5** | Routines + review queue (+ RLS-first, email intake, bookkeeping tiers, FP&A) | ⏳ Specified | 5 unattended nights |
| **P6** | Client portal | ⏳ Specified | Cross-tenant attack fails closed; mobile pass |
| **P7** | Dress rehearsal: full simulated client month incl. portal | ⏳ Specified | Outputs verifiably correct; < 3 hrs operator time |
| **P8** | Payroll: Gusto partner + WH-347 certified payroll | ⏳ Specified | Totals tie to Gusto to the penny; WH-347 matches a known-good sample |

**The portal is required before the first client.** Client acquisition begins only after the Phase 7 dress rehearsal passes.

By phase count the build is ~50% complete; by *product* completeness it is earlier, because the unbuilt phases carry the highest-integration-risk work (the review queue, the portal, and the end-to-end loop).

---

## 10. Testing & QA Posture

- ~238 unit/integration tests, ruff-clean, split into CI-safe (run without a database) and DB-backed (require Postgres).
- Full suite runs in CI against a Postgres service container with `pg_trgm` enabled (per QA review).
- Phase gates assert end-to-end behavioral properties against the live QBO sandbox, with a fixture mode for CI.
- Track record: the gates have caught a malformed-payload bug, a migration-ordering bug, a constraint-vocabulary drift, a non-rerunnable test harness, an ambiguity the matcher correctly refused to guess on, and two classes of test-environment pollution — with zero defects found in the rules' business logic.
- Independently reviewed (2026-06-17): a QA reviewer provisioned their own toolchain and database and reproduced 238/238 passing, ruff-clean.

---

## 11. Known Limitations & Open Decisions

- **Sandbox-only validation.** The system has only ever seen the QuickBooks sandbox. The first real client month is where the actual risk lives. Mitigation: Phase 7 is a hard gate; a read-only shadow run against one real client dataset is planned before any posting path is relied on.
- **`carlos:` provenance is regex-validated, not row-backed.** The human-authority source type is the one provenance path not tied to a concrete row — by design. An annotations table is planned for Phase 5 so these refs resolve to an auditable row like the machine source types.
- **`pg_trgm` is an infra dependency.** Required by near-duplicate detection and the duplicate-vendor rule. Covered by a migration preflight and a deploy checklist.
- **QBO mapping edge cases.** A few transaction shapes carry explanatory warnings rather than fully resolving (e.g. specific unbalanced/unresolved-account cases) — surfaced, never silent.
- **QBO-only by choice.** The canonical layer is platform-agnostic; a second accounting system would be a new adapter, built revenue-funded against a signed engagement rather than speculatively.
- **The binding constraint is client acquisition, not the software.** A complete platform with no clients is a hobby; the build cannot be allowed to become a reason to delay the sales motion once Phase 7 passes.

---

## 12. Glossary

- **Canonical model** — the platform-agnostic internal representation of accounting data, into which QBO (or any future source) is mapped.
- **CDC** — Change Data Capture; QBO's mechanism for retrieving entities changed since a cursor, used for incremental sync.
- **Close checklist** — the set of conditions that must pass before a period's books are considered closed and deliverables generate.
- **Fact** — one atomic, sourced, durable piece of knowledge about a client in the knowledge layer.
- **Flag** — a finding raised by a rule, pointing at the canonical row it concerns.
- **Gate** — an executable test asserting a phase's end-to-end properties; must pass before the phase advances.
- **Provenance / `source_ref`** — the pointer every fact, flag, and entry carries to the row, document, or message it derives from.
- **Review queue** — the single inbox where everything requiring the operator's judgment appears.
- **Routine** — a scheduled Claude Code job that runs the pipeline and produces draft outputs.
- **RLS** — row-level security; database-enforced tenant isolation.
- **WIP schedule** — work-in-progress schedule; the construction-accounting report tracking job costs, billings, and over/underbilling.

---

*End of specification.*
