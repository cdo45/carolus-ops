# Build Roadmap

> **The portal is REQUIRED before the first client. Sell only after P7.**
>
> Per principle 5 (green gates, then sell): no phase advances without its
> test gate passing **as code** in `/tests`.

| Phase | Scope | Est. hours | Test gate |
|------:|-------|-----------:|-----------|
| P0 | Rails/setup: repo skeleton, CLAUDE.md, CI, env template, smoke test | 3–5 | CI green on a fresh clone: `ruff check` + `pytest` pass |
| P1 | Foundation: canonical Postgres DDL, QBO OAuth, idempotent sync engine | 15–20 | Kill token → self-recovers; double-run → zero dupes |
| P2 | Rules engine: deterministic SQL checks over canonical books | 12–16 | ≥14/15 seeded errors caught |
| P3 | Knowledge layer: fact validator + renderer | 15–20 | ≥90% fact accuracy, zero unsourced facts, byte-identical re-renders |
| P4 | Document pipeline: intake, extraction, statement reconciliation | 20–25 | Statements tie to the penny or escalate — never silently wrong |
| P5 | Routines + review queue: scheduled runs, single queue for Carlos | 15–20 | 5 unattended nights |
| P6 | Client portal (lives in `carolus-advisory` repo) | 25–30 | Cross-tenant attack fails closed; mobile pass |
| P7 | Dress rehearsal: full simulated client month, portal included | 10–15 | Outputs verifiably correct; <3 hrs Carlos time |
| P8 | Post-clients: Gusto payroll partner + WH-347 certified payroll | 20–25 | Payroll totals tie to Gusto to the penny; WH-347 output matches a known-good sample |

## GATE — Phase 1 (how to run it)

The phase-1 gate is code: `tests/gate_phase1.py`. It runs against the
**live QBO sandbox** (never CI) and must print `GATE: PASS (3/3)` before
Phase 2 work starts.

```sh
cp .env.example .env                 # fill from Doppler; generate APP_ENCRYPTION_KEY
uv run python -m db.migrate          # apply schema
uv run python -m sync.connect        # one-time sandbox OAuth consent
uv run python -m tests.gate_phase1 --realm <sandbox_realm_id>
```

Checks: (a) full sync twice → second run writes zero canonical rows;
(b) corrupted stored access token → transparent refresh recovery;
(c) every transaction's journal lines net to zero or carry an open
`transform_warning` flag. The same properties run in CI against fixtures
(`tests/test_full_sync_db.py`, `tests/test_incremental.py`).

## GATE — Phase 2 (how to run it)

Runs against the **live QBO sandbox** (never CI), in the same calendar
month as seeding (R020's window is the current month). Must print
`GATE: PASS (3/3)` before Phase 3 work starts.

```sh
uv run python -m db.migrate                          # 0003-0006 apply
uv run python -m tests.seed_errors --realm <realm>   # plant 15 violations
uv run python -m tests.gate_phase2 --realm <realm>   # sync -> engine -> assert
```

Checks: (a) ≥14/15 seeded violations flagged open with the expected
rule_code on the correct canonical row; (b) every open engine flag's
source_ref resolves to a real canonical row; (c) a second engine run
creates zero new open flags. The seeder refuses to run unless
QBO_ENVIRONMENT=sandbox; its manifest lives in data/ (gitignored).
The same lifecycle properties are CI-tested in tests/test_rules_engine.py
and per-rule fire/non-fire cases in tests/test_rules_*.py.

## GATE — Phase 3 (how to run it)

Fully local: DB-backed with canned model outputs — **no live LLM call,
no QBO sandbox**. Needs a scratch Postgres database (the gate DROPS and
rebuilds its schema; it refuses to run against DATABASE_URL):

```sh
CAROLUS_TEST_DB=postgresql://carolus:...@localhost:5432/carolus_test \
    uv run python -m tests.gate_phase3
```

Checks: every planted violation (fake source_ref, near-duplicate,
over-length) rejected with the correct reason code; zero facts with
dangling source_refs (SQL join proof); ≥90% of valid ops applied;
supersede chain integrity with retired facts never rendering;
byte-identical re-renders with single-fact diffs confined to their
section. Must print `GATE: PASS (5/5)` before Phase 4 work starts.
The same properties are CI-tested across tests/test_fact_store.py,
test_validator.py, test_render.py, and test_extract.py.

## Sequence notes

- P0–P5 build the machine; P6 makes it client-visible; P7 proves the whole
  loop end-to-end before any real client touches it.
- P8 starts only after paying clients are live — payroll is an expansion,
  not a prerequisite.
- Hour estimates are working figures for a solo builder with Claude Code;
  gates, not hours, decide when a phase is done.
