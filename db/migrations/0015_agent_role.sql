-- 0015_agent_role.sql — the write-capable agent role behind the full tenant
-- RLS wall (Phase 5 hard wall), including the parent-scoped journal_lines path.
--
-- carolus_agent is the least-privilege role the per-client data steps connect
-- as. Like carolus_app it is tenant-isolated to the client named by the
-- app.current_client GUC, but it also WRITES journal_lines — the one tenant
-- table without a client_id (it scopes through transaction_id ->
-- transactions.client_id). Every direct-client_id table reuses the existing
-- tenant_isolation policy (the role list grows, the predicate does not);
-- journal_lines gets a parent-scoped policy of its own.
--
-- carolus_app (the portal role) is NOT changed in intent here — it simply
-- becomes one of two roles the shared policies name, and it never touches
-- journal_lines. We do NOT FORCE RLS, so the pipeline's OWNER connection keeps
-- bypassing: migrations, sync, and every existing test are unaffected;
-- isolation binds carolus_agent (and carolus_app) only. An unset GUC makes
-- current_setting('app.current_client', true) NULL → the predicate is false →
-- zero rows (fails closed).
--
-- Idempotent across the test fixture's DROP SCHEMA + re-migrate: the role is
-- cluster-level (guarded), grants are no-ops when already held, and the policy
-- ALTERs / the journal_lines policy run against a freshly-rebuilt schema.

-- Roles are cluster-level and survive DROP SCHEMA, so guard creation.
DO $role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'carolus_agent') THEN
        CREATE ROLE carolus_agent NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$role$;

-- So the owner (and the tests) can SET ROLE into it.
GRANT carolus_agent TO current_user;
GRANT USAGE ON SCHEMA public TO carolus_agent;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO carolus_agent;
-- The root and the parent-scoped table get DML directly; every direct
-- client_id table is granted in the loop below.
GRANT SELECT, INSERT, UPDATE, DELETE ON clients TO carolus_agent;
GRANT SELECT, INSERT, UPDATE, DELETE ON journal_lines TO carolus_agent;

DO $wall$
DECLARE
    t text;
BEGIN
    -- DML on every direct client_id table.
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
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO carolus_agent', t);
    END LOOP;

    -- Bring carolus_agent under the SAME tenant_isolation policy as carolus_app
    -- on clients and every direct-client_id table. One policy per table, the
    -- predicate untouched — only the role list grows, looped over the catalog so
    -- coverage can't drift. journal_lines has no tenant_isolation policy yet, so
    -- it is not matched here; its parent-scoped policy is created below.
    FOR t IN
        SELECT c.relname
        FROM pg_policy p
        JOIN pg_class c ON c.oid = p.polrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND p.polname = 'tenant_isolation'
        ORDER BY c.relname
    LOOP
        EXECUTE format(
            'ALTER POLICY tenant_isolation ON %I TO carolus_app, carolus_agent', t
        );
    END LOOP;
END
$wall$;

-- The parent-scoped wall on journal_lines — the only tenant table without a
-- client_id. A line is visible/writable only when its parent transaction
-- belongs to the GUC's client. TO carolus_agent only: carolus_app (the portal)
-- has no grant on journal_lines and never touches it. No FORCE — the owner
-- keeps bypassing, so the pipeline and existing tests are unaffected.
ALTER TABLE journal_lines ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON journal_lines FOR ALL TO carolus_agent
    USING (EXISTS (
        SELECT 1 FROM transactions t
        WHERE t.id = journal_lines.transaction_id
          AND t.client_id = current_setting('app.current_client', true)::uuid
    ))
    WITH CHECK (EXISTS (
        SELECT 1 FROM transactions t
        WHERE t.id = journal_lines.transaction_id
          AND t.client_id = current_setting('app.current_client', true)::uuid
    ));
