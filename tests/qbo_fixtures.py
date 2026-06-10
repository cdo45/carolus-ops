"""A small fake QBO company used by transform/full-sync tests.

Shaped like real QBO v3 payloads (only the fields transforms read).
Invoice 1002 references an unstaged item on purpose — it must come out
unbalanced and flagged, never silently wrong.
"""

from __future__ import annotations

from typing import Any

_META = {
    "MetaData": {
        "CreateTime": "2026-05-20T09:00:00-07:00",
        "LastUpdatedTime": "2026-05-20T10:00:00-07:00",
    }
}

COMPANY: dict[str, list[dict[str, Any]]] = {
    "Account": [
        {"Id": "1", "Name": "Checking", "AccountType": "Bank",
         "AccountSubType": "Checking", "Active": True},
        {"Id": "2", "Name": "Undeposited Funds", "AccountType": "Other Current Asset",
         "AccountSubType": "UndepositedFunds", "Active": True},
        {"Id": "20", "Name": "Accounts Receivable",
         "AccountType": "Accounts Receivable", "Active": True},
        {"Id": "21", "Name": "Accounts Payable",
         "AccountType": "Accounts Payable", "Active": True},
        {"Id": "30", "Name": "Construction Income", "AccountType": "Income",
         "Active": True},
        {"Id": "40", "Name": "Job Materials", "AccountType": "Cost of Goods Sold",
         "Active": True},
        {"Id": "41", "Name": "Office Expenses", "AccountType": "Expense",
         "Active": True},
        {"Id": "50", "Name": "Company Card", "AccountType": "Credit Card",
         "AccountSubType": "CreditCard", "Active": True},
    ],
    "Item": [
        {"Id": "100", "Name": "Labor", "IncomeAccountRef": {"value": "30"}},
        {"Id": "101", "Name": "Materials", "IncomeAccountRef": {"value": "30"},
         "ExpenseAccountRef": {"value": "40"}},
    ],
    "Customer": [
        {"Id": "200", "DisplayName": "Acme Builders", "Active": True},
        {"Id": "201", "DisplayName": "Kitchen Remodel", "Active": True,
         "Job": True, "ParentRef": {"value": "200"},
         "FullyQualifiedName": "Acme Builders:Kitchen Remodel"},
    ],
    "Vendor": [
        {"Id": "300", "DisplayName": "Home Depot", "Active": True},
    ],
    "Invoice": [
        {"Id": "1001", "TxnDate": "2026-05-01", "TotalAmt": 1500.00,
         "CustomerRef": {"value": "201"},
         "Line": [
             {"Amount": 1000.00, "Description": "labor",
              "SalesItemLineDetail": {"ItemRef": {"value": "100"}}},
             {"Amount": 500.00, "Description": "materials",
              "SalesItemLineDetail": {"ItemRef": {"value": "101"}}},
         ], **_META},
        # references item 999 which is not staged -> warning + unbalanced
        {"Id": "1002", "TxnDate": "2026-05-02", "TotalAmt": 250.00,
         "CustomerRef": {"value": "200"},
         "Line": [
             {"Amount": 250.00,
              "SalesItemLineDetail": {"ItemRef": {"value": "999"}}},
         ], **_META},
    ],
    "Bill": [
        {"Id": "2001", "TxnDate": "2026-05-03", "TotalAmt": 320.00,
         "DocNumber": "INV-778",
         "VendorRef": {"value": "300"},
         "Line": [
             {"Amount": 200.00, "Description": "lumber",
              "AccountBasedExpenseLineDetail": {
                  "AccountRef": {"value": "40"},
                  "CustomerRef": {"value": "201"}}},
             {"Amount": 120.00,
              "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "41"}}},
         ], **_META},
    ],
    "Payment": [
        {"Id": "3001", "TxnDate": "2026-05-10", "TotalAmt": 1500.00,
         "CustomerRef": {"value": "200"}, **_META},
    ],
    "BillPayment": [
        {"Id": "4001", "TxnDate": "2026-05-11", "TotalAmt": 320.00,
         "VendorRef": {"value": "300"}, "PayType": "Check",
         "CheckPayment": {"BankAccountRef": {"value": "1"}},
         "Line": [{"Amount": 320.00,
                   "LinkedTxn": [{"TxnId": "2001", "TxnType": "Bill"}]}],
         **_META},
    ],
    "Purchase": [
        {"Id": "5001", "TxnDate": "2026-05-12", "TotalAmt": 89.99,
         "AccountRef": {"value": "1"}, "PaymentType": "Check",
         "EntityRef": {"value": "300", "type": "Vendor"},
         "Line": [
             {"Amount": 89.99,
              "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "41"}}},
         ], **_META},
        # Credit=true: a refund — postings flip
        {"Id": "5002", "TxnDate": "2026-05-13", "TotalAmt": 25.00, "Credit": True,
         "AccountRef": {"value": "50"},
         "Line": [
             {"Amount": 25.00,
              "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "41"}}},
         ], **_META},
    ],
    "JournalEntry": [
        {"Id": "6001", "TxnDate": "2026-05-14",
         "Line": [
             {"Amount": 75.25, "Description": "accrual",
              "JournalEntryLineDetail": {"PostingType": "Debit",
                                         "AccountRef": {"value": "41"}}},
             {"Amount": 75.25,
              "JournalEntryLineDetail": {"PostingType": "Credit",
                                         "AccountRef": {"value": "1"}}},
         ], **_META},
    ],
    "Deposit": [
        {"Id": "7001", "TxnDate": "2026-05-15", "TotalAmt": 1500.00,
         "DepositToAccountRef": {"value": "1"},
         "Line": [
             {"Amount": 1500.00,
              "DepositLineDetail": {"AccountRef": {"value": "2"}}},
         ], **_META},
    ],
    "CreditMemo": [
        {"Id": "8001", "TxnDate": "2026-05-16", "TotalAmt": 100.00,
         "CustomerRef": {"value": "200"},
         "Line": [
             {"Amount": 100.00,
              "SalesItemLineDetail": {"ItemRef": {"value": "100"}}},
         ], **_META},
    ],
    "VendorCredit": [
        {"Id": "9001", "TxnDate": "2026-05-17", "TotalAmt": 50.00,
         "VendorRef": {"value": "300"},
         "Line": [
             {"Amount": 50.00,
              "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "41"}}},
         ], **_META},
    ],
}


class FakeQbo:
    """Stands in for QboClient in tests: serves the fixture company."""

    def query(self, entity: str, where: str | None = None) -> list[dict[str, Any]]:
        return [dict(payload) for payload in COMPANY.get(entity, [])]
