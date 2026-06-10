-- 0007_facts_taxonomy — knowledge-layer fact schema.
--
-- 1) facts.category moves to the agreed knowledge taxonomy. Ordering per
--    0003's lesson: DROP constraint -> remap rows -> ADD constraint.
--    The facts append-only trigger blocks content rewrites by design;
--    a taxonomy migration IS a sanctioned rewrite, so the trigger is
--    disabled for the remap inside this transaction and re-enabled after.
--    Remap (defensive — written even if no rows exist yet):
--      financial / tax / compliance -> accounting_policy
--      operational                  -> operations
--      preference                   -> preferences
--      context                      -> entity_profile
--
-- 2) facts.effective_date (date the fact became true, distinct from
--    created_at = when we learned it). Part of the knowledge-layer fact
--    shape: validator accepts it, renderer sorts by it. Immutable content
--    -> added to the append-only trigger's guarded columns.

ALTER TABLE facts DROP CONSTRAINT facts_category_check;
ALTER TABLE facts DISABLE TRIGGER facts_append_only;

UPDATE facts SET category = 'accounting_policy'
    WHERE category IN ('financial', 'tax', 'compliance');
UPDATE facts SET category = 'operations' WHERE category = 'operational';
UPDATE facts SET category = 'preferences' WHERE category = 'preference';
UPDATE facts SET category = 'entity_profile' WHERE category = 'context';

ALTER TABLE facts ENABLE TRIGGER facts_append_only;
ALTER TABLE facts ADD CONSTRAINT facts_category_check CHECK (
    category IN ('entity_profile', 'operations', 'accounting_policy',
                 'relationships', 'preferences', 'watch_items',
                 'resolved_history')
);

ALTER TABLE facts ADD COLUMN effective_date date;

CREATE OR REPLACE FUNCTION facts_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'facts are append-only: DELETE not allowed (supersede instead)';
    END IF;
    IF (NEW.id, NEW.client_id, NEW.category, NEW.statement, NEW.source_type,
        NEW.source_ref, NEW.confidence, NEW.created_at, NEW.effective_date)
       IS DISTINCT FROM
       (OLD.id, OLD.client_id, OLD.category, OLD.statement, OLD.source_type,
        OLD.source_ref, OLD.confidence, OLD.created_at, OLD.effective_date) THEN
        RAISE EXCEPTION 'facts are append-only: only status/superseded_by may change';
    END IF;
    RETURN NEW;
END
$$;
