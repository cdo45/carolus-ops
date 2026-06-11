-- 0011_jobs_completed_at — when the job actually finished. CURATED:
-- set by Carlos alongside status='completed'/'closed'; sync never writes
-- it (the jobs upsert touches only entity_id/name/status — the same
-- protection contract that keeps status and contract_amount curated).
-- R031 grants a 14-day grace window after completed_at for trailing
-- invoices/cleanup costs; a completed job with NULL completed_at gets no
-- grace (prior behavior).

ALTER TABLE jobs ADD COLUMN completed_at date;
