-- 0016_tenant_guc_nullif.sql — unscoped tenant policies must DENY, not throw.
--
-- Every tenant_isolation policy (0013 carolus_app, 0015 carolus_agent) read the
-- tenant as current_setting('app.current_client', true)::uuid. That casts the
-- raw GUC: an UNSET GUC reads NULL (NULL::uuid is NULL → row excluded → fine),
-- but a session whose GUC has reverted to '' — what a pooled connection sees
-- after a scoped transaction's SET LOCAL ends — reads '' and ''::uuid RAISES
-- invalid_text_representation. So an unscoped/empty session ERRORS instead of
-- failing closed to zero rows. The bug is latent in 0013/0014 too; their tested
-- paths only ever yield NULL.
--
-- Fix once, for every policy and every future one: read the tenant through a
-- STABLE current_tenant() that NULLIFs '' to NULL before the cast, so '' denies
-- exactly like unset (predicate UNKNOWN → zero rows, no cast error). Only the
-- GUC read changes; every predicate, role list, and command is otherwise
-- untouched (ALTER POLICY ... USING/WITH CHECK leaves TO ... as-is).
--
-- Idempotent across the test fixture's DROP SCHEMA + re-migrate: CREATE OR
-- REPLACE re-defines the function on the rebuilt schema, the EXECUTE grant is a
-- no-op when already held, and the policies (created by 0013/0014/0015 earlier
-- in the same migrate) are re-pointed each time.

CREATE OR REPLACE FUNCTION current_tenant() RETURNS uuid LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_client', true), '')::uuid
$$;

-- The tenant roles evaluate this inside their policies, so they must EXECUTE it.
GRANT EXECUTE ON FUNCTION current_tenant() TO carolus_app, carolus_agent;

DO $repoint$
DECLARE
    t text;
BEGIN
    -- clients (root): keyed on its own id.
    EXECUTE $cl$
        ALTER POLICY tenant_isolation ON clients
        USING (id = current_tenant())
        WITH CHECK (id = current_tenant())
    $cl$;

    -- direct client_id tables: keyed on client_id.
    FOR t IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
          AND EXISTS (
              SELECT 1 FROM pg_attribute a
              WHERE a.attrelid = c.oid AND a.attname = 'client_id'
                AND a.attnum > 0 AND NOT a.attisdropped
          )
        ORDER BY c.relname
    LOOP
        EXECUTE format($ci$
            ALTER POLICY tenant_isolation ON %I
            USING (client_id = current_tenant())
            WITH CHECK (client_id = current_tenant())
        $ci$, t);
    END LOOP;

    -- journal_lines (parent-scoped): the only tenant table without a client_id.
    EXECUTE $jl$
        ALTER POLICY tenant_isolation ON journal_lines
        USING (EXISTS (
            SELECT 1 FROM transactions t
            WHERE t.id = journal_lines.transaction_id
              AND t.client_id = current_tenant()
        ))
        WITH CHECK (EXISTS (
            SELECT 1 FROM transactions t
            WHERE t.id = journal_lines.transaction_id
              AND t.client_id = current_tenant()
        ))
    $jl$;
END
$repoint$;
