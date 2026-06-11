-- 0012_sales_tax_account — curated sales-tax liability account per client.
--
-- Some charts of accounts carry MULTIPLE GlobalTaxPayable accounts
-- (e.g. Arizona Dept of Revenue + Board of Equalization); automatic
-- resolution correctly refuses to guess between them. This column is the
-- human answer: CURATED via sync/set_tax_account.py — sync never writes
-- it (same contract as jobs.status/completed_at, contract_amount,
-- doc_status). Transform resolution order: this column first, then the
-- single-candidate fallback, then a self-explaining transform_warning.

ALTER TABLE clients ADD COLUMN sales_tax_account_id uuid
    REFERENCES accounts(id);
CREATE INDEX clients_sales_tax_account_id_idx
    ON clients (sales_tax_account_id);
