-- 0013_rls.sql — database-enforced tenant isolation (Phase 5, chunk 1).
--
-- carolus_app is the least-privilege role the portal/agent connect as. Every
-- table with a client_id gets a tenant_isolation policy keyed on the
-- app.current_client GUC; the clients table is keyed on its own id. The
-- pipeline keeps connecting as the table OWNER, which bypasses RLS (we do
-- NOT FORCE it), so existing behavior and tests are unchanged — isolation
-- applies to carolus_app only. An unset GUC makes
-- current_setting('app.current_client', true) NULL → the policy predicate is
-- false → zero rows (fails closed).
--
-- Idempotent across the test fixture's DROP SCHEMA + re-migrate: the role is
-- cluster-level (guarded), grants are no-ops when already held, and the
-- per-table policies are always created against a freshly-rebuilt schema.

-- Roles are cluster-level and survive DROP SCHEMA, so guard creation.
DO $role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'carolus_app') THEN
        CREATE ROLE carolus_app NOLOGIN;  -- NOSUPERUSER, NOBYPASSRLS by default
    END IF;
END
$role$;

-- So the owner (and the tests) can SET ROLE into it.
GRANT carolus_app TO current_user;
GRANT USAGE ON SCHEMA public TO carolus_app;
-- The only sequence in the schema backs qbo_raw's bigserial (a tenant table).
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO carolus_app;

DO $rls$
DECLARE
    t text;
BEGIN
    -- clients is keyed on its own id; every other tenant table on client_id.
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON clients TO carolus_app';
    EXECUTE 'ALTER TABLE clients ENABLE ROW LEVEL SECURITY';
    EXECUTE $pol$
        CREATE POLICY tenant_isolation ON clients FOR ALL TO carolus_app
        USING (id = current_setting('app.current_client', true)::uuid)
        WITH CHECK (id = current_setting('app.current_client', true)::uuid)
    $pol$;

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
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO carolus_app', t);
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format($pol$
            CREATE POLICY tenant_isolation ON %I FOR ALL TO carolus_app
            USING (client_id = current_setting('app.current_client', true)::uuid)
            WITH CHECK (client_id = current_setting('app.current_client', true)::uuid)
        $pol$, t);
    END LOOP;
END
$rls$;
