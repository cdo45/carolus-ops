"""Seed exactly 15 known violations into the QBO SANDBOX for the Phase 2 gate.

Usage:
    uv run python -m tests.seed_errors --realm <sandbox_realm_id>

Creates real sandbox transactions via the QBO API covering 15 distinct
rule codes, prints a manifest (qbo_id -> expected rule_code), and writes
it to data/seed_manifest.json (data/ is gitignored — sandbox ids never
enter git). Every seeded transaction's PrivateNote is tagged
'CAROLUS-SEED-<n>' so humans can find them in the QBO UI.

Idempotent-ish: re-runs read the existing manifest, verify each seeded
transaction still exists in the sandbox, and create only what's missing.
Supporting objects (vendors, accounts, items, customers) are found by
name before being created.

REFUSES to run unless QBO_ENVIRONMENT=sandbox. Timing note: R020's spike
window is the current calendar month — run the gate in the same month as
the seeder.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from sync.qbo_client import QboClient  # noqa: E402

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "data" / "seed_manifest.json"
TAG = "CAROLUS-SEED"

TODAY = date.today()


def last_saturday(today: date) -> date:
    return today - timedelta(days=(today.isoweekday() - 6) % 7)


def months_ago_mid(today: date, months: int) -> date:
    total = today.year * 12 + (today.month - 1) - months
    return date(total // 12, total % 12 + 1, 15)


@dataclass
class SeedItem:
    n: int
    rule_code: str
    entity_type: str  # QBO entity of the manifest target's transaction
    target: str  # transaction | account | entity | job
    target_kind: str | None  # for entity targets: vendor/customer
    qbo_id: str  # filled at creation
    description: str


def _q(name: str) -> str:
    return name.replace("'", r"\'")


class Seeder:
    def __init__(self, qbo: QboClient) -> None:
        self.qbo = qbo

    # ---------- ensure-helpers: find by name, create if missing ----------

    def ensure(self, entity: str, where: str, payload: dict[str, Any]) -> str:
        rows = self.qbo.query(entity, where)
        if rows:
            return str(rows[0]["Id"])
        created = self.qbo.create(entity, payload)
        return str(created[entity]["Id"])

    def vendor(self, name: str) -> str:
        return self.ensure(
            "Vendor", f"DisplayName = '{_q(name)}'", {"DisplayName": name}
        )

    def customer(self, name: str, parent_id: str | None = None) -> str:
        payload: dict[str, Any] = {"DisplayName": name}
        if parent_id:
            payload |= {"Job": True, "ParentRef": {"value": parent_id}}
        return self.ensure("Customer", f"DisplayName = '{_q(name)}'", payload)

    def account(self, name: str, acct_type: str) -> str:
        return self.ensure(
            "Account", f"Name = '{_q(name)}'",
            {"Name": name, "AccountType": acct_type},
        )

    def first_bank_account(self) -> str:
        rows = self.qbo.query("Account", "AccountType = 'Bank'")
        if rows:
            return str(rows[0]["Id"])
        return self.account("CAROLUS Seed Bank", "Bank")

    def item(self, name: str, income_account_id: str) -> str:
        return self.ensure(
            "Item", f"Name = '{_q(name)}'",
            {"Name": name, "Type": "Service",
             "IncomeAccountRef": {"value": income_account_id}},
        )

    # ---------- transaction builders ----------

    def purchase(
        self, *, amount: float, txn_date: date, bank: str, expense: str,
        note: str, vendor: str | None = None, job_customer: str | None = None,
    ) -> str:
        detail: dict[str, Any] = {"AccountRef": {"value": expense}}
        if job_customer:
            detail["CustomerRef"] = {"value": job_customer}
        payload: dict[str, Any] = {
            "PaymentType": "Cash",
            "AccountRef": {"value": bank},
            "TxnDate": txn_date.isoformat(),
            "PrivateNote": note,
            "Line": [{
                "Amount": amount,
                "DetailType": "AccountBasedExpenseLineDetail",
                "AccountBasedExpenseLineDetail": detail,
            }],
        }
        if vendor:
            payload["EntityRef"] = {"value": vendor, "Type": "Vendor"}
        return str(self.qbo.create("Purchase", payload)["Purchase"]["Id"])

    def bill(
        self, *, amount: float, txn_date: date, vendor: str, expense: str,
        note: str, doc_number: str | None = None, job_customer: str | None = None,
    ) -> str:
        detail: dict[str, Any] = {"AccountRef": {"value": expense}}
        if job_customer:
            detail["CustomerRef"] = {"value": job_customer}
        payload: dict[str, Any] = {
            "VendorRef": {"value": vendor},
            "TxnDate": txn_date.isoformat(),
            "PrivateNote": note,
            "Line": [{
                "Amount": amount,
                "DetailType": "AccountBasedExpenseLineDetail",
                "AccountBasedExpenseLineDetail": detail,
            }],
        }
        if doc_number:
            payload["DocNumber"] = doc_number
        return str(self.qbo.create("Bill", payload)["Bill"]["Id"])

    def journal_entry(
        self, *, amount: float, txn_date: date, debit: str, credit: str, note: str,
    ) -> str:
        payload = {
            "TxnDate": txn_date.isoformat(),
            "PrivateNote": note,
            "Line": [
                {"Amount": amount, "DetailType": "JournalEntryLineDetail",
                 "JournalEntryLineDetail": {"PostingType": "Debit",
                                            "AccountRef": {"value": debit}}},
                {"Amount": amount, "DetailType": "JournalEntryLineDetail",
                 "JournalEntryLineDetail": {"PostingType": "Credit",
                                            "AccountRef": {"value": credit}}},
            ],
        }
        return str(self.qbo.create("JournalEntry", payload)["JournalEntry"]["Id"])

    def invoice(
        self, *, amount: float, txn_date: date, customer: str, item: str, note: str,
    ) -> str:
        payload = {
            "CustomerRef": {"value": customer},
            "TxnDate": txn_date.isoformat(),
            "PrivateNote": note,
            "Line": [{
                "Amount": amount,
                "DetailType": "SalesItemLineDetail",
                "SalesItemLineDetail": {"ItemRef": {"value": item}},
            }],
        }
        return str(self.qbo.create("Invoice", payload)["Invoice"]["Id"])

    def unapplied_payment(
        self, *, amount: float, txn_date: date, customer: str, note: str,
    ) -> str:
        payload = {
            "CustomerRef": {"value": customer},
            "TotalAmt": amount,
            "TxnDate": txn_date.isoformat(),
            "PrivateNote": note,
        }
        return str(self.qbo.create("Payment", payload)["Payment"]["Id"])


def seed_all(seeder: Seeder) -> list[SeedItem]:
    """Create supporting objects + the 15 manifest violations."""
    bank = seeder.first_bank_account()
    suspense = seeder.account("CAROLUS Seed Suspense", "Other Current Asset")
    refund_exp = seeder.account("CAROLUS Seed Refund Expense", "Expense")
    cogs = seeder.account("CAROLUS Seed COGS", "Cost of Goods Sold")
    office = seeder.account("CAROLUS Seed Office", "Expense")
    income = seeder.account("CAROLUS Seed Income", "Income")
    uncategorized = seeder.account("Uncategorized Expense", "Expense")
    service = seeder.item("CAROLUS Seed Service", income)

    vendor_a = seeder.vendor("CAROLUS SEED VENDOR A")  # duplicate pair
    vendor_b = seeder.vendor("CAROLUS SEED VENDOR B")  # big opener
    vendor_c = seeder.vendor("CAROLUS SEED VENDOR C")  # spike
    vendor_d = seeder.vendor("CAROLUS SEED VENDOR D")  # duplicate doc number
    whale = seeder.customer("CAROLUS SEED WHALE CORP")
    cust = seeder.customer("CAROLUS SEED CUSTOMER")
    job = seeder.customer("CAROLUS SEED JOB", parent_id=cust)

    items: list[SeedItem] = []

    def add(n: int, rule: str, entity_type: str, qbo_id: str, desc: str,
            target: str = "transaction", target_kind: str | None = None,
            target_qbo_id: str | None = None) -> None:
        items.append(SeedItem(
            n=n, rule_code=rule, entity_type=entity_type, target=target,
            target_kind=target_kind, qbo_id=target_qbo_id or qbo_id,
            description=desc,
        ))

    # 1. R010 — duplicate purchase pair; the LATER twin is the manifest item
    seeder.purchase(amount=750.00, txn_date=TODAY - timedelta(days=5),
                    bank=bank, expense=office, vendor=vendor_a,
                    note=f"{TAG}-SUPPORT-1 earlier twin")
    p2 = seeder.purchase(amount=750.00, txn_date=TODAY - timedelta(days=2),
                         bank=bank, expense=office, vendor=vendor_a,
                         note=f"{TAG}-1 duplicate payment later twin")
    add(1, "R010", "Purchase", p2, "same vendor+amount 3 days apart")

    # 2. R011 — two bills, same vendor, same DocNumber
    seeder.bill(amount=410.00, txn_date=TODAY - timedelta(days=20),
                vendor=vendor_d, expense=office, doc_number="CAROLUS-DUP-1",
                note=f"{TAG}-SUPPORT-2 first entry")
    b2 = seeder.bill(amount=410.00, txn_date=TODAY - timedelta(days=4),
                     vendor=vendor_d, expense=office, doc_number="CAROLUS-DUP-1",
                     note=f"{TAG}-2 duplicate doc number")
    add(2, "R011", "Bill", b2, "same vendor + DocNumber twice")

    # 3. R012 — round-number JE
    je = seeder.journal_entry(amount=5000.00, txn_date=TODAY - timedelta(days=3),
                              debit=office, credit=bank,
                              note=f"{TAG}-3 round number JE")
    add(3, "R012", "JournalEntry", je, "$5,000.00 round JE line")

    # 4. R013 — aged suspense balance (40 days; under R016's 45 on purpose)
    s = seeder.purchase(amount=200.00, txn_date=TODAY - timedelta(days=40),
                        bank=bank, expense=suspense,
                        note=f"{TAG}-4 aged suspense parking")
    add(4, "R013", "Purchase", s, "suspense balance aged 40d",
        target="account", target_qbo_id=suspense)

    # 5. R014 — credit-side expense balance this month
    je_refund = seeder.journal_entry(
        amount=300.00, txn_date=TODAY.replace(day=min(TODAY.day, 5)),
        debit=bank, credit=refund_exp, note=f"{TAG}-5 refund into expense")
    add(5, "R014", "JournalEntry", je_refund,
        "expense account net credit this month",
        target="account", target_qbo_id=refund_exp)

    # 6. R015 — stale uncategorized (20 days)
    u = seeder.purchase(amount=150.00, txn_date=TODAY - timedelta(days=20),
                        bank=bank, expense=uncategorized,
                        note=f"{TAG}-6 stale uncategorized")
    add(6, "R015", "Purchase", u, "uncategorized for 20 days")

    # 7. R016 — backdated 60 days (created today)
    bd = seeder.purchase(amount=123.45, txn_date=TODAY - timedelta(days=60),
                         bank=bank, expense=office, vendor=vendor_a,
                         note=f"{TAG}-7 backdated entry")
    add(7, "R016", "Purchase", bd, "txn_date 60d before CreateTime")

    # 8. R017 — weekend JE (non-round amount to stay out of R012)
    wj = seeder.journal_entry(amount=77.10, txn_date=last_saturday(TODAY),
                              debit=office, credit=bank,
                              note=f"{TAG}-8 weekend JE")
    add(8, "R017", "JournalEntry", wj, "JE dated Saturday")

    # 9. R020 — vendor spend spike: $100 history, $4,000 this month
    seeder.purchase(amount=100.00, txn_date=months_ago_mid(TODAY, 3),
                    bank=bank, expense=office, vendor=vendor_c,
                    note=f"{TAG}-SUPPORT-9 trailing history")
    spike = seeder.purchase(amount=4000.00, txn_date=TODAY.replace(day=min(TODAY.day, 6)),
                            bank=bank, expense=office, vendor=vendor_c,
                            note=f"{TAG}-9 spend spike month")
    add(9, "R020", "Purchase", spike, "month spend 40x trailing avg",
        target="entity", target_kind="vendor", target_qbo_id=vendor_c)

    # 10. R021 — first-ever vendor transaction at $6,000
    nb = seeder.bill(amount=6000.00, txn_date=TODAY - timedelta(days=6),
                     vendor=vendor_b, expense=office,
                     note=f"{TAG}-10 large first bill")
    add(10, "R021", "Bill", nb, "new vendor opens at $6,000")

    # 11. R023 — A/R concentration: one $25,000 open invoice
    inv = seeder.invoice(amount=25000.00, txn_date=TODAY - timedelta(days=8),
                         customer=whale, item=service,
                         note=f"{TAG}-11 AR concentration")
    add(11, "R023", "Invoice", inv, "whale customer dominates open AR",
        target="entity", target_kind="customer", target_qbo_id=whale)

    # 12. R024 — $800 purchase with no vendor
    nv = seeder.purchase(amount=800.00, txn_date=TODAY - timedelta(days=2),
                         bank=bank, expense=office,
                         note=f"{TAG}-12 spend without vendor")
    add(12, "R024", "Purchase", nv, "no EntityRef on $800 spend")

    # 13. R030 — COGS without job
    cg = seeder.purchase(amount=400.00, txn_date=TODAY - timedelta(days=2),
                         bank=bank, expense=cogs, vendor=vendor_a,
                         note=f"{TAG}-13 COGS without job")
    add(13, "R030", "Purchase", cg, "untagged COGS $400")

    # 14. R032 — job underwater: billed $500, costs $2,000
    seeder.invoice(amount=500.00, txn_date=TODAY - timedelta(days=9),
                   customer=job, item=service,
                   note=f"{TAG}-SUPPORT-14 small job billing")
    seeder.bill(amount=2000.00, txn_date=TODAY - timedelta(days=7),
                vendor=vendor_a, expense=cogs, job_customer=job,
                note=f"{TAG}-14 job cost overrun")
    add(14, "R032", "Customer", job, "job margin -1500",
        target="job", target_qbo_id=job)

    # 15. R033 — unapplied customer payment, 35 days old
    up = seeder.unapplied_payment(amount=1000.00,
                                  txn_date=TODAY - timedelta(days=35),
                                  customer=cust,
                                  note=f"{TAG}-15 unapplied payment")
    add(15, "R033", "Payment", up, "payment applied to nothing for 35d")

    return items


def verify_existing(qbo: QboClient, manifest: dict[str, Any]) -> bool:
    """True if every manifest transaction still exists in the sandbox."""
    for item in manifest.get("items", []):
        if item["target"] not in ("transaction",):
            continue
        rows = qbo.query(item["entity_type"], f"Id = '{item['qbo_id']}'")
        if not rows:
            return False
    return bool(manifest.get("items"))


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Seed Phase 2 gate violations")
    parser.add_argument("--realm", required=True, help="sandbox realm id")
    args = parser.parse_args(argv)

    if os.environ.get("QBO_ENVIRONMENT") != "sandbox":
        print("REFUSING: QBO_ENVIRONMENT must be 'sandbox' to seed", file=sys.stderr)
        return 2
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT id FROM clients WHERE qbo_realm_id = %s", (args.realm,)
        ).fetchone()
        if row is None:
            print(f"no client for realm {args.realm} — run sync.connect first",
                  file=sys.stderr)
            return 2
        client_id: UUID = row[0]
        qbo = QboClient(conn, client_id, args.realm)

        if MANIFEST_PATH.exists():
            manifest = json.loads(MANIFEST_PATH.read_text())
            if manifest.get("realm") == args.realm and verify_existing(qbo, manifest):
                print(f"already seeded — manifest intact at {MANIFEST_PATH}")
                _print_manifest(manifest)
                return 0
            print("manifest stale or incomplete — reseeding")

        items = seed_all(Seeder(qbo))

    manifest = {
        "realm": args.realm,
        "seeded_on": TODAY.isoformat(),
        "items": [asdict(item) for item in items],
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"seeded {len(items)} violations; manifest -> {MANIFEST_PATH}")
    _print_manifest(manifest)
    print("\nNOTE: R020's window is the current calendar month — run the"
          " gate this month.")
    return 0


def _print_manifest(manifest: dict[str, Any]) -> None:
    print(f"{'n':>3} {'rule':<6} {'entity':<13} {'qbo_id':<10} description")
    for item in manifest["items"]:
        print(f"{item['n']:>3} {item['rule_code']:<6} {item['entity_type']:<13}"
              f" {item['qbo_id']:<10} {item['description']}")


if __name__ == "__main__":
    raise SystemExit(main())
