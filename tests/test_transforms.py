"""Pure unit tests for the staging->canonical line builders (no DB).

The debit/credit conventions asserted here mirror docs/QBO_MAPPING.md.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sync.transforms import (
    AccountInfo,
    ItemAccounts,
    Resolver,
    build_journal_lines,
    header_entity_id,
    transaction_amount,
)

AR = uuid4()
AP = uuid4()
BANK = uuid4()
UNDEPOSITED = uuid4()
INCOME = uuid4()
MATERIALS = uuid4()
OFFICE = uuid4()
CARD = uuid4()
TAXPAY = uuid4()
TAXPAY2 = uuid4()
CUSTOMER = uuid4()
VENDOR = uuid4()
JOB = uuid4()


def resolver(*, with_tax_account: bool = True,
             extra_tax_account: bool = False,
             curated_tax: object = None) -> Resolver:
    accounts = {
        "1": AccountInfo(BANK, "Bank", "Checking"),
        "2": AccountInfo(UNDEPOSITED, "Other Current Asset", "UndepositedFunds"),
        "20": AccountInfo(AR, "Accounts Receivable", None),
        "21": AccountInfo(AP, "Accounts Payable", None),
        "30": AccountInfo(INCOME, "Income", None),
        "40": AccountInfo(MATERIALS, "Cost of Goods Sold", None),
        "41": AccountInfo(OFFICE, "Expense", None),
        "50": AccountInfo(CARD, "Credit Card", "CreditCard"),
    }
    if with_tax_account:
        accounts["70"] = AccountInfo(
            TAXPAY, "Other Current Liability", "GlobalTaxPayable",
            "Arizona Dept of Revenue",
        )
    if extra_tax_account:
        accounts["71"] = AccountInfo(
            TAXPAY2, "Other Current Liability", "GlobalTaxPayable",
            "Board of Equalization",
        )
    return Resolver(
        curated_tax_account_id=curated_tax,  # type: ignore[arg-type]
        accounts=accounts,
        items={
            "100": ItemAccounts("30", None),
            "101": ItemAccounts("30", "40"),
        },
        entities={("200", "customer"): CUSTOMER, ("300", "vendor"): VENDOR,
                  ("201", "customer"): uuid4()},
        jobs={"201": JOB},
    )


def by_side(lines: list[Any]) -> tuple[dict[UUID, Decimal], dict[UUID, Decimal]]:
    debits: dict[UUID, Decimal] = {}
    credits: dict[UUID, Decimal] = {}
    for line in lines:
        side = debits if line.posting_type == "debit" else credits
        side[line.account_id] = side.get(line.account_id, Decimal(0)) + line.amount
    return debits, credits


def test_invoice_balances_and_attributes_job() -> None:
    payload = {
        "Id": "1001", "TotalAmt": 1500.00, "CustomerRef": {"value": "201"},
        "Line": [
            {"Amount": 1000.00, "SalesItemLineDetail": {"ItemRef": {"value": "100"}}},
            {"Amount": 500.00, "SalesItemLineDetail": {"ItemRef": {"value": "101"}}},
        ],
    }
    lines, warns = build_journal_lines("Invoice", payload, resolver())
    assert warns == []
    debits, credits = by_side(lines)
    assert debits == {AR: Decimal("1500.00")}
    assert credits == {INCOME: Decimal("1500.00")}
    assert [line.line_no for line in lines] == [0, 1, 2]
    assert all(line.job_id == JOB for line in lines), "header job tags every line"


def test_invoice_unknown_item_is_flagged_not_silent() -> None:
    payload = {
        "Id": "1002", "TotalAmt": 250.00, "CustomerRef": {"value": "200"},
        "Line": [
            {"Amount": 250.00, "SalesItemLineDetail": {"ItemRef": {"value": "999"}}},
        ],
    }
    lines, warns = build_journal_lines("Invoice", payload, resolver())
    assert any("no income account" in w for w in warns)
    assert any("unbalanced" in w for w in warns)
    debits, credits = by_side(lines)
    assert debits == {AR: Decimal("250.00")} and credits == {}


def test_bill_credits_ap_and_takes_line_level_job() -> None:
    payload = {
        "Id": "2001", "TotalAmt": 320.00, "VendorRef": {"value": "300"},
        "Line": [
            {"Amount": 200.00, "AccountBasedExpenseLineDetail": {
                "AccountRef": {"value": "40"}, "CustomerRef": {"value": "201"}}},
            {"Amount": 120.00, "AccountBasedExpenseLineDetail": {
                "AccountRef": {"value": "41"}}},
        ],
    }
    lines, warns = build_journal_lines("Bill", payload, resolver())
    assert warns == []
    debits, credits = by_side(lines)
    assert credits == {AP: Decimal("320.00")}
    assert debits == {MATERIALS: Decimal("200.00"), OFFICE: Decimal("120.00")}
    materials_line = next(line for line in lines if line.account_id == MATERIALS)
    assert materials_line.job_id == JOB


def test_payment_falls_back_to_undeposited_funds() -> None:
    payload = {"Id": "3001", "TotalAmt": 1500.00, "CustomerRef": {"value": "200"}}
    lines, warns = build_journal_lines("Payment", payload, resolver())
    assert warns == []
    debits, credits = by_side(lines)
    assert debits == {UNDEPOSITED: Decimal("1500.00")}
    assert credits == {AR: Decimal("1500.00")}


def test_bill_payment_by_check() -> None:
    payload = {
        "Id": "4001", "TotalAmt": 320.00, "PayType": "Check",
        "CheckPayment": {"BankAccountRef": {"value": "1"}},
    }
    lines, warns = build_journal_lines("BillPayment", payload, resolver())
    assert warns == []
    debits, credits = by_side(lines)
    assert debits == {AP: Decimal("320.00")}
    assert credits == {BANK: Decimal("320.00")}


def test_purchase_and_credit_purchase_flip() -> None:
    base = {
        "Id": "5001", "TotalAmt": 89.99, "AccountRef": {"value": "1"},
        "Line": [{"Amount": 89.99, "AccountBasedExpenseLineDetail": {
            "AccountRef": {"value": "41"}}}],
    }
    lines, warns = build_journal_lines("Purchase", base, resolver())
    assert warns == []
    debits, credits = by_side(lines)
    assert debits == {OFFICE: Decimal("89.99")}
    assert credits == {BANK: Decimal("89.99")}

    refund = dict(base, Id="5002", Credit=True, AccountRef={"value": "50"})
    lines, warns = build_journal_lines("Purchase", refund, resolver())
    assert warns == []
    debits, credits = by_side(lines)
    assert debits == {CARD: Decimal("89.99")}, "Credit=true flips the entry"
    assert credits == {OFFICE: Decimal("89.99")}


def test_journal_entry_passthrough() -> None:
    payload = {
        "Id": "6001",
        "Line": [
            {"Amount": 75.25, "JournalEntryLineDetail": {
                "PostingType": "Debit", "AccountRef": {"value": "41"}}},
            {"Amount": 75.25, "JournalEntryLineDetail": {
                "PostingType": "Credit", "AccountRef": {"value": "1"}}},
        ],
    }
    lines, warns = build_journal_lines("JournalEntry", payload, resolver())
    assert warns == []
    debits, credits = by_side(lines)
    assert debits == {OFFICE: Decimal("75.25")}
    assert credits == {BANK: Decimal("75.25")}
    assert transaction_amount("JournalEntry", payload) == Decimal("75.25")


def test_deposit_moves_undeposited_to_bank() -> None:
    payload = {
        "Id": "7001", "TotalAmt": 1500.00, "DepositToAccountRef": {"value": "1"},
        "Line": [{"Amount": 1500.00, "DepositLineDetail": {
            "AccountRef": {"value": "2"}}}],
    }
    lines, warns = build_journal_lines("Deposit", payload, resolver())
    assert warns == []
    debits, credits = by_side(lines)
    assert debits == {BANK: Decimal("1500.00")}
    assert credits == {UNDEPOSITED: Decimal("1500.00")}


def test_credit_memo_reverses_invoice_directions() -> None:
    payload = {
        "Id": "8001", "TotalAmt": 100.00, "CustomerRef": {"value": "200"},
        "Line": [{"Amount": 100.00, "SalesItemLineDetail": {
            "ItemRef": {"value": "100"}}}],
    }
    lines, warns = build_journal_lines("CreditMemo", payload, resolver())
    assert warns == []
    debits, credits = by_side(lines)
    assert debits == {INCOME: Decimal("100.00")}
    assert credits == {AR: Decimal("100.00")}


def test_vendor_credit_reverses_bill_directions() -> None:
    payload = {
        "Id": "9001", "TotalAmt": 50.00, "VendorRef": {"value": "300"},
        "Line": [{"Amount": 50.00, "AccountBasedExpenseLineDetail": {
            "AccountRef": {"value": "41"}}}],
    }
    lines, warns = build_journal_lines("VendorCredit", payload, resolver())
    assert warns == []
    debits, credits = by_side(lines)
    assert debits == {AP: Decimal("50.00")}
    assert credits == {OFFICE: Decimal("50.00")}


def test_negative_amount_flips_posting_side() -> None:
    payload = {
        "Id": "6002",
        "Line": [
            {"Amount": 50.00, "JournalEntryLineDetail": {
                "PostingType": "Debit", "AccountRef": {"value": "41"}}},
            {"Amount": -50.00, "JournalEntryLineDetail": {
                "PostingType": "Debit", "AccountRef": {"value": "1"}}},
        ],
    }
    lines, warns = build_journal_lines("JournalEntry", payload, resolver())
    assert warns == []
    flipped = next(line for line in lines if line.account_id == BANK)
    assert flipped.posting_type == "credit"
    assert flipped.amount == Decimal("50.00")


def test_unknown_txn_type_warns() -> None:
    lines, warns = build_journal_lines("Estimate", {"Id": "1"}, resolver())
    assert lines == []
    assert any("no journal-line mapping" in w for w in warns)


TAXED_INVOICE = {
    "Id": "1003", "TotalAmt": 108.00, "CustomerRef": {"value": "201"},
    "TxnTaxDetail": {"TotalTax": 8.00},
    "Line": [{"Amount": 100.00, "SalesItemLineDetail": {
        "ItemRef": {"value": "100"}}}],
}


def test_taxed_invoice_balances_into_tax_payable() -> None:
    lines, warns = build_journal_lines("Invoice", TAXED_INVOICE, resolver())
    assert warns == []
    debits, credits = by_side(lines)
    assert debits == {AR: Decimal("108.00")}
    assert credits == {INCOME: Decimal("100.00"), TAXPAY: Decimal("8.00")}
    tax_line = next(line for line in lines if line.account_id == TAXPAY)
    assert tax_line.line_no == 2, "tax line appended last — stable line_no"
    assert tax_line.job_id == JOB, "tax inherits the header job tag"


def test_taxed_credit_memo_reverses_directions() -> None:
    payload = dict(TAXED_INVOICE, Id="8002")
    lines, warns = build_journal_lines("CreditMemo", payload, resolver())
    assert warns == []
    debits, credits = by_side(lines)
    assert credits == {AR: Decimal("108.00")}
    assert debits == {INCOME: Decimal("100.00"), TAXPAY: Decimal("8.00")}


def test_missing_tax_account_warns_never_guesses() -> None:
    lines, warns = build_journal_lines(
        "Invoice", TAXED_INVOICE, resolver(with_tax_account=False)
    )
    assert any("no GlobalTaxPayable account" in w for w in warns)
    assert any("unbalanced" in w for w in warns)
    debits, credits = by_side(lines)
    assert TAXPAY not in credits and TAXPAY not in debits


def test_two_tax_candidates_warn_listing_both_never_guess() -> None:
    """The Arizona/Board case: resolution refuses to pick, and the warning
    is self-explanatory — it names every candidate."""
    lines, warns = build_journal_lines(
        "Invoice", TAXED_INVOICE, resolver(extra_tax_account=True)
    )
    (tax_warn,) = [w for w in warns if "GlobalTaxPayable" in w]
    assert "2 GlobalTaxPayable candidates" in tax_warn
    assert "Arizona Dept of Revenue [qbo 70]" in tax_warn
    assert "Board of Equalization [qbo 71]" in tax_warn
    assert "set_tax_account" in tax_warn
    debits, credits = by_side(lines)
    assert TAXPAY not in credits and TAXPAY2 not in credits


def test_curated_tax_account_beats_ambiguity() -> None:
    """Resolution order: the curated fk wins even with two candidates."""
    r = resolver(extra_tax_account=True, curated_tax=TAXPAY2)
    lines, warns = build_journal_lines("Invoice", TAXED_INVOICE, r)
    assert warns == []
    debits, credits = by_side(lines)
    assert credits[TAXPAY2] == Decimal("8.00"), "curated account used"
    assert TAXPAY not in credits


def test_journal_entry_line_entity_lands_job() -> None:
    payload = {
        "Id": "6003",
        "Line": [
            {"Amount": 250.00, "JournalEntryLineDetail": {
                "PostingType": "Debit", "AccountRef": {"value": "40"},
                "Entity": {"Type": "Customer", "EntityRef": {"value": "201"}}}},
            {"Amount": 250.00, "JournalEntryLineDetail": {
                "PostingType": "Credit", "AccountRef": {"value": "1"}}},
        ],
    }
    lines, warns = build_journal_lines("JournalEntry", payload, resolver())
    assert warns == []
    tagged = next(line for line in lines if line.account_id == MATERIALS)
    untagged = next(line for line in lines if line.account_id == BANK)
    assert tagged.job_id == JOB and untagged.job_id is None


def test_deposit_line_entity_lands_job() -> None:
    payload = {
        "Id": "7002", "TotalAmt": 600.00,
        "DepositToAccountRef": {"value": "1"},
        "Line": [{"Amount": 600.00, "DepositLineDetail": {
            "AccountRef": {"value": "30"}, "Entity": {"value": "201"}}}],
    }
    lines, warns = build_journal_lines("Deposit", payload, resolver())
    assert warns == []
    income_line = next(line for line in lines if line.account_id == INCOME)
    assert income_line.job_id == JOB


def test_header_entity_resolution() -> None:
    r = resolver()
    assert header_entity_id(
        "Invoice", {"CustomerRef": {"value": "200"}}, r) == CUSTOMER
    assert header_entity_id("Bill", {"VendorRef": {"value": "300"}}, r) == VENDOR
    assert header_entity_id(
        "Purchase", {"EntityRef": {"value": "300", "type": "Vendor"}}, r) == VENDOR
    assert header_entity_id(  # legacy/defensive: uppercase casing tolerated
        "Purchase", {"EntityRef": {"value": "300", "Type": "Vendor"}}, r) == VENDOR
    assert header_entity_id(
        "Purchase", {"EntityRef": {"value": "777", "type": "Employee"}}, r) is None
    assert header_entity_id("JournalEntry", {}, r) is None
