"""Deterministic staging -> canonical transforms.

Reads the latest qbo_raw payload per (entity_type, qbo_id) and upserts
canonical rows. Pure content-driven: re-running against unchanged staging
data writes ZERO rows (upserts carry IS DISTINCT FROM guards), so canonical
can be rebuilt or re-transformed at any time without re-calling QBO.

Debit/credit conventions per transaction type live in docs/QBO_MAPPING.md —
keep that file in lockstep with _BUILDERS below. Anything the mapping cannot
resolve or balance is written as far as it goes and flagged with rule_code
'transform_warning' (never silently wrong).

Curated columns are never touched by sync: transactions.doc_status,
transactions.review_tier, jobs.contract_amount.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

import psycopg

# Entity types we stage. Item is staging-only reference data: item-based
# invoice/bill lines carry only ItemRef, so the item's Income/ExpenseAccountRef
# is required to resolve them to accounts. Employee is deferred to P8.
REFERENCE_ENTITIES: tuple[str, ...] = ("Account", "Customer", "Vendor", "Item")
TRANSACTION_ENTITIES: tuple[str, ...] = (
    "Invoice",
    "Bill",
    "Payment",
    "BillPayment",
    "Purchase",
    "JournalEntry",
    "Deposit",
    "CreditMemo",
    "VendorCredit",
)
ALL_ENTITIES: tuple[str, ...] = REFERENCE_ENTITIES + TRANSACTION_ENTITIES

TRANSFORM_WARNING = "transform_warning"

Payload = Mapping[str, Any]


# ---------------------------------------------------------------- helpers


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")


def _is_deleted(payload: Payload) -> bool:
    """CDC deletion stubs ({Id, status: Deleted}) carry no entity content —
    they must never be transformed into canonical values. Deletion itself is
    applied separately by sync.incremental (soft flag, never hard delete)."""
    return payload.get("status") == "Deleted"


def _ref_value(ref: Any) -> str | None:
    if isinstance(ref, Mapping):
        value = ref.get("value")
        return str(value) if value is not None else None
    return None


def _txn_date(payload: Payload) -> date | None:
    raw = payload.get("TxnDate")
    return date.fromisoformat(raw) if raw else None


def _qbo_last_updated(payload: Payload) -> datetime | None:
    """QBO's own LastUpdatedTime — content-derived, so re-transforms of the
    same payload produce the same value (no idempotency drift)."""
    raw = (payload.get("MetaData") or {}).get("LastUpdatedTime")
    return datetime.fromisoformat(raw) if raw else None


def _qbo_created(payload: Payload) -> datetime | None:
    """QBO's CreateTime — when the entry was keyed in, vs txn_date when it
    claims to have happened. Content-derived (no drift)."""
    raw = (payload.get("MetaData") or {}).get("CreateTime")
    return datetime.fromisoformat(raw) if raw else None


def _doc_number(payload: Payload) -> str | None:
    raw = payload.get("DocNumber")
    text = str(raw).strip() if raw is not None else ""
    return text or None


# Types whose payloads carry application links worth tracking.
_LINKED_TYPES: frozenset[str] = frozenset({"Payment", "BillPayment"})


def _has_linked_txn(txn_type: str, payload: Payload) -> bool | None:
    """True/False for Payment & BillPayment (is this applied to anything?),
    NULL for types where the question does not apply."""
    if txn_type not in _LINKED_TYPES:
        return None
    candidates = list(payload.get("LinkedTxn") or [])
    for line in payload.get("Line", []):
        candidates.extend(line.get("LinkedTxn") or [])
    return any(linked.get("TxnId") for linked in candidates)


# ---------------------------------------------------------------- resolver


@dataclass(frozen=True)
class AccountInfo:
    id: UUID
    acct_type: str | None
    acct_subtype: str | None


@dataclass(frozen=True)
class ItemAccounts:
    income_account_qbo_id: str | None
    expense_account_qbo_id: str | None


@dataclass
class Resolver:
    """Lookup maps the line builders need, all keyed by QBO ids."""

    accounts: dict[str, AccountInfo] = field(default_factory=dict)
    items: dict[str, ItemAccounts] = field(default_factory=dict)
    entities: dict[tuple[str, str], UUID] = field(default_factory=dict)
    jobs: dict[str, UUID] = field(default_factory=dict)

    def account_id(self, qbo_id: str | None) -> UUID | None:
        if qbo_id is None:
            return None
        info = self.accounts.get(qbo_id)
        return info.id if info else None

    def _single_account(self, *, acct_type: str | None = None,
                        acct_subtype: str | None = None) -> UUID | None:
        matches = [
            info
            for info in self.accounts.values()
            if (acct_type is None or info.acct_type == acct_type)
            and (acct_subtype is None or info.acct_subtype == acct_subtype)
        ]
        return matches[0].id if len(matches) == 1 else None

    @property
    def ar_account_id(self) -> UUID | None:
        return self._single_account(acct_type="Accounts Receivable")

    @property
    def ap_account_id(self) -> UUID | None:
        return self._single_account(acct_type="Accounts Payable")

    @property
    def undeposited_funds_id(self) -> UUID | None:
        return self._single_account(acct_subtype="UndepositedFunds")

    def item_income_account(self, item_qbo_id: str | None) -> UUID | None:
        if item_qbo_id is None or item_qbo_id not in self.items:
            return None
        return self.account_id(self.items[item_qbo_id].income_account_qbo_id)

    def item_expense_account(self, item_qbo_id: str | None) -> UUID | None:
        if item_qbo_id is None or item_qbo_id not in self.items:
            return None
        return self.account_id(self.items[item_qbo_id].expense_account_qbo_id)


# ---------------------------------------------------------------- line specs


@dataclass(frozen=True)
class RawLine:
    account_id: UUID
    amount: Decimal
    posting_type: str  # 'debit' | 'credit'
    job_id: UUID | None = None
    description: str | None = None

    def normalized(self) -> RawLine:
        """Store positive amounts; a negative amount flips the posting side."""
        if self.amount < 0:
            flipped = "credit" if self.posting_type == "debit" else "debit"
            return RawLine(
                self.account_id, -self.amount, flipped, self.job_id, self.description
            )
        return self


@dataclass(frozen=True)
class LineSpec:
    line_no: int
    account_id: UUID
    amount: Decimal
    posting_type: str
    job_id: UUID | None
    description: str | None


BuildResult = tuple[list[RawLine], list[str]]


def _header_job(payload: Payload, r: Resolver) -> UUID | None:
    """If the header customer is a QBO job (sub-customer), tag its lines."""
    return r.jobs.get(_ref_value(payload.get("CustomerRef")) or "")


def _expense_lines(payload: Payload, r: Resolver, posting: str) -> BuildResult:
    """AccountBased / ItemBased expense lines (Bill, Purchase, VendorCredit)."""
    lines: list[RawLine] = []
    warns: list[str] = []
    for line in payload.get("Line", []):
        amount = _dec(line.get("Amount"))
        if "AccountBasedExpenseLineDetail" in line:
            detail = line["AccountBasedExpenseLineDetail"]
            account = r.account_id(_ref_value(detail.get("AccountRef")))
            if account is None:
                warns.append("expense line: unresolvable AccountRef")
                continue
        elif "ItemBasedExpenseLineDetail" in line:
            detail = line["ItemBasedExpenseLineDetail"]
            item = _ref_value(detail.get("ItemRef"))
            account = r.item_expense_account(item)
            if account is None:
                warns.append(f"expense line: no expense account for item {item!r}")
                continue
        else:
            continue  # subtotal / description-only lines carry no posting
        if amount == 0:
            continue
        job = r.jobs.get(_ref_value(detail.get("CustomerRef")) or "")
        lines.append(
            RawLine(account, amount, posting, job, line.get("Description"))
        )
    return lines, warns


def _sales_lines(payload: Payload, r: Resolver, posting: str) -> BuildResult:
    """SalesItemLineDetail lines (Invoice, CreditMemo) + discount lines."""
    lines: list[RawLine] = []
    warns: list[str] = []
    job = _header_job(payload, r)
    contra = "debit" if posting == "credit" else "credit"
    for line in payload.get("Line", []):
        amount = _dec(line.get("Amount"))
        if "SalesItemLineDetail" in line:
            item = _ref_value(line["SalesItemLineDetail"].get("ItemRef"))
            account = r.item_income_account(item)
            if account is None:
                warns.append(f"sales line: no income account for item {item!r}")
                continue
            if amount == 0:
                continue
            lines.append(RawLine(account, amount, posting, job, line.get("Description")))
        elif "DiscountLineDetail" in line and amount != 0:
            account = r.account_id(
                _ref_value(line["DiscountLineDetail"].get("DiscountAccountRef"))
            )
            if account is None:
                warns.append("discount line without resolvable account")
                continue
            lines.append(RawLine(account, amount, contra, job, "discount"))
    tax = _dec((payload.get("TxnTaxDetail") or {}).get("TotalTax", 0))
    if tax != 0:
        warns.append(f"sales tax {tax} not mapped in P1")
    return lines, warns


def _lines_invoice(payload: Payload, r: Resolver) -> BuildResult:
    # Invoice: debit A/R for the total, credit income per sales line.
    lines, warns = _sales_lines(payload, r, "credit")
    total = _dec(payload.get("TotalAmt"))
    ar = r.account_id(_ref_value(payload.get("ARAccountRef"))) or r.ar_account_id
    if ar is None:
        warns.append("no Accounts Receivable account resolvable")
    elif total != 0:
        lines.insert(0, RawLine(ar, total, "debit", _header_job(payload, r), "A/R"))
    return lines, warns


def _lines_credit_memo(payload: Payload, r: Resolver) -> BuildResult:
    # CreditMemo: invoice reversal — debit income lines, credit A/R.
    lines, warns = _sales_lines(payload, r, "debit")
    total = _dec(payload.get("TotalAmt"))
    ar = r.account_id(_ref_value(payload.get("ARAccountRef"))) or r.ar_account_id
    if ar is None:
        warns.append("no Accounts Receivable account resolvable")
    elif total != 0:
        lines.insert(0, RawLine(ar, total, "credit", _header_job(payload, r), "A/R"))
    return lines, warns


def _lines_bill(payload: Payload, r: Resolver) -> BuildResult:
    # Bill: credit A/P for the total, debit expense lines.
    lines, warns = _expense_lines(payload, r, "debit")
    total = _dec(payload.get("TotalAmt"))
    ap = r.account_id(_ref_value(payload.get("APAccountRef"))) or r.ap_account_id
    if ap is None:
        warns.append("no Accounts Payable account resolvable")
    elif total != 0:
        lines.insert(0, RawLine(ap, total, "credit", None, "A/P"))
    return lines, warns


def _lines_vendor_credit(payload: Payload, r: Resolver) -> BuildResult:
    # VendorCredit: bill reversal — debit A/P, credit expense lines.
    lines, warns = _expense_lines(payload, r, "credit")
    total = _dec(payload.get("TotalAmt"))
    ap = r.account_id(_ref_value(payload.get("APAccountRef"))) or r.ap_account_id
    if ap is None:
        warns.append("no Accounts Payable account resolvable")
    elif total != 0:
        lines.insert(0, RawLine(ap, total, "debit", None, "A/P"))
    return lines, warns


def _lines_payment(payload: Payload, r: Resolver) -> BuildResult:
    # Customer payment: debit bank/undeposited funds, credit A/R.
    warns: list[str] = []
    total = _dec(payload.get("TotalAmt"))
    if total == 0:
        return [], warns
    deposit_to = (
        r.account_id(_ref_value(payload.get("DepositToAccountRef")))
        or r.undeposited_funds_id
    )
    ar = r.account_id(_ref_value(payload.get("ARAccountRef"))) or r.ar_account_id
    lines: list[RawLine] = []
    if deposit_to is None:
        warns.append("payment: no deposit-to or Undeposited Funds account")
    else:
        lines.append(RawLine(deposit_to, total, "debit", None, "payment received"))
    if ar is None:
        warns.append("no Accounts Receivable account resolvable")
    else:
        lines.append(RawLine(ar, total, "credit", None, "A/R"))
    return lines, warns


def _lines_bill_payment(payload: Payload, r: Resolver) -> BuildResult:
    # BillPayment: debit A/P, credit the paying bank / credit-card account.
    warns: list[str] = []
    total = _dec(payload.get("TotalAmt"))
    if total == 0:
        return [], warns
    pay_type = payload.get("PayType")
    if pay_type == "Check":
        pay_account = r.account_id(
            _ref_value((payload.get("CheckPayment") or {}).get("BankAccountRef"))
        )
    elif pay_type == "CreditCard":
        pay_account = r.account_id(
            _ref_value((payload.get("CreditCardPayment") or {}).get("CCAccountRef"))
        )
    else:
        pay_account = None
        warns.append(f"bill payment: unhandled PayType {pay_type!r}")
    ap = r.account_id(_ref_value(payload.get("APAccountRef"))) or r.ap_account_id
    lines: list[RawLine] = []
    if ap is None:
        warns.append("no Accounts Payable account resolvable")
    else:
        lines.append(RawLine(ap, total, "debit", None, "A/P"))
    if pay_account is None:
        if pay_type in ("Check", "CreditCard"):
            warns.append("bill payment: unresolvable payment account")
    else:
        lines.append(RawLine(pay_account, total, "credit", None, "bill payment"))
    return lines, warns


def _lines_purchase(payload: Payload, r: Resolver) -> BuildResult:
    # Purchase (check/cc/cash expense): credit the payment account, debit
    # expense lines. Credit=true means a refund — directions flip.
    lines, warns = _expense_lines(payload, r, "debit")
    total = _dec(payload.get("TotalAmt"))
    pay_account = r.account_id(_ref_value(payload.get("AccountRef")))
    if pay_account is None:
        warns.append("purchase: unresolvable payment AccountRef")
    elif total != 0:
        lines.insert(0, RawLine(pay_account, total, "credit", None, "payment account"))
    if payload.get("Credit") is True:
        lines = [
            RawLine(
                line.account_id,
                line.amount,
                "credit" if line.posting_type == "debit" else "debit",
                line.job_id,
                line.description,
            )
            for line in lines
        ]
    return lines, warns


def _lines_deposit(payload: Payload, r: Resolver) -> BuildResult:
    # Deposit: debit the bank account, credit each deposit line's account.
    lines: list[RawLine] = []
    warns: list[str] = []
    total = _dec(payload.get("TotalAmt"))
    bank = r.account_id(_ref_value(payload.get("DepositToAccountRef")))
    if bank is None:
        warns.append("deposit: unresolvable DepositToAccountRef")
    elif total != 0:
        lines.append(RawLine(bank, total, "debit", None, "deposit"))
    for line in payload.get("Line", []):
        if "DepositLineDetail" not in line:
            continue
        amount = _dec(line.get("Amount"))
        if amount == 0:
            continue
        account = r.account_id(
            _ref_value(line["DepositLineDetail"].get("AccountRef"))
        )
        if account is None:
            warns.append("deposit line: unresolvable AccountRef")
            continue
        lines.append(RawLine(account, amount, "credit", None, line.get("Description")))
    return lines, warns


def _lines_journal_entry(payload: Payload, r: Resolver) -> BuildResult:
    # JournalEntry: postings are explicit in the payload — pass through.
    lines: list[RawLine] = []
    warns: list[str] = []
    for line in payload.get("Line", []):
        detail = line.get("JournalEntryLineDetail")
        if not detail:
            continue
        amount = _dec(line.get("Amount"))
        if amount == 0:
            continue
        account = r.account_id(_ref_value(detail.get("AccountRef")))
        if account is None:
            warns.append("journal line: unresolvable AccountRef")
            continue
        posting = str(detail.get("PostingType", "")).lower()
        if posting not in ("debit", "credit"):
            warns.append(f"journal line: bad PostingType {detail.get('PostingType')!r}")
            continue
        lines.append(RawLine(account, amount, posting, None, line.get("Description")))
    return lines, warns


_BUILDERS: dict[str, Any] = {
    "Invoice": _lines_invoice,
    "Bill": _lines_bill,
    "Payment": _lines_payment,
    "BillPayment": _lines_bill_payment,
    "Purchase": _lines_purchase,
    "JournalEntry": _lines_journal_entry,
    "Deposit": _lines_deposit,
    "CreditMemo": _lines_credit_memo,
    "VendorCredit": _lines_vendor_credit,
}


def build_journal_lines(
    txn_type: str, payload: Payload, resolver: Resolver
) -> tuple[list[LineSpec], list[str]]:
    """Build balanced journal lines for one transaction payload.

    Returns (lines, warnings). Lines are numbered 0..n in build order;
    an imbalance or unresolvable reference appends a warning — the caller
    flags those as transform_warning rather than dropping the transaction.
    """
    builder = _BUILDERS.get(txn_type)
    if builder is None:
        return [], [f"no journal-line mapping for {txn_type}"]
    raw, warns = builder(payload, resolver)
    normalized = [line.normalized() for line in raw]
    debits = sum(
        (line.amount for line in normalized if line.posting_type == "debit"),
        Decimal("0.00"),
    )
    credits = sum(
        (line.amount for line in normalized if line.posting_type == "credit"),
        Decimal("0.00"),
    )
    if debits != credits:
        warns.append(f"unbalanced lines: debits {debits} != credits {credits}")
    lines = [
        LineSpec(
            line_no=index,
            account_id=line.account_id,
            amount=line.amount,
            posting_type=line.posting_type,
            job_id=line.job_id,
            description=line.description,
        )
        for index, line in enumerate(normalized)
    ]
    return lines, warns


# ---------------------------------------------------------------- header rows

_HEADER_ENTITY: dict[str, tuple[str, str]] = {
    "Invoice": ("CustomerRef", "customer"),
    "Payment": ("CustomerRef", "customer"),
    "CreditMemo": ("CustomerRef", "customer"),
    "Bill": ("VendorRef", "vendor"),
    "BillPayment": ("VendorRef", "vendor"),
    "VendorCredit": ("VendorRef", "vendor"),
}


def header_entity_id(
    txn_type: str, payload: Payload, resolver: Resolver
) -> UUID | None:
    if txn_type == "Purchase":
        ref = payload.get("EntityRef")
        kind = str(ref.get("Type", "")).lower() if isinstance(ref, Mapping) else ""
        if kind in ("customer", "vendor", "employee"):
            qbo_id = _ref_value(ref)
            return resolver.entities.get((qbo_id or "", kind))
        return None
    spec = _HEADER_ENTITY.get(txn_type)
    if spec is None:
        return None
    ref_field, kind = spec
    qbo_id = _ref_value(payload.get(ref_field))
    return resolver.entities.get((qbo_id or "", kind))


def transaction_amount(txn_type: str, payload: Payload) -> Decimal:
    if txn_type == "JournalEntry":
        total = Decimal("0.00")
        for line in payload.get("Line", []):
            detail = line.get("JournalEntryLineDetail")
            if detail and str(detail.get("PostingType", "")).lower() == "debit":
                total += _dec(line.get("Amount"))
        return total
    return _dec(payload.get("TotalAmt"))


# ---------------------------------------------------------------- persistence


@dataclass
class TransformResult:
    written: dict[str, int]
    flags_created: int

    @property
    def total_written(self) -> int:
        return sum(self.written.values()) + self.flags_created


def latest_staged(
    conn: psycopg.Connection, client_id: UUID, entity_types: Sequence[str]
) -> dict[str, dict[str, dict[str, Any]]]:
    """Latest payload per (entity_type, qbo_id) from the append-only staging log."""
    rows = conn.execute(
        """
        SELECT DISTINCT ON (entity_type, qbo_id) entity_type, qbo_id, payload
        FROM qbo_raw
        WHERE client_id = %s AND entity_type = ANY(%s)
        ORDER BY entity_type, qbo_id, fetched_at DESC, id DESC
        """,
        (client_id, list(entity_types)),
    ).fetchall()
    staged: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for entity_type, qbo_id, payload in rows:
        staged[entity_type][qbo_id] = payload
    return dict(staged)


def _upsert_account(conn: psycopg.Connection, client_id: UUID, p: Payload) -> int:
    cur = conn.execute(
        """
        INSERT INTO accounts (client_id, qbo_id, name, acct_type, acct_subtype, active)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (client_id, qbo_id) DO UPDATE SET
            name = excluded.name,
            acct_type = excluded.acct_type,
            acct_subtype = excluded.acct_subtype,
            active = excluded.active
        WHERE (accounts.name, accounts.acct_type, accounts.acct_subtype, accounts.active)
            IS DISTINCT FROM
            (excluded.name, excluded.acct_type, excluded.acct_subtype, excluded.active)
        """,
        (
            client_id,
            str(p["Id"]),
            p.get("Name") or f"Account {p['Id']}",
            p.get("AccountType"),
            p.get("AccountSubType"),
            bool(p.get("Active", True)),
        ),
    )
    return cur.rowcount


def _upsert_entity(
    conn: psycopg.Connection, client_id: UUID, kind: str, p: Payload
) -> int:
    cur = conn.execute(
        """
        INSERT INTO entities (client_id, qbo_id, kind, name, active)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (client_id, qbo_id, kind) DO UPDATE SET
            name = excluded.name,
            active = excluded.active
        WHERE (entities.name, entities.active)
            IS DISTINCT FROM (excluded.name, excluded.active)
        """,
        (
            client_id,
            str(p["Id"]),
            kind,
            p.get("DisplayName") or f"{kind} {p['Id']}",
            bool(p.get("Active", True)),
        ),
    )
    return cur.rowcount


def _upsert_job(
    conn: psycopg.Connection,
    client_id: UUID,
    p: Payload,
    parent_entity_id: UUID | None,
) -> int:
    # contract_amount is curated by Carlos — set only at insert (NULL).
    cur = conn.execute(
        """
        INSERT INTO jobs (client_id, qbo_id, entity_id, name, status)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (client_id, qbo_id) DO UPDATE SET
            entity_id = excluded.entity_id,
            name = excluded.name,
            status = excluded.status
        WHERE (jobs.entity_id, jobs.name, jobs.status)
            IS DISTINCT FROM (excluded.entity_id, excluded.name, excluded.status)
        """,
        (
            client_id,
            str(p["Id"]),
            parent_entity_id,
            p.get("FullyQualifiedName") or p.get("DisplayName") or f"Job {p['Id']}",
            "active" if p.get("Active", True) else "inactive",
        ),
    )
    return cur.rowcount


def _upsert_transaction(
    conn: psycopg.Connection,
    client_id: UUID,
    txn_type: str,
    p: Payload,
    entity_id: UUID | None,
) -> tuple[UUID, int]:
    """Upsert one transaction header; returns (id, rows_written).

    doc_status / review_tier are curated and never overwritten here.
    """
    qbo_id = str(p["Id"])
    cur = conn.execute(
        """
        INSERT INTO transactions
            (client_id, qbo_id, txn_type, txn_date, amount, entity_id,
             qbo_synced_at, doc_number, qbo_created_at, has_linked_txn)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (client_id, qbo_id, txn_type) DO UPDATE SET
            txn_date = excluded.txn_date,
            amount = excluded.amount,
            entity_id = excluded.entity_id,
            qbo_synced_at = excluded.qbo_synced_at,
            doc_number = excluded.doc_number,
            qbo_created_at = excluded.qbo_created_at,
            has_linked_txn = excluded.has_linked_txn
        WHERE (transactions.txn_date, transactions.amount, transactions.entity_id,
               transactions.qbo_synced_at, transactions.doc_number,
               transactions.qbo_created_at, transactions.has_linked_txn)
            IS DISTINCT FROM
            (excluded.txn_date, excluded.amount, excluded.entity_id,
             excluded.qbo_synced_at, excluded.doc_number, excluded.qbo_created_at,
             excluded.has_linked_txn)
        RETURNING id
        """,
        (
            client_id,
            qbo_id,
            txn_type,
            _txn_date(p),
            transaction_amount(txn_type, p),
            entity_id,
            _qbo_last_updated(p),
            _doc_number(p),
            _qbo_created(p),
            _has_linked_txn(txn_type, p),
        ),
    )
    row = cur.fetchone()
    written = cur.rowcount
    if row is None:  # conflict with no change -> RETURNING yields nothing
        row = conn.execute(
            "SELECT id FROM transactions"
            " WHERE client_id = %s AND qbo_id = %s AND txn_type = %s",
            (client_id, qbo_id, txn_type),
        ).fetchone()
        written = 0
    assert row is not None
    return row[0], written


def _upsert_lines(
    conn: psycopg.Connection, transaction_id: UUID, lines: Sequence[LineSpec]
) -> int:
    written = 0
    for line in lines:
        cur = conn.execute(
            """
            INSERT INTO journal_lines
                (transaction_id, line_no, account_id, job_id, amount,
                 posting_type, description)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (transaction_id, line_no) DO UPDATE SET
                account_id = excluded.account_id,
                job_id = excluded.job_id,
                amount = excluded.amount,
                posting_type = excluded.posting_type,
                description = excluded.description
            WHERE (journal_lines.account_id, journal_lines.job_id,
                   journal_lines.amount, journal_lines.posting_type,
                   journal_lines.description)
                IS DISTINCT FROM
                (excluded.account_id, excluded.job_id, excluded.amount,
                 excluded.posting_type, excluded.description)
            """,
            (
                transaction_id,
                line.line_no,
                line.account_id,
                line.job_id,
                line.amount,
                line.posting_type,
                line.description,
            ),
        )
        written += cur.rowcount
    stale = conn.execute(
        "DELETE FROM journal_lines WHERE transaction_id = %s AND line_no >= %s",
        (transaction_id, len(lines)),
    )
    return written + stale.rowcount


def _flag_once(
    conn: psycopg.Connection,
    client_id: UUID,
    rule_code: str,
    source_type: str,
    source_ref: str,
    detail: str,
) -> int:
    """Idempotent flag insert: one open flag per (client, rule, source_ref)."""
    cur = conn.execute(
        """
        INSERT INTO flags
            (client_id, rule_code, severity, status, source_type, source_ref, detail)
        SELECT %(client_id)s, %(rule_code)s, 'warn', 'open',
               %(source_type)s, %(source_ref)s, %(detail)s
        WHERE NOT EXISTS (
            SELECT 1 FROM flags
            WHERE client_id = %(client_id)s AND rule_code = %(rule_code)s
              AND source_ref = %(source_ref)s AND status = 'open'
        )
        """,
        {
            "client_id": client_id,
            "rule_code": rule_code,
            "source_type": source_type,
            "source_ref": source_ref,
            "detail": detail,
        },
    )
    return cur.rowcount


def _build_resolver(conn: psycopg.Connection, client_id: UUID,
                    staged_items: Mapping[str, Payload]) -> Resolver:
    resolver = Resolver()
    for qbo_id, acct_type, acct_subtype, account_id in conn.execute(
        "SELECT qbo_id, acct_type, acct_subtype, id FROM accounts WHERE client_id = %s",
        (client_id,),
    ).fetchall():
        resolver.accounts[qbo_id] = AccountInfo(account_id, acct_type, acct_subtype)
    for qbo_id, kind, entity_id in conn.execute(
        "SELECT qbo_id, kind, id FROM entities WHERE client_id = %s", (client_id,)
    ).fetchall():
        resolver.entities[(qbo_id, kind)] = entity_id
    for qbo_id, job_id in conn.execute(
        "SELECT qbo_id, id FROM jobs WHERE client_id = %s", (client_id,)
    ).fetchall():
        resolver.jobs[qbo_id] = job_id
    for qbo_id, payload in staged_items.items():
        if _is_deleted(payload):
            continue
        resolver.items[qbo_id] = ItemAccounts(
            income_account_qbo_id=_ref_value(payload.get("IncomeAccountRef")),
            expense_account_qbo_id=_ref_value(payload.get("ExpenseAccountRef")),
        )
    return resolver


def transform_client(conn: psycopg.Connection, client_id: UUID) -> TransformResult:
    """Map latest staged payloads to canonical rows for one client.

    Safe to re-run any time: unchanged staging data writes zero rows.
    """
    staged = latest_staged(conn, client_id, ALL_ENTITIES)
    written: dict[str, int] = defaultdict(int)
    flags_created = 0

    for payload in staged.get("Account", {}).values():
        if _is_deleted(payload):
            continue
        written["accounts"] += _upsert_account(conn, client_id, payload)
    customers = {
        qbo_id: payload
        for qbo_id, payload in staged.get("Customer", {}).items()
        if not _is_deleted(payload)
    }
    for payload in customers.values():
        written["entities"] += _upsert_entity(conn, client_id, "customer", payload)
    for payload in staged.get("Vendor", {}).values():
        if _is_deleted(payload):
            continue
        written["entities"] += _upsert_entity(conn, client_id, "vendor", payload)

    # QBO jobs are sub-customers (Job=true); parent comes from ParentRef.
    entity_ids: dict[tuple[str, str], UUID] = {
        (qbo_id, kind): entity_id
        for qbo_id, kind, entity_id in conn.execute(
            "SELECT qbo_id, kind, id FROM entities WHERE client_id = %s",
            (client_id,),
        ).fetchall()
    }
    for payload in customers.values():
        if payload.get("Job") is True:
            parent_qbo = _ref_value(payload.get("ParentRef"))
            parent = entity_ids.get((parent_qbo or "", "customer"))
            written["jobs"] += _upsert_job(conn, client_id, payload, parent)

    resolver = _build_resolver(conn, client_id, staged.get("Item", {}))

    for txn_type in TRANSACTION_ENTITIES:
        for qbo_id, payload in staged.get(txn_type, {}).items():
            if _is_deleted(payload):
                continue
            entity_id = header_entity_id(txn_type, payload, resolver)
            txn_id, txn_written = _upsert_transaction(
                conn, client_id, txn_type, payload, entity_id
            )
            written["transactions"] += txn_written
            lines, warns = build_journal_lines(txn_type, payload, resolver)
            written["journal_lines"] += _upsert_lines(conn, txn_id, lines)
            if warns:
                flags_created += _flag_once(
                    conn,
                    client_id,
                    TRANSFORM_WARNING,
                    "transaction",
                    f"qbo:{txn_type}:{qbo_id}",
                    "; ".join(warns),
                )
    conn.commit()
    return TransformResult(written=dict(written), flags_created=flags_created)
