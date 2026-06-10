# CLAUDE.md — carolus-ops

## System overview

carolus-ops is the operations backbone of Carolus Advisory, an automated
accounting/advisory firm. QuickBooks Online data is pulled by an idempotent
sync engine into a canonical Postgres schema; a deterministic rules engine
runs SQL checks over those books; Claude performs interpretive analysis on
top of validated, source-referenced facts; anything that requires human
judgment lands in a single review queue for Carlos; approved outputs are
published to the client portal. In short: QBO → canonical Postgres → rules →
Claude analysis → review queue → portal.

## The Five Operating Principles

### 1. Claude interprets, scripts extract, math validates, Carlos approves.

### 2. Provenance or it doesn't exist — no fact, flag, or entry without a source_ref; validators enforce, not prompts.

### 3. The queue is the job — anything needing Carlos appears in one review queue; everything else the machine owns.

### 4. Risk-gated autonomy — internal analysis: auto. Posting to books: confidence-tiered. Anything leaving the building: human click, forever.

### 5. Green gates, then sell — no phase advances without its test gate passing as code.

## Engineering standards

- Python 3.12 with `uv` for all pipeline code.
- Type hints required on all functions and module-level declarations.
- Every module gets a test. No untested code paths into production.
- Secrets only via environment variables (Doppler-sourced). Never hardcoded,
  never committed — see `.gitignore`: client data never enters git.
- All LLM calls must request schema-validated JSON and reject/retry on
  invalid output. Free-text LLM output is never parsed into the pipeline.
- Idempotency required for all sync and write operations: re-running any
  job must produce zero duplicates and zero drift.

## Working agreement for CC sessions

- Work in sequential chunks; never write files for a later chunk early.
- One concern per commit, with a clear, descriptive message.
- Build/test before every push — a failing tree never gets pushed.
- Stop on failure and report; do not improvise around a broken step.
