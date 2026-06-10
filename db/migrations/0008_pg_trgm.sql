-- 0008_pg_trgm — trigram support for near-duplicate fact detection.
--
-- knowledge/validator.py rejects ops whose statement is >= 0.6 trigram-
-- similar to an existing active fact. pg_trgm is a TRUSTED extension on
-- PG 13+, so the database owner can install it without superuser.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX facts_statement_trgm_idx
    ON facts USING gin (statement gin_trgm_ops);
