"""Seed exactly 21 known violations into the QBO SANDBOX for the Phase 2 gate.

Usage:
    uv run python -m tests.seed_errors --realm <sandbox_realm_id>

Creates real sandbox transactions via the QBO API covering 21 distinct
rule codes (rules v2 thresholds), prints a manifest, and writes
it to data/seed_manifest.json (data/ is gitignored — sandbox ids never
enter git). Every seeded transaction's PrivateNote is tagged
'CAROLUS-SEED-<n>' so humans can find them in the QBO UI.

Idempotent-ish: re-runs read the existing manifest, verify each seeded
transaction still exists in the sandbox, and create only what's missing.

GENERATION ISOLATION (strict, per seed): every seeding generation mints
a nonce, and EVERY seed that creates or depends on entity history gets
its OWN entity — 'CAROLUS SEED <nonce> <KIND> <RULE>'. No vendor or
customer is shared across rule seeds (R010's twin pair shares its one
vendor by design: the pair IS the signal). History-sensitive amounts are
salted by the nonce. Rules with trailing-window or first-ever semantics
therefore see clean history every reseed.

GENERATION CLEANUP: before minting a new generation, prior CAROLUS-SEED
invoices/bills still carrying OPEN balances are neutralized through the
API — invoices voided (QBO allows it), bills offset with a zero-out
VendorCredit tagged CAROLUS-SEED-CLEANUP (bills are not voidable; the
credit zeroes the vendor's A/P, though the bill's own Balance remains —
old per-bill flags are already open and inert). Closed/neutral artifacts
are skipped. This keeps denominator rules (R023 A/R share, future ratio
rules) testable across any number of generations. A summary prints
n voided / n credited / n skipped with reasons.

REFUSES to run unless QBO_ENVIRONMENT=sandbox. Timing note: R020's spike
window is the current calendar month — run the gate in the same month as
the seeder.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from sync.qbo_client import QboClient, QboRequestError  # noqa: E402

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "data" / "seed_manifest.json"
TAG = "CAROLUS-SEED"
EXPECTED_SEEDS = 21  # manifest size under rules v2 — fewer means reseed

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
    target: str  # transaction | account | entity | job | client
    target_kind: str | None  # for entity targets: vendor/customer
    qbo_id: str  # filled at creation
    description: str
    alt_qbo_id: str | None = None  # pair rules (R027): hit on either


def _q(name: str) -> str:
    return name.replace("'", r"\'")


class SeedFailure(Exception):
    """A seed step was rejected by QBO; the message names the step, the
    entity, the QBO fault detail, and the payload that was sent."""


def format_qbo_fault(exc: QboRequestError) -> str:
    """Pull code/Message/Detail out of a QBO Fault body, if parseable."""
    try:
        fault = json.loads(exc.body)["Fault"]["Error"][0]
        return (
            f"code {fault.get('code')}: {fault.get('Message')}"
            f" — {fault.get('Detail')}"
        )
    except Exception:
        return f"HTTP {exc.status_code}: {exc.body[:300]}"


class Seeder:
    def __init__(self, qbo: QboClient) -> None:
        self.qbo = qbo
        self.step_label: str = "setup"

    def step(self, label: str) -> None:
        """Name the seed item being built, for failure reports."""
        self.step_label = label

    def _create(self, entity: str, payload: dict[str, Any],
                *, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            return self.qbo.create(entity, payload, params=params)
        except QboRequestError as exc:
            raise SeedFailure(
                f"[{self.step_label}] creating {entity} failed —"
                f" {format_qbo_fault(exc)}\n"
                f"  payload sent: {json.dumps(payload, default=str)}"
            ) from exc

    def _query(
        self, entity: str, where: str | None = None
    ) -> list[dict[str, Any]]:
        try:
            return self.qbo.query(entity, where)
        except QboRequestError as exc:
            raise SeedFailure(
                f"[{self.step_label}] querying {entity}"
                f" ({where or 'all'}) failed — {format_qbo_fault(exc)}"
            ) from exc

    # ---------- ensure-helpers: find by name, create if missing ----------

    def ensure(self, entity: str, where: str, payload: dict[str, Any]) -> str:
        rows = self._query(entity, where)
        if rows:
            return str(rows[0]["Id"])
        created = self._create(entity, payload)
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
        rows = self._query("Account", "AccountType = 'Bank'")
        if rows:
            return str(rows[0]["Id"])
        return self.account("CAROLUS Seed Bank", "Bank")

    def item(self, name: str, income_account_id: str) -> str:
        return self.ensure(
            "Item", f"Name = '{_q(name)}'",
            {"Name": name, "Type": "Service",
             "IncomeAccountRef": {"value": income_account_id}},
        )

    def open_ar_total(self) -> float:
        """Sum the sandbox's current open A/R from QBO's own Balance field.

        query() pages internally, so this sees every invoice; Balance is not
        reliably filterable in a QBO WHERE, so filter Balance > 0 in code.
        """
        total = 0.0
        for inv in self._query("Invoice"):
            balance = float(inv.get("Balance") or 0)
            if balance > 0:
                total += balance
        return total

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
            # v3 EntityRef sub-property is lowercase 'type' — uppercase 'Type'
            # is an unsupported property and fails QBO's parse (code 2010)
            payload["EntityRef"] = {"value": vendor, "type": "Vendor"}
        return str(self._create("Purchase", payload)["Purchase"]["Id"])

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
        return str(self._create("Bill", payload)["Bill"]["Id"])

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
        return str(self._create("JournalEntry", payload)["JournalEntry"]["Id"])

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
        return str(self._create("Invoice", payload)["Invoice"]["Id"])

    def unapplied_payment(
        self, *, amount: float, txn_date: date, customer: str, note: str,
    ) -> str:
        payload = {
            "CustomerRef": {"value": customer},
            "TotalAmt": amount,
            "TxnDate": txn_date.isoformat(),
            "PrivateNote": note,
        }
        return str(self._create("Payment", payload)["Payment"]["Id"])


def seed_all(seeder: Seeder, nonce: str) -> list[SeedItem]:
    """Create this generation's objects + the 21 manifest violations.

    nonce: per-generation token — history-sensitive entities get
    generation-unique names; history-sensitive amounts get a cent salt
    derived from the nonce.
    """
    mint = f"CAROLUS SEED {nonce.upper()}"
    salt = (int(nonce, 16) % 89 + 1) / 100.0  # 0.01-0.89 per generation

    seeder.step("support objects: accounts, item, vendors, customers")
    bank = seeder.first_bank_account()
    suspense = seeder.account("CAROLUS Seed Suspense", "Other Current Asset")
    refund_exp = seeder.account("CAROLUS Seed Refund Expense", "Expense")
    cogs = seeder.account("CAROLUS Seed COGS", "Cost of Goods Sold")
    office = seeder.account("CAROLUS Seed Office", "Expense")
    income = seeder.account("CAROLUS Seed Income", "Income")
    uncategorized = seeder.account("Uncategorized Expense", "Expense")
    service = seeder.item("CAROLUS Seed Service", income)

    # STRICT entity-per-seed isolation: every seed that creates or depends
    # on entity history gets its OWN entity this generation. Only R010's
    # twin pair shares a vendor — by design, the pair IS the signal.
    def vend(role: str) -> str:
        return seeder.vendor(f"{mint} VENDOR {role}")

    def customer_for(role: str, parent: str | None = None) -> str:
        suffix = "JOB" if parent else "CUSTOMER"
        return seeder.customer(f"{mint} {suffix} {role}", parent_id=parent)

    vendor_r010 = vend("R010")  # shared by the duplicate PAIR only
    vendor_r011 = vend("R011")
    vendor_r016 = vend("R016")
    vendor_r020 = vend("R020")
    vendor_r021 = vend("R021")  # first-ever txn must stay first
    vendor_r026 = vend("R026")  # backdated bill must not predate anyone
    vendor_r028 = vend("R028")
    vendor_r030 = vend("R030")
    vendor_r032 = vend("R032")
    vendor_r034 = vend("R034")
    vendor_r035 = vend("R035")
    whale = customer_for("R023")
    customer_r025 = customer_for("R025")
    customer_r033 = customer_for("R033")
    parent_r032 = customer_for("R032")
    job = customer_for("R032", parent=parent_r032)

    items: list[SeedItem] = []

    def add(n: int, rule: str, entity_type: str, qbo_id: str, desc: str,
            target: str = "transaction", target_kind: str | None = None,
            target_qbo_id: str | None = None,
            alt_qbo_id: str | None = None) -> None:
        items.append(SeedItem(
            n=n, rule_code=rule, entity_type=entity_type, target=target,
            target_kind=target_kind, qbo_id=target_qbo_id or qbo_id,
            description=desc, alt_qbo_id=alt_qbo_id,
        ))

    seeder.step("SEED-1 R010 duplicate purchase pair")
    # 1. R010 — duplicate purchase pair; the LATER twin is the manifest item
    seeder.purchase(amount=750.00 + salt, txn_date=TODAY - timedelta(days=5),
                    bank=bank, expense=office, vendor=vendor_r010,
                    note=f"{TAG}-SUPPORT-1 earlier twin")
    p2 = seeder.purchase(amount=750.00 + salt, txn_date=TODAY - timedelta(days=2),
                         bank=bank, expense=office, vendor=vendor_r010,
                         note=f"{TAG}-1 duplicate payment later twin")
    add(1, "R010", "Purchase", p2, "same vendor+amount 3 days apart")

    seeder.step("SEED-2 R011 duplicate doc number bills")
    # 2. R011 — two bills, same vendor, same DocNumber
    seeder.bill(amount=410.00 + salt, txn_date=TODAY - timedelta(days=20),
                vendor=vendor_r011, expense=office, doc_number=f"CAROLUS-DUP-{nonce.upper()}",
                note=f"{TAG}-SUPPORT-2 first entry")
    b2 = seeder.bill(amount=410.00 + salt, txn_date=TODAY - timedelta(days=4),
                     vendor=vendor_r011, expense=office, doc_number=f"CAROLUS-DUP-{nonce.upper()}",
                     note=f"{TAG}-2 duplicate doc number")
    add(2, "R011", "Bill", b2, "same vendor + DocNumber twice")

    seeder.step("SEED-3 R012 round-number JE")
    # 3. R012 — round-number JE
    je = seeder.journal_entry(amount=5000.00, txn_date=TODAY - timedelta(days=3),
                              debit=office, credit=bank,
                              note=f"{TAG}-3 round number JE")
    add(3, "R012", "JournalEntry", je, "$5,000.00 round JE line")

    seeder.step("SEED-4 R013 aged suspense balance")
    # 4. R013 — aged suspense balance (40 days; under R016's 45 on purpose)
    s = seeder.purchase(amount=200.00, txn_date=TODAY - timedelta(days=40),
                        bank=bank, expense=suspense,
                        note=f"{TAG}-4 aged suspense parking")
    add(4, "R013", "Purchase", s, "suspense balance aged 40d",
        target="account", target_qbo_id=suspense)

    seeder.step("SEED-5 R014 credit-side expense balance")
    # 5. R014 — credit-side expense balance this month
    je_refund = seeder.journal_entry(
        amount=300.00, txn_date=TODAY.replace(day=min(TODAY.day, 5)),
        debit=bank, credit=refund_exp, note=f"{TAG}-5 refund into expense")
    add(5, "R014", "JournalEntry", je_refund,
        "expense account net credit this month",
        target="account", target_qbo_id=refund_exp)

    seeder.step("SEED-6 R015 stale uncategorized")
    # 6. R015 — stale uncategorized (20 days)
    u = seeder.purchase(amount=150.00, txn_date=TODAY - timedelta(days=20),
                        bank=bank, expense=uncategorized,
                        note=f"{TAG}-6 stale uncategorized")
    add(6, "R015", "Purchase", u, "uncategorized for 20 days")

    seeder.step("SEED-7 R016 backdated entry")
    # 7. R016 — backdated 60 days (created today)
    bd = seeder.purchase(amount=123.45, txn_date=TODAY - timedelta(days=60),
                         bank=bank, expense=office, vendor=vendor_r016,
                         note=f"{TAG}-7 backdated entry")
    add(7, "R016", "Purchase", bd, "txn_date 60d before CreateTime")

    seeder.step("SEED-8 R017 weekend JE")
    # 8. R017 — weekend JE (non-round amount to stay out of R012)
    wj = seeder.journal_entry(amount=77.10, txn_date=last_saturday(TODAY),
                              debit=office, credit=bank,
                              note=f"{TAG}-8 weekend JE")
    add(8, "R017", "JournalEntry", wj, "JE dated Saturday")

    seeder.step("SEED-9 R020 vendor spend spike")
    # 9. R020 — v2: 3 months of $100 history, then $6,000 this month
    for months_ago in (1, 2, 3):
        seeder.purchase(amount=100.00 + salt,
                        txn_date=months_ago_mid(TODAY, months_ago),
                        bank=bank, expense=office, vendor=vendor_r020,
                        note=f"{TAG}-SUPPORT-9 trailing history"
                             f" m-{months_ago}")
    spike = seeder.purchase(amount=6000.00, txn_date=TODAY.replace(day=min(TODAY.day, 6)),
                            bank=bank, expense=office, vendor=vendor_r020,
                            note=f"{TAG}-9 spend spike month")
    add(9, "R020", "Purchase", spike, "month spend 40x trailing avg",
        target="entity", target_kind="vendor", target_qbo_id=vendor_r020)

    seeder.step("SEED-10 R021 large first vendor bill")
    # 10. R021 — first-ever vendor transaction at $6,000
    nb = seeder.bill(amount=6000.00 + salt, txn_date=TODAY - timedelta(days=6),
                     vendor=vendor_r021, expense=office,
                     note=f"{TAG}-10 large first bill")
    add(10, "R021", "Bill", nb, "new vendor opens at $6,000")

    seeder.step("SEED-11 R023 AR concentration invoice")
    # 11. R023 — the whale must be BOTH > $25k open AND > 50% of total open
    # A/R. The sandbox accumulates open invoices (its own samples; this gen's
    # later small ones), so a fixed $25k is not reliably the majority. Size
    # the whale against live ambient A/R (cleanup has voided prior seed
    # invoices, and the fresh nonce'd whale has none of its own yet):
    # ambient + 25001 makes the whale alone exceed all other open A/R
    # combined (-> > 50%), clears $25k, and the 25k buffer absorbs the few
    # small invoices seeded after this (e.g. R025 ~$1k).
    whale_amount = seeder.open_ar_total() + 25000.0 + 1.0
    inv = seeder.invoice(amount=whale_amount, txn_date=TODAY - timedelta(days=8),
                         customer=whale, item=service,
                         note=f"{TAG}-11 AR concentration")
    add(11, "R023", "Invoice", inv, "whale customer dominates open AR",
        target="entity", target_kind="customer", target_qbo_id=whale)

    seeder.step("SEED-12 R024 spend without vendor")
    # 12. R024 — $800 purchase with no vendor
    nv = seeder.purchase(amount=800.00, txn_date=TODAY - timedelta(days=2),
                         bank=bank, expense=office,
                         note=f"{TAG}-12 spend without vendor")
    add(12, "R024", "Purchase", nv, "no EntityRef on $800 spend")

    seeder.step("SEED-13 R030 COGS without job")
    # 13. R030 — COGS without job
    cg = seeder.purchase(amount=600.00, txn_date=TODAY - timedelta(days=2),
                         bank=bank, expense=cogs, vendor=vendor_r030,
                         note=f"{TAG}-13 COGS without job")
    add(13, "R030", "Purchase", cg, "untagged COGS $600 (v2 floor 500)")

    seeder.step("SEED-14 R032 job margin negative")
    # 14. R032 — underwater AND mature: billed $500, costs $2,000 dated
    # 50 days back (>=45d first-cost age -> the critical path; Bills are
    # R016-exempt so the backdating stays clean)
    seeder.invoice(amount=500.00, txn_date=TODAY - timedelta(days=9),
                   customer=job, item=service,
                   note=f"{TAG}-SUPPORT-14 small job billing")
    seeder.bill(amount=2000.00, txn_date=TODAY - timedelta(days=50),
                vendor=vendor_r032, expense=cogs, job_customer=job,
                note=f"{TAG}-14 job cost overrun")
    add(14, "R032", "Customer", job, "job margin -1500, critical grade",
        target="job", target_qbo_id=job)

    seeder.step("SEED-15 R033 unapplied payment")
    # 15. R033 — unapplied customer payment, 35 days old, >= $500 (v2)
    up = seeder.unapplied_payment(amount=1000.00,
                                  txn_date=TODAY - timedelta(days=35),
                                  customer=customer_r033,
                                  note=f"{TAG}-15 unapplied payment")
    add(15, "R033", "Payment", up, "payment applied to nothing for 35d")

    seeder.step("SEED-16 R025 stale receivable")
    # 16. R025 — invoice 100 days old, unpaid (QBO Balance = full amount)
    stale_inv = seeder.invoice(amount=1500.00,
                               txn_date=TODAY - timedelta(days=100),
                               customer=customer_r025, item=service,
                               note=f"{TAG}-16 stale receivable")
    add(16, "R025", "Invoice", stale_inv, "open invoice aged 100d >= $1k")

    seeder.step("SEED-17 R026 aged payable")
    # 17. R026 — bill 70 days old, unpaid (Bills are R016-exempt)
    aged_bill = seeder.bill(amount=1200.00,
                            txn_date=TODAY - timedelta(days=70),
                            vendor=vendor_r026, expense=office,
                            note=f"{TAG}-17 aged payable")
    add(17, "R026", "Bill", aged_bill, "open bill aged 70d >= $1k")

    seeder.step("SEED-18 R027 near-duplicate vendor pair")
    # 18. R027 — two ACTIVE vendors with >= 0.7 trigram-similar names
    twin_a = seeder.vendor(f"{mint} HADLEY CONSTRUCTION")
    twin_b = seeder.vendor(f"{mint} HADLEY CONSTRUCTION LLC")
    add(18, "R027", "Vendor", twin_a, "near-duplicate vendor names",
        target="entity", target_kind="vendor", target_qbo_id=twin_a,
        alt_qbo_id=twin_b)

    seeder.step("SEED-19 R028 overdrawn bank account")
    # 19. R028 — fresh bank account that only ever pays out
    overdrawn = seeder.account("CAROLUS Seed Overdrawn Bank", "Bank")
    seeder.purchase(amount=500.00, txn_date=TODAY - timedelta(days=3),
                    bank=overdrawn, expense=office, vendor=vendor_r028,
                    note=f"{TAG}-SUPPORT-19 overdraft spend")
    add(19, "R028", "Account", overdrawn, "bank book balance -500",
        target="account", target_qbo_id=overdrawn)

    seeder.step("SEED-20 R034 untagged COGS cluster")
    # 20. R034 — $5,600 of COGS this month, all untagged (also trips R030
    # per line >= $500 — expected collateral)
    for n, amount in enumerate((2000.00, 2000.00, 1600.00), start=1):
        seeder.purchase(amount=amount,
                        txn_date=TODAY.replace(day=min(TODAY.day, 4)),
                        bank=bank, expense=cogs, vendor=vendor_r034,
                        note=f"{TAG}-SUPPORT-20-{n} untagged COGS cluster")
    add(20, "R034", "Customer", "", "untagged COGS ratio 100% of $5.6k",
        target="client", target_qbo_id="(client)")

    seeder.step("SEED-21 R035 cost-active unbilled job")
    # 21. R035 — job burning $12k with no invoice in 30 days
    parent_r035 = customer_for("R035")
    unbilled_job = customer_for("R035", parent=parent_r035)
    seeder.bill(amount=12000.00, txn_date=TODAY - timedelta(days=10),
                vendor=vendor_r035, expense=cogs, job_customer=unbilled_job,
                note=f"{TAG}-21 unbilled job costs")
    add(21, "R035", "Customer", unbilled_job, "cost-active job, zero"
        " invoices in 30d", target="job", target_qbo_id=unbilled_job)

    return items


def cleanup_prior_generations(
    seeder: Seeder, conn: psycopg.Connection, client_id: UUID
) -> dict[str, Any]:
    """Neutralize prior generations' OPEN-balance seed artifacts.

    Candidates come from the seed tag in STAGED payloads (local, no API
    cost); their live state (Balance, SyncToken) comes from QBO. Voids
    invoices; zero-out VendorCredits for bills; skips anything closed,
    gone, or oddly shaped — with the reason."""
    candidates = conn.execute(
        """
        SELECT DISTINCT entity_type, qbo_id FROM qbo_raw
        WHERE client_id = %s AND entity_type IN ('Invoice', 'Bill')
          AND payload ->> 'PrivateNote' LIKE 'CAROLUS-SEED%%'
          AND payload ->> 'PrivateNote' NOT LIKE '%%CLEANUP%%'
        ORDER BY entity_type, qbo_id
        """,
        (client_id,),
    ).fetchall()
    voided = credited = 0
    skipped: list[tuple[str, str]] = []
    for entity_type, qbo_id in candidates:
        label = f"{entity_type} {qbo_id}"
        seeder.step(f"cleanup {label}")
        live = seeder._query(entity_type, f"Id = '{qbo_id}'")
        if not live:
            skipped.append((label, "no longer exists in QBO"))
            continue
        payload = live[0]
        balance = float(payload.get("Balance") or 0)
        if balance <= 0:
            skipped.append((label, "no open balance"))
            continue
        if entity_type == "Invoice":
            seeder._create(
                "Invoice",
                {"Id": str(payload["Id"]),
                 "SyncToken": str(payload["SyncToken"])},
                params={"operation": "void"},
            )
            voided += 1
        else:  # Bill — not voidable; offset with a zero-out vendor credit
            vendor_ref = (payload.get("VendorRef") or {}).get("value")
            first_line = (payload.get("Line") or [{}])[0]
            account_ref = ((first_line.get("AccountBasedExpenseLineDetail")
                            or {}).get("AccountRef") or {}).get("value")
            if not (vendor_ref and account_ref):
                skipped.append((label, "unsupported shape for vendor credit"))
                continue
            seeder._create("VendorCredit", {
                "VendorRef": {"value": str(vendor_ref)},
                "TxnDate": TODAY.isoformat(),
                "PrivateNote": f"{TAG}-CLEANUP bill {qbo_id}",
                "Line": [{
                    "Amount": balance,
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "AccountBasedExpenseLineDetail": {
                        "AccountRef": {"value": str(account_ref)},
                    },
                }],
            })
            credited += 1
    return {"voided": voided, "credited": credited, "skipped": skipped}


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
    parser.add_argument("--reseed", action="store_true",
                        help="force a new generation (cleanup + mint) even"
                             " when the manifest is intact")
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

        if MANIFEST_PATH.exists() and not args.reseed:
            manifest = json.loads(MANIFEST_PATH.read_text())
            if (manifest.get("realm") == args.realm
                    and len(manifest.get("items", [])) == EXPECTED_SEEDS
                    and verify_existing(qbo, manifest)):
                print(f"already seeded — manifest intact at {MANIFEST_PATH}"
                      " (use --reseed to force a new generation)")
                _print_manifest(manifest)
                return 0
            print("manifest stale, incomplete, or pre-v2 — reseeding")
        elif args.reseed:
            print("--reseed: forcing a new generation")

        seeder = Seeder(qbo)
        nonce = secrets.token_hex(3)
        try:
            cleanup = cleanup_prior_generations(seeder, conn, client_id)
            print(f"cleanup: {cleanup['voided']} voided,"
                  f" {cleanup['credited']} credited,"
                  f" {len(cleanup['skipped'])} skipped")
            for label, why in cleanup["skipped"]:
                print(f"  skipped {label}: {why}")
            items = seed_all(seeder, nonce)
        except SeedFailure as exc:
            print(f"\nSEED ABORTED — {exc}", file=sys.stderr)
            print("(no manifest written; objects created before this step"
                  " will be found by name on the next run)", file=sys.stderr)
            return 1

    manifest = {
        "realm": args.realm,
        "seeded_on": TODAY.isoformat(),
        "nonce": nonce,
        "items": [asdict(item) for item in items],
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"seeded {len(items)} violations; manifest -> {MANIFEST_PATH}")
    _print_manifest(manifest)
    print("\nNOTE: R020's window is the current calendar month — run the"
          " gate this month.")
    print("NOT API-SEEDABLE (unit-test covered instead):")
    print("  R018 — needs a green-closed period plus a post-close edit;"
          " close_runs is internal state (tests/test_rules_audit_new.py)")
    print("  R022 — QBO rejects BillPayments with no linked Bill"
          " (tests/test_rules_vendor.py)")
    print("  R031 — job completion status/date are curated canonical"
          " columns, not API fields (tests/test_rules_construction.py)")
    return 0


def _print_manifest(manifest: dict[str, Any]) -> None:
    print(f"{'n':>3} {'rule':<6} {'entity':<13} {'qbo_id':<10} description")
    for item in manifest["items"]:
        print(f"{item['n']:>3} {item['rule_code']:<6} {item['entity_type']:<13}"
              f" {item['qbo_id']:<10} {item['description']}")


if __name__ == "__main__":
    raise SystemExit(main())
