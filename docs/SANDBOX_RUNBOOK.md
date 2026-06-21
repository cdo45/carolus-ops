# Carolus-Ops — QBO Sandbox Setup & Terminal Testing Runbook

**Purpose:** Explain how the QuickBooks Online sandbox is connected and how the
pipeline is tested against it from the terminal. Written so a developer who has
never touched the Intuit API can understand the model and reproduce the setup.

**Audience:** Developer / QA picking up `carolus-ops`.

---

## 1. The mental model (read this first)

The most common confusion: **QuickBooks Online is not a database you connect to with a connection string.** It's a cloud SaaS with a REST API guarded by OAuth2. So unlike Postgres (where you paste a `postgresql://…` URL and you're in), connecting to QBO has three moving parts:

1. **An app identity** — a Client ID + Client Secret issued by Intuit, identifying *our software*.
2. **A company to read** — for development, Intuit gives you a free fake company called a **sandbox**, pre-loaded with realistic sample data.
3. **A user authorization** — a one-time browser consent where the company owner clicks "Authorize," after which Intuit hands our app rotating access tokens. We never see or store a QuickBooks password; we store encrypted tokens.

So there are **two completely separate credentials** in play, and people conflate them:

| | What it is | Where it comes from |
|---|---|---|
| `DATABASE_URL` | Postgres connection string | Neon dashboard → Connect |
| QBO Client ID / Secret | OAuth app keys | Intuit Developer portal → app → Keys |

The database is *our* storage. QBO is the *source system* we pull from. The pipeline syncs QBO → Postgres; it does not run "on" QBO.

### Sandbox vs. production keys

Intuit issues two key sets per app, and they are **not interchangeable**:

- **Development keys** work *only* with sandbox companies. This is what we use.
- **Production keys** work *only* with real, live QBO companies (and require Intuit to approve the app first).

The code switches between them on the `QBO_ENVIRONMENT` env var (`sandbox` vs `production`). Everything we've built and tested runs against the sandbox with development keys. Moving to a real client later is a credential swap, not a code change.

---

## 2. Intuit-side setup (one time)

1. **Create an Intuit Developer account** at `developer.intuit.com`. On signup, Intuit **automatically provisions a sandbox QuickBooks Online company** — a fully functional fake company with sample customers, vendors, a full chart of accounts, invoices, etc. (Ours is the default Intuit sample: a California landscaping business. That detail matters later — its sales tax agency is the California Board of Equalization.)
2. **Create an app** inside your workspace (ours is named "Test App" in the "CAROLUS ADVISORY" workspace). The app is the OAuth identity.
3. **Get the development keys.** App → **Keys & Credentials** → **Development** tab → toggle **Show credentials**. Copy the **Client ID** and **Client Secret**. (The Client ID is the public half of the pair; the secret is sensitive — treat it like a password.)
4. **Register the redirect URI.** App → **Settings** → **Redirect URIs** tab → make sure you're on **Development** → add exactly:
   ```
   http://localhost:8000/callback
   ```
   This is the URL Intuit will send the user's browser back to after they authorize. Intuit matches it **character for character** — a trailing slash or `https` instead of `http` will be rejected. Our connect script runs a tiny local web server on `localhost:8000` to catch this callback.
5. **Confirm the scope.** App → **Permissions** → ensure **Accounting** is enabled (it usually is by default).
6. **Free-tier note:** the developer portal is free, with up to 500,000 API calls/month — we'll never approach that.

---

## 3. Local-side setup (one time)

1. **Clone the repo** (`carolus-ops`). We used GitHub Desktop; `git clone` works identically.
2. **Install `uv`** (the Python package manager the project uses):
   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
3. **Create the `.env` file.** The repo contains `.env.example` (a committed template); the real `.env` is git-ignored and never committed. Copy the template and fill in real values:
   ```sh
   cp .env.example .env
   ```
   ```
   QBO_CLIENT_ID=<from Intuit Keys & Credentials>
   QBO_CLIENT_SECRET=<from Intuit Keys & Credentials>
   QBO_REDIRECT_URI=http://localhost:8000/callback
   QBO_ENVIRONMENT=sandbox
   DATABASE_URL=<from Neon → Connect>
   APP_ENCRYPTION_KEY=<generated, see next step>
   ```
4. **Generate the encryption key.** This Fernet key encrypts the QBO OAuth tokens before they're stored in Postgres:
   ```sh
   uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   Paste the output as `APP_ENCRYPTION_KEY` in `.env`. (Keep a copy in a password manager — if lost, you simply re-authorize QBO, but don't lose it casually.)
5. **Install dependencies:**
   ```sh
   uv sync
   ```

> **Security note:** secrets live only in `.env` on the developer's machine (and in a secrets manager for deployment). They are never pasted into chat, prompts, or committed to git. The `.gitignore` enforces this; the connect/sync code reads secrets from the environment only.

---

## 4. Database setup (one time + on every pull)

The schema is managed by a homegrown ordered-SQL migration runner (no ORM). Apply it:

```sh
uv run python -m db.migrate
```

This applies any un-applied migrations (currently `0001`–`0014`) and is **idempotent** — running it when everything is already applied does nothing. It also runs a preflight that checks required Postgres extensions (e.g. `pg_trgm`) are available and fails with a clear message if not.

> **Standing rule:** after every `git pull`, run `db.migrate` before anything else. New code often ships new migrations, and running them costs nothing when there's nothing to apply.

---

## 5. Connecting to the sandbox (one time per machine, ~100-day token life)

This is the OAuth handshake. Run:

```sh
uv run python -m sync.connect
```

What happens:

1. The script prints an Intuit authorize URL and opens it in your browser.
2. You sign in with your **Intuit developer** credentials and pick the **sandbox company**, then click **Authorize**.
3. Intuit redirects your browser to `http://localhost:8000/callback?code=…&realmId=…`.
4. The script's local listener catches that callback, exchanges the `code` for access + refresh tokens, creates a `clients` row keyed to the company's **realm ID**, and stores the **encrypted** tokens in the `sync_connections` table.
5. On success it prints the realm ID. Ours is `9341457249446742`.

### Two gotchas we hit live (so you don't lose time on them)

- **`invalid_redirect_uri` error.** Means the redirect URI wasn't registered in Intuit's Settings (or doesn't match exactly). Fix: add `http://localhost:8000/callback` in App → Settings → Redirect URIs (Development), save, retry.
- **Safari "Can't Open the Page" on the localhost callback.** Safari's HTTPS-Only mode refuses to load an `http://localhost` URL, so the browser never delivers the callback even though the authorization succeeded (you'll see the `code` in Safari's address bar). Two fixes:
  - **Quick:** copy the full callback URL from the address bar and deliver it with curl, which ignores Safari's rule:
    ```sh
    curl 'http://localhost:8000/callback?code=…&state=…&realmId=…'
    ```
    The waiting `sync.connect` listener catches it and completes.
  - **Permanent:** use Chrome for the auth step, or disable Safari's HTTPS-Only setting.

> Tokens auto-refresh after this. The access token lives ~60 minutes and is refreshed automatically by the client; the refresh token rotates and is re-persisted on every refresh, so you won't repeat this browser dance for ~100 days.

---

## 6. Pulling the data (the sync)

```sh
uv run python -m sync.full_sync --realm 9341457249446742
```

This is a **two-stage sync**:

1. **Pull → staging.** Every QBO entity (Account, Customer, Vendor, Invoice, Bill, Payment, Purchase, JournalEntry, etc.) is fetched via the API and landed *untouched* as raw JSONB in the `qbo_raw` staging table.
2. **Transform → canonical.** Deterministic code maps the raw payloads into the normalized tables (`accounts`, `entities`, `jobs`, `transactions`, `journal_lines`).

It prints a summary of what it fetched and wrote. Key property: it's **idempotent** — running it again with no QBO changes writes **zero** canonical rows (every upsert is change-guarded).

There's also a transform-only command that re-runs the staging→canonical step **without calling QBO** (useful after a mapping change):

```sh
uv run python -m sync.retransform --realm 9341457249446742
```

You can confirm data landed by opening the Neon dashboard → Tables → `accounts` / `transactions` and seeing rows.

---

## 7. How testing works — "gates as code"

This is the part most worth understanding. The project uses two kinds of tests:

### Unit/integration tests (`pytest`)

~250+ tests, ruff-clean. Split into:

- **CI-safe** — run with no database (LLM seams stubbed, pure logic). These run in GitHub CI on every push.
- **DB-backed** — require a scratch Postgres; cover the rules engine, fact store, transforms, document pipeline, RLS, and the review queue. These run in CI against a Postgres service container and locally.

```sh
uv run ruff check .
uv run pytest            # full suite when CAROLUS_TEST_DB is set to a scratch DB
```

### Phase gates (executable, against the live sandbox)

Each phase has a **gate script** that proves end-to-end behavioral properties against the real sandbox — not mocked. A phase isn't "done" until its gate prints PASS. The gates are how we test *the actual integration*, not just isolated functions.

The clever part is **how we test detection**: we deliberately create known-bad transactions in the sandbox via the QBO API, then assert the engine catches them.

```sh
# 1. Plant 21 deliberate violations in the sandbox (one per rule), print a manifest
uv run python -m tests.seed_errors --realm 9341457249446742 --reseed

# 2. Run the Phase 2 gate: re-sync, run the rules engine, assert each seeded
#    violation was flagged with the correct rule code on the correct row
uv run python -m tests.gate_phase2 --realm 9341457249446742
```

`seed_errors.py` creates things like a duplicate payment, a round-number journal entry, an aged suspense balance, an overdrawn bank account, a near-duplicate vendor name, etc. — each tagged in its QBO `PrivateNote` field (`CAROLUS-SEED-…`) so the harness can find and clean them up later. The gate then verifies the engine's detection against that manifest (bar: catch ≥ 20 of 21).

Other gates follow the same pattern:

- **Phase 1 gate** — full sync twice (proves idempotency), corrupt the stored token and call for it (proves self-recovery), and verify every transaction's journal lines net to zero or carry a warning.
- **Phase 4 gate** — generate synthetic bank-statement PDFs from the canonical data (including a deliberately corrupted one and an image-only one), run them through the document pipeline, and assert each either reconciles to the penny or escalates with the correct reason — never silently wrong.

### Why seeding has a `--reseed` flag

The sandbox accumulates state across test runs. Re-seeding mints *fresh, unique* vendors/customers each generation and cleans up prior generations' open balances, so history-sensitive rules (e.g. "first-ever transaction with a new vendor," "customer concentration as a % of A/R") test correctly every time instead of being thrown off by leftover data from earlier runs.

---

## 8. Full reproducible sequence (copy/paste)

For a developer setting up from scratch, after the Intuit-side and `.env` steps:

```sh
# install + deps
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync

# database
uv run python -m db.migrate

# connect to the sandbox (browser auth; see gotchas in §5)
uv run python -m sync.connect
# → note the realm ID it prints (ours: 9341457249446742)

# pull the data
uv run python -m sync.full_sync --realm <realm>

# (sandbox only) set the sales tax account, since the sample company
# has two candidate tax agencies — pick Board of Equalization
uv run python -m sync.set_tax_account --realm <realm>            # lists candidates
uv run python -m sync.set_tax_account --realm <realm> --account 90
uv run python -m sync.retransform --realm <realm>               # repair taxed txns

# run the gates
uv run python -m tests.seed_errors --realm <realm> --reseed     # plant 21 violations
uv run python -m tests.gate_phase2 --realm <realm>              # detection gate
uv run python -m tests.gate_phase4 --realm <realm>              # document pipeline gate

# unit/integration suite (needs a scratch Postgres in CAROLUS_TEST_DB)
uv run ruff check .
CAROLUS_TEST_DB=postgresql://…/carolus_test uv run pytest
```

---

## 9. Quick FAQ for the dev

**"Where's the QBO connection string?"** There isn't one. QBO uses OAuth, not a connection string — see §1. The only connection string is `DATABASE_URL` (Postgres).

**"Why a browser popup to connect?"** That's the OAuth consent. It happens once per machine; tokens then refresh automatically.

**"Is the sandbox data real?"** No — it's Intuit's free fake company, safe to seed bad data into. No real client books have ever touched this.

**"How do we test against a real client later?"** Swap to production keys, set `QBO_ENVIRONMENT=production`, and the same client authorizes via the same OAuth flow. No code change.

**"Why create bad data on purpose?"** That's how detection is tested end-to-end — plant a known duplicate payment, prove the engine flags exactly it. Mocked unit tests prove the logic; the seed-and-detect gates prove the whole integration against the real API.

**"What if a gate fails?"** Read it as a finding first, not a bug. Several gate "failures" in development were the engine being *correct* about polluted test data, with the harness needing a fix — not the business logic. The gate output names the specific seeded item and expected rule code, which tells you immediately whether it's a logic miss or an environment issue.

---

*End of runbook.*
