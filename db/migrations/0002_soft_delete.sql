-- 0002_soft_delete — CDC deletion handling.
--
-- When CDC reports an entity as status=Deleted we soft-flag the canonical
-- row (first observation wins; rows are NEVER hard-deleted — history and
-- journal lines stay queryable). Set by sync/incremental.py.

ALTER TABLE accounts ADD COLUMN qbo_deleted_at timestamptz;
ALTER TABLE entities ADD COLUMN qbo_deleted_at timestamptz;
ALTER TABLE jobs ADD COLUMN qbo_deleted_at timestamptz;
ALTER TABLE transactions ADD COLUMN qbo_deleted_at timestamptz;
