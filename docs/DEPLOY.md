# Deploy / infra preflight

Run through this before pointing carolus-ops at a new Postgres — a fresh
environment, a new managed instance, or a new client database. The
migration runner enforces the extension requirement automatically
(`db/migrate.py` preflight); the rest is operator responsibility.

## 1. Postgres version

Postgres **>= 13** is required; **16** is what CI runs and the recommended
target. We rely on `gen_random_uuid()` (core since 13) and `pg_trgm` as a
TRUSTED extension (13+, so a database owner can enable it without
superuser).

Check: `SELECT version();`

## 2. Required extensions

| Extension | Needed by | Notes |
|---|---|---|
| `pg_trgm` | migration 0008; R027 duplicate-vendor detection + fact near-duplicate gate | trusted on PG 13+; ships with `postgresql-contrib` |

`db/migrate.py` runs a **preflight** before applying any migration: if a
required extension is neither installed nor available to install, it aborts
with an actionable message — naming the extension and how to enable it —
instead of failing partway through migration 0008. To enable manually (as
the database owner):

```sql
CREATE EXTENSION pg_trgm;
```

On managed Postgres (RDS, Cloud SQL, Supabase, …) enable it through the
provider's extension catalog if `CREATE EXTENSION` is restricted, then
re-run the migrator.

## 3. Secrets / environment

All secrets come from the environment (Doppler-sourced); none are committed
(`client data never enters git`). See [`.env.example`](../.env.example) for
the full list. Required before running:

- **`DATABASE_URL`** — canonical Postgres connection string.
- **`APP_ENCRYPTION_KEY`** — Fernet key encrypting OAuth tokens at rest.
  Must be present and **stable**: rotating it strands every stored token.
  Generate with:
  ```sh
  uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- **`QBO_CLIENT_ID`**, **`QBO_CLIENT_SECRET`**, **`QBO_REDIRECT_URI`**,
  **`QBO_ENVIRONMENT`** (`sandbox` | `production`).
- **`R2_ACCOUNT_ID`**, **`R2_ACCESS_KEY_ID`**, **`R2_SECRET_ACCESS_KEY`** —
  document storage (Phase 4 runs on LocalFS until R2 is wired).
- **`RESEND_API_KEY`** — outbound email.

## 4. Apply migrations

Migrations are plain SQL in `db/migrations/`, applied in version order and
tracked in `schema_migrations`. The runner is **idempotent** — it applies
only what is pending and is a no-op on an up-to-date database.

```sh
uv run python -m db.migrate
```

Current order: `0001_init` → `0002_soft_delete` → `0003_severity_vocab` →
`0004_txn_provenance_fields` → `0005_linked_txn` → `0006_close_runs` →
`0007_facts_taxonomy` → `0008_pg_trgm` → `0009_document_lifecycle` →
`0010_rec_runs` → `0011_jobs_completed_at` → `0012_sales_tax_account`.

Exit codes: `0` applied/up-to-date · `2` `DATABASE_URL` unset · `3`
preflight failed (a required extension is unavailable — see §2).

## 5. Per-client: sales-tax account curation

`clients.sales_tax_account_id` is **curated**, never written by sync. A
chart of accounts with two or more `GlobalTaxPayable` accounts (e.g.
multiple state agencies) leaves taxed transactions flagged until one is
chosen — the transform refuses to guess. After the first sync of a new
client:

```sh
uv run python -m sync.set_tax_account --realm <realm_id>                      # list candidates
uv run python -m sync.set_tax_account --realm <realm_id> --account <qbo_id>   # set it
uv run python -m sync.retransform --realm <realm_id>                          # repair flagged txns
```

A single `GlobalTaxPayable` account resolves automatically and needs no
curation. Confirm `retransform`'s `warnings_retained` is empty.

## 6. CI parity

`.github/workflows/ci.yml` runs ruff + the full pytest suite (unit and
DB-backed) against a `postgres:16` service with `pg_trgm` enabled. Green CI
means the migrations apply cleanly and every DB-backed test passes on a
matching server — the same preflight and schema this checklist provisions.

## 7. Tenant isolation (RLS)

Migration 0013 enforces tenant isolation in Postgres via the least-privilege
`carolus_app` role — the identity the portal/agent connect through (scoped
per request by `db.tenant.tenant_tx`); the migrating role must be able to
create it (superuser, or a role with `CREATEROLE`).

