# carolus-ops

Private operations repo for Carolus Advisory, an automated accounting and
advisory practice. It holds the pipeline that syncs QuickBooks Online into a
canonical Postgres schema, runs deterministic rule checks over the books,
layers Claude-driven analysis on top of source-referenced facts, routes
anything needing human judgment into a single review queue, and publishes
approved output to the client portal (portal code lives in
`carolus-advisory`). Code, prompts, tests, and docs live here; client data
never enters git (enforced by `.gitignore`).

## Operating principles

1. Claude interprets, scripts extract, math validates, Carlos approves.
2. Provenance or it doesn't exist — no fact, flag, or entry without a source_ref; validators enforce, not prompts.
3. The queue is the job — anything needing Carlos appears in one review queue; everything else the machine owns.
4. Risk-gated autonomy — internal analysis: auto. Posting to books: confidence-tiered. Anything leaving the building: human click, forever.
5. Green gates, then sell — no phase advances without its test gate passing as code.

Full text and engineering standards: [CLAUDE.md](CLAUDE.md).

## Repo map

```
db/         canonical schema + migrations          (Phase 1)
sync/       QBO adapter + idempotent sync engine   (Phase 1)
rules/      deterministic SQL checks               (Phase 2)
knowledge/  fact validator + renderer              (Phase 3)
docpipe/    document intake/extraction             (Phase 4)
routines/   routine prompt configs                 (Phase 5)
prompts/    versioned LLM prompt files
tests/      phase-gate test scripts
docs/       roadmap + design docs
```

## Local setup

```sh
git clone git@github.com:cdo45/carolus-ops.git && cd carolus-ops
uv sync                  # Python 3.12, creates .venv
cp .env.example .env     # fill values from Doppler — never commit .env
uv run pytest            # should pass before you touch anything
```

## Roadmap

Phase plan and test gates: [docs/ROADMAP.md](docs/ROADMAP.md). The portal is
required before the first client; sell only after P7.
