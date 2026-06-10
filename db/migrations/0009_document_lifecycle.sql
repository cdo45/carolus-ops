-- 0009_document_lifecycle — documents grow the Phase 4 pipeline shape.
--
-- Lifecycle: received -> classified -> (extracted) -> validated ->
-- matched, with escalated as the catch-all terminal for anything the
-- machine cannot prove (escalation_reason says why). sha256 stays
-- unique per client (0001): duplicate upload = same document.
-- 'kind' (placeholder since 0001) becomes doc_type with the agreed enum.
-- extracted holds the parsed payload (statement data / receipt fields);
-- matched_txn links a backed document to its canonical transaction.

ALTER TABLE documents RENAME COLUMN kind TO doc_type;

-- defensive: normalize any pre-lifecycle status values before the check
UPDATE documents SET status = 'received'
    WHERE status NOT IN ('received', 'classified', 'extracted', 'validated',
                         'matched', 'escalated');

ALTER TABLE documents ADD CONSTRAINT documents_doc_type_check CHECK (
    doc_type IS NULL OR doc_type IN
        ('bank_statement', 'receipt', 'invoice', 'contract', 'other')
);
ALTER TABLE documents ADD CONSTRAINT documents_status_check CHECK (
    status IN ('received', 'classified', 'extracted', 'validated',
               'matched', 'escalated')
);

ALTER TABLE documents ADD COLUMN escalation_reason text;
ALTER TABLE documents ADD COLUMN period_start date;
ALTER TABLE documents ADD COLUMN period_end date;
ALTER TABLE documents ADD COLUMN extracted jsonb;
ALTER TABLE documents ADD COLUMN matched_txn uuid REFERENCES transactions(id);
ALTER TABLE documents ADD COLUMN filename text;
ALTER TABLE documents ADD COLUMN content_type text;
ALTER TABLE documents ADD COLUMN source_channel text;

CREATE INDEX documents_matched_txn_idx ON documents (matched_txn);
CREATE INDEX documents_status_idx ON documents (client_id, status);
