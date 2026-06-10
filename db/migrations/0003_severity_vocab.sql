-- 0003_severity_vocab — one severity vocabulary, everywhere.
--
-- Phase 2 rule modules declare severity in ('info','warn','critical').
-- 0001 shipped flags with ('info','warning','error','critical'); rather
-- than maintain a mapping layer, migrate the rows and the constraint so
-- exactly one vocabulary exists in the system.

UPDATE flags SET severity = 'warn' WHERE severity = 'warning';
UPDATE flags SET severity = 'critical' WHERE severity = 'error';

ALTER TABLE flags DROP CONSTRAINT flags_severity_check;
ALTER TABLE flags ADD CONSTRAINT flags_severity_check
    CHECK (severity IN ('info', 'warn', 'critical'));
