-- 0006_close_runs — one row per (client, period) close evaluation.
--
-- Design choice (over kpi_values metric='close_status'): a close is a
-- structured set of named condition results, each pass/fail/not_evaluated
-- with detail — jsonb, not a single numeric. kpi_values stays reserved
-- for actual scalar KPIs. Latest evaluation wins via change-guarded
-- upsert: re-evaluating unchanged books writes zero rows (zero drift).

CREATE TABLE close_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL REFERENCES clients(id),
    period_start date NOT NULL,
    period_end date NOT NULL,
    status text NOT NULL CHECK (status IN ('green', 'red', 'incomplete')),
    conditions jsonb NOT NULL,
    evaluated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (client_id, period_start, period_end)
);
CREATE INDEX close_runs_client_id_idx ON close_runs (client_id);
