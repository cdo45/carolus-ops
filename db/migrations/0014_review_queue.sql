-- 0014_review_queue.sql — the single operator inbox ("the queue is the job").
--
-- review_queue is the ONE cross-client place anything needing Carlos lands:
-- a proposed categorization, a flag to act on, a draft report/email to
-- approve. Same source_type/source_ref provenance pattern as flags; payload
-- carries the proposed action/draft. Open items are unique per
-- (client_id, source_type, source_ref) so re-running a producer refreshes
-- rather than duplicating. RLS mirrors 0013 (carolus_app is tenant-scoped;
-- the owner-run operator view spans clients).

CREATE TABLE review_queue (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id uuid NOT NULL REFERENCES clients(id),
    kind text NOT NULL CHECK (
        kind IN ('categorization', 'flag', 'draft_report', 'draft_email')
    ),
    status text NOT NULL DEFAULT 'open' CHECK (
        status IN ('open', 'approved', 'dismissed', 'snoozed')
    ),
    priority text NOT NULL DEFAULT 'info' CHECK (
        priority IN ('info', 'warn', 'critical')
    ),
    source_type text NOT NULL,
    source_ref text NOT NULL,
    title text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    run_id uuid REFERENCES runs(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    resolved_by text,
    resolution_note text
);

-- One open item per object: re-running a producer refreshes, never duplicates.
CREATE UNIQUE INDEX review_queue_open_unique
    ON review_queue (client_id, source_type, source_ref) WHERE status = 'open';
-- Inbox scan.
CREATE INDEX review_queue_inbox_idx
    ON review_queue (client_id, status, created_at);

-- RLS — mirror 0013 (tests/test_rls.py guards that this exists).
GRANT SELECT, INSERT, UPDATE, DELETE ON review_queue TO carolus_app;
ALTER TABLE review_queue ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON review_queue FOR ALL TO carolus_app
    USING (client_id = current_setting('app.current_client', true)::uuid)
    WITH CHECK (client_id = current_setting('app.current_client', true)::uuid);
