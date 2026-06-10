-- 0001_init — canonical + staging + sync schema.
--
-- Raw-then-canonical: qbo_raw stores QBO responses untouched; deterministic
-- transforms map staging -> canonical. Analysis reads canonical ONLY, and
-- canonical can always be rebuilt from staging without re-calling QBO.
-- Single public schema; the canonical / staging / sync groups below are
-- logical, marked by section headers.

-- ============================ canonical ============================

CREATE TABLE clients (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    qbo_realm_id text UNIQUE,
    tier text,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL REFERENCES clients(id),
    qbo_id text NOT NULL,
    name text NOT NULL,
    acct_type text,
    acct_subtype text,
    active boolean NOT NULL DEFAULT true,
    UNIQUE (client_id, qbo_id)
);
CREATE INDEX accounts_client_id_idx ON accounts (client_id);

CREATE TABLE entities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL REFERENCES clients(id),
    qbo_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('customer', 'vendor', 'employee')),
    name text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    UNIQUE (client_id, qbo_id, kind)
);
CREATE INDEX entities_client_id_idx ON entities (client_id);

CREATE TABLE jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL REFERENCES clients(id),
    qbo_id text NOT NULL,
    entity_id uuid REFERENCES entities(id),
    name text NOT NULL,
    contract_amount numeric,
    status text,
    UNIQUE (client_id, qbo_id)
);
CREATE INDEX jobs_client_id_idx ON jobs (client_id);
CREATE INDEX jobs_entity_id_idx ON jobs (entity_id);

CREATE TABLE transactions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL REFERENCES clients(id),
    qbo_id text NOT NULL,
    txn_type text NOT NULL,
    txn_date date,
    amount numeric,
    entity_id uuid REFERENCES entities(id),
    doc_status text NOT NULL DEFAULT 'unbacked',
    review_tier text,
    qbo_synced_at timestamptz,
    UNIQUE (client_id, qbo_id, txn_type)
);
-- leading client_id column also serves the client_id fk
CREATE INDEX transactions_client_id_txn_date_idx ON transactions (client_id, txn_date);
CREATE INDEX transactions_entity_id_idx ON transactions (entity_id);

-- line_no: stable per-transaction line identity (QBO line Id where present,
-- else ordinal) so re-transforms upsert in place instead of delete+reinsert —
-- required for the zero-new/zero-modified idempotency rule.
CREATE TABLE journal_lines (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id uuid NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    line_no integer NOT NULL,
    account_id uuid NOT NULL REFERENCES accounts(id),
    job_id uuid REFERENCES jobs(id),
    amount numeric NOT NULL,
    posting_type text NOT NULL CHECK (posting_type IN ('debit', 'credit')),
    description text,
    UNIQUE (transaction_id, line_no)
);
CREATE INDEX journal_lines_account_id_idx ON journal_lines (account_id);
CREATE INDEX journal_lines_job_id_idx ON journal_lines (job_id);

-- Facts are append-only: supersede, never edit. Only status/superseded_by
-- may change after insert (enforced by trigger below — validators enforce,
-- not prompts). Extend the category list by migration when a new category
-- is agreed.
CREATE TABLE facts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL REFERENCES clients(id),
    category text NOT NULL CHECK (
        category IN ('financial', 'operational', 'tax', 'compliance', 'preference', 'context')
    ),
    statement text NOT NULL CHECK (char_length(statement) <= 200),
    source_type text NOT NULL,
    source_ref text NOT NULL,
    confidence numeric CHECK (confidence >= 0 AND confidence <= 1),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded')),
    superseded_by uuid REFERENCES facts(id),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX facts_client_id_status_idx ON facts (client_id, status);
CREATE INDEX facts_superseded_by_idx ON facts (superseded_by);

CREATE FUNCTION facts_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'facts are append-only: DELETE not allowed (supersede instead)';
    END IF;
    IF (NEW.id, NEW.client_id, NEW.category, NEW.statement, NEW.source_type,
        NEW.source_ref, NEW.confidence, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.client_id, OLD.category, OLD.statement, OLD.source_type,
        OLD.source_ref, OLD.confidence, OLD.created_at) THEN
        RAISE EXCEPTION 'facts are append-only: only status/superseded_by may change';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER facts_append_only
    BEFORE UPDATE OR DELETE ON facts
    FOR EACH ROW EXECUTE FUNCTION facts_append_only();

-- source_type/source_ref NOT NULL: no flag without provenance (principle 2).
CREATE TABLE flags (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL REFERENCES clients(id),
    rule_code text NOT NULL,
    severity text NOT NULL CHECK (severity IN ('info', 'warning', 'error', 'critical')),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
    source_type text NOT NULL,
    source_ref text NOT NULL,
    detail text,
    resolution_note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz
);
CREATE INDEX flags_client_id_status_idx ON flags (client_id, status);

CREATE TABLE runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid REFERENCES clients(id),
    routine text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
    actions jsonb NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX runs_client_id_idx ON runs (client_id);

CREATE TABLE kpi_values (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL REFERENCES clients(id),
    kpi_code text NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    value numeric NOT NULL,
    source_ref text NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (client_id, kpi_code, period_start, period_end)
);

CREATE TABLE vendor_patterns (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL REFERENCES clients(id),
    entity_id uuid REFERENCES entities(id),
    match_pattern text NOT NULL,
    account_id uuid REFERENCES accounts(id),
    confidence numeric CHECK (confidence >= 0 AND confidence <= 1),
    occurrences integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (client_id, match_pattern)
);
CREATE INDEX vendor_patterns_entity_id_idx ON vendor_patterns (entity_id);
CREATE INDEX vendor_patterns_account_id_idx ON vendor_patterns (account_id);

-- Document bytes live in R2 (storage_ref), never in the DB or git.
CREATE TABLE documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL REFERENCES clients(id),
    kind text,
    storage_ref text NOT NULL,
    sha256 text NOT NULL,
    status text NOT NULL DEFAULT 'received',
    received_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    UNIQUE (client_id, sha256)
);

-- Email bodies live in R2 (body_ref), not inline.
CREATE TABLE emails (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid REFERENCES clients(id),
    direction text NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    message_id text UNIQUE,
    from_addr text,
    to_addr text,
    subject text,
    body_ref text,
    status text NOT NULL DEFAULT 'received',
    received_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX emails_client_id_idx ON emails (client_id);

-- ============================ staging ============================

-- Append-only landing zone for untouched QBO payloads. Multiple rows per
-- (entity_type, qbo_id) accumulate over time; transforms read the latest.
CREATE TABLE qbo_raw (
    id bigserial PRIMARY KEY,
    client_id uuid NOT NULL REFERENCES clients(id),
    entity_type text NOT NULL,
    qbo_id text NOT NULL,
    payload jsonb NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    sync_run_id uuid REFERENCES runs(id)
);
CREATE INDEX qbo_raw_client_entity_qbo_idx ON qbo_raw (client_id, entity_type, qbo_id);
CREATE INDEX qbo_raw_sync_run_id_idx ON qbo_raw (sync_run_id);

-- ============================ sync ============================

-- Tokens are stored Fernet-encrypted only (status flips to needs_reauth
-- when a refresh comes back invalid_grant).
CREATE TABLE sync_connections (
    client_id uuid PRIMARY KEY REFERENCES clients(id),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'needs_reauth')),
    access_token_enc text,
    refresh_token_enc text,
    token_expires_at timestamptz,
    refresh_expires_at timestamptz,
    last_full_sync timestamptz,
    last_cdc_cursor timestamptz
);
