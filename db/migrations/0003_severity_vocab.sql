-- 0003_severity_vocab — one severity vocabulary, everywhere.
--
-- Phase 2 rule modules declare severity in ('info','warn','critical').
-- 0001 shipped flags with ('info','warning','error','critical'); rather
-- than maintain a mapping layer, migrate the rows and the constraint so
-- exactly one vocabulary exists in the system.
--
-- ORDERING MATTERS (the runner wraps this file in one transaction):
-- the old constraint must be DROPPED before rows are rewritten to the
-- new vocabulary — 'warn' is not legal under the old check, so updating
-- first aborts the migration on any database that already has Phase 1
-- flags (e.g. transform_warning rows). Constraint-touching migrations
-- must always go: drop -> rewrite data -> add.

ALTER TABLE flags DROP CONSTRAINT flags_severity_check;

UPDATE flags SET severity = 'warn' WHERE severity = 'warning';
UPDATE flags SET severity = 'critical' WHERE severity = 'error';

ALTER TABLE flags ADD CONSTRAINT flags_severity_check
    CHECK (severity IN ('info', 'warn', 'critical'));
