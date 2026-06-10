-- 0005_linked_txn — application-link presence on canonical transactions.
--
-- has_linked_txn: whether the payload's LinkedTxn entries apply this
-- transaction to another (BillPayment -> Bill, Payment -> Invoice).
-- NULL = not applicable for the type (or unknown); transforms set
-- true/false for Payment and BillPayment. Used by R022
-- (payment_without_bill) and R033 (deposit_unapplied).

ALTER TABLE transactions ADD COLUMN has_linked_txn boolean;
