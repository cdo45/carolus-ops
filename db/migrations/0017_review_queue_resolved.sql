-- 0017_review_queue_resolved.sql — flag triage can RESOLVE a queue item.
--
-- routines.queue.triage closes a kind='flag' item as 'dismissed' or 'resolved'
-- and propagates the same status to the underlying flag (flags already allow
-- both). review_queue's status CHECK (0014) admitted 'dismissed' but not
-- 'resolved'; add it. A CHECK is altered by replacing the constraint, so
-- drop the auto-named one and recreate it (verified name:
-- review_queue_status_check). 'approved'/'snoozed' stay for the suggestion-type
-- items (categorizations, drafts) that triage will handle later.

ALTER TABLE review_queue DROP CONSTRAINT review_queue_status_check;
ALTER TABLE review_queue ADD CONSTRAINT review_queue_status_check
    CHECK (status IN ('open', 'approved', 'dismissed', 'snoozed', 'resolved'));
