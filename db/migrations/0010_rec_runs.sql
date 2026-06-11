-- 0010_rec_runs — one row per (client, account, statement document)
-- bank reconciliation. Re-running a rec upserts the same row with a
-- change guard (zero drift on unchanged inputs). tied=true means the
-- statement movement fully reconciles against canonical QBO activity:
-- that feeds the close checklist's documents_reviewed condition.

CREATE TABLE rec_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL REFERENCES clients(id),
    account_id uuid NOT NULL REFERENCES accounts(id),
    statement_doc_id uuid NOT NULL REFERENCES documents(id),
    period_start date NOT NULL,
    period_end date NOT NULL,
    statement_beginning numeric NOT NULL,
    statement_ending numeric NOT NULL,
    qbo_cleared_balance numeric NOT NULL,
    matched_count integer NOT NULL,
    unmatched_statement_lines jsonb NOT NULL DEFAULT '[]',
    unmatched_qbo_txns jsonb NOT NULL DEFAULT '[]',
    outstanding_items jsonb NOT NULL DEFAULT '[]',
    tied boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (client_id, account_id, statement_doc_id)
);
CREATE INDEX rec_runs_account_id_idx ON rec_runs (account_id);
CREATE INDEX rec_runs_statement_doc_id_idx ON rec_runs (statement_doc_id);
