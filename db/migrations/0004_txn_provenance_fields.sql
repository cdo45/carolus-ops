-- 0004_txn_provenance_fields — DocNumber + CreateTime onto canonical txns.
--
-- doc_number:     QBO DocNumber (vendor invoice/ref no) — R011 duplicate_bill
-- qbo_created_at: QBO MetaData.CreateTime, content-derived like
--                 qbo_synced_at — R016 backdated_entry
-- Both extracted by sync/transforms.py from the staged payload; re-transform
-- backfills existing rows without re-calling QBO (raw-then-canonical).

ALTER TABLE transactions ADD COLUMN doc_number text;
ALTER TABLE transactions ADD COLUMN qbo_created_at timestamptz;
