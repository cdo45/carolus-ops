"""Schema-shape tests for every QBO create payload the seeder emits.

These assert the payloads against the v3 entity contracts (required
fields, DetailType strings, ref shapes, property casing) using a
recording fake — no live API needed. The uppercase-'Type' regression
(QBO fault 2010, unsupported property) is pinned here: no payload may
contain a 'Type' key on a transaction ref; the v3 sub-property is
lowercase 'type'. (Item.Type is the one legitimate top-level 'Type'
property, asserted explicitly.)
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest

from sync.qbo_client import QboRequestError
from tests.seed_errors import (
    SeedFailure,
    Seeder,
    cleanup_prior_generations,
    format_qbo_fault,
    seed_all,
)

VALID_DETAIL_TYPES = {
    "AccountBasedExpenseLineDetail",
    "JournalEntryLineDetail",
    "SalesItemLineDetail",
}
TXN_DATE = date(2026, 6, 1)


class RecordingQbo:
    """Stands in for QboClient: records every create, finds nothing."""

    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.created_params: list[dict[str, str] | None] = []
        self._next_id = 0

    def query(self, entity: str, where: str | None = None) -> list[dict[str, Any]]:
        return []

    def create(self, entity: str, payload: dict[str, Any],
               *, params: dict[str, str] | None = None) -> dict[str, Any]:
        self.created.append((entity, payload))
        self.created_params.append(params)
        self._next_id += 1
        return {entity: {"Id": str(self._next_id)}}


@pytest.fixture
def seeder() -> Seeder:
    return Seeder(RecordingQbo())  # type: ignore[arg-type]


def recorder(seeder: Seeder) -> RecordingQbo:
    return seeder.qbo  # type: ignore[return-value]


def created(seeder: Seeder, entity: str) -> list[dict[str, Any]]:
    return [p for e, p in recorder(seeder).created if e == entity]


def walk_keys(obj: Any) -> Iterator[str]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from walk_keys(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk_keys(value)


# ------------------------------------------------------------ transactions


def test_purchase_payload_shape(seeder: Seeder) -> None:
    seeder.purchase(amount=750.0, txn_date=TXN_DATE, bank="35", expense="64",
                    note="CAROLUS-SEED-1", vendor="56", job_customer="77")
    (payload,) = created(seeder, "Purchase")

    assert payload["PaymentType"] in ("Cash", "Check", "CreditCard")
    assert payload["AccountRef"] == {"value": "35"}, "header ref = paying account"
    assert payload["TxnDate"] == "2026-06-01"
    assert payload["PrivateNote"].startswith("CAROLUS-SEED")
    (line,) = payload["Line"]
    assert line["DetailType"] == "AccountBasedExpenseLineDetail"
    detail = line["AccountBasedExpenseLineDetail"]
    assert detail["AccountRef"] == {"value": "64"}
    assert detail["CustomerRef"] == {"value": "77"}
    assert line["Amount"] == 750.0
    assert payload["EntityRef"] == {"value": "56", "type": "Vendor"}, (
        "payee ref uses lowercase 'type' per the v3 EntityRef schema"
    )
    assert "Type" not in walk_keys(payload), "uppercase 'Type' fails QBO parse"


def test_purchase_without_vendor_omits_entity_ref(seeder: Seeder) -> None:
    seeder.purchase(amount=800.0, txn_date=TXN_DATE, bank="35", expense="64",
                    note="CAROLUS-SEED-12")
    (payload,) = created(seeder, "Purchase")
    assert "EntityRef" not in payload
    assert "CustomerRef" not in payload["Line"][0]["AccountBasedExpenseLineDetail"]


def test_bill_payload_shape(seeder: Seeder) -> None:
    seeder.bill(amount=410.0, txn_date=TXN_DATE, vendor="56", expense="64",
                note="CAROLUS-SEED-2", doc_number="CAROLUS-DUP-1",
                job_customer="77")
    (payload,) = created(seeder, "Bill")

    assert payload["VendorRef"] == {"value": "56"}
    assert payload["DocNumber"] == "CAROLUS-DUP-1"
    (line,) = payload["Line"]
    assert line["DetailType"] == "AccountBasedExpenseLineDetail"
    assert line["AccountBasedExpenseLineDetail"]["AccountRef"] == {"value": "64"}
    assert line["AccountBasedExpenseLineDetail"]["CustomerRef"] == {"value": "77"}


def test_journal_entry_payload_shape(seeder: Seeder) -> None:
    seeder.journal_entry(amount=5000.0, txn_date=TXN_DATE, debit="64",
                         credit="35", note="CAROLUS-SEED-3")
    (payload,) = created(seeder, "JournalEntry")

    debit_line, credit_line = payload["Line"]
    for line in (debit_line, credit_line):
        assert line["DetailType"] == "JournalEntryLineDetail"
        assert line["Amount"] == 5000.0
        assert line["JournalEntryLineDetail"]["AccountRef"]["value"]
    assert debit_line["JournalEntryLineDetail"]["PostingType"] == "Debit"
    assert credit_line["JournalEntryLineDetail"]["PostingType"] == "Credit"


def test_invoice_payload_shape(seeder: Seeder) -> None:
    seeder.invoice(amount=25000.0, txn_date=TXN_DATE, customer="88",
                   item="11", note="CAROLUS-SEED-11")
    (payload,) = created(seeder, "Invoice")

    assert payload["CustomerRef"] == {"value": "88"}
    (line,) = payload["Line"]
    assert line["DetailType"] == "SalesItemLineDetail"
    assert line["SalesItemLineDetail"]["ItemRef"] == {"value": "11"}
    assert line["Amount"] == 25000.0


def test_unapplied_payment_payload_shape(seeder: Seeder) -> None:
    seeder.unapplied_payment(amount=1000.0, txn_date=TXN_DATE, customer="88",
                             note="CAROLUS-SEED-15")
    (payload,) = created(seeder, "Payment")

    assert payload["CustomerRef"] == {"value": "88"}
    assert payload["TotalAmt"] == 1000.0
    assert "Line" not in payload, "no Line = unapplied payment, per the v3 docs"


# ------------------------------------------------------------ support objects


def test_support_object_payload_shapes(seeder: Seeder) -> None:
    seeder.vendor("CAROLUS SEED VENDOR A")
    seeder.customer("CAROLUS SEED CUSTOMER")
    parent = recorder(seeder).created[-1]
    seeder.customer("CAROLUS SEED JOB", parent_id="2")
    seeder.account("CAROLUS Seed COGS", "Cost of Goods Sold")
    seeder.item("CAROLUS Seed Service", "5")

    by_entity = dict(recorder(seeder).created)
    assert by_entity["Vendor"] == {"DisplayName": "CAROLUS SEED VENDOR A"}
    assert parent[1] == {"DisplayName": "CAROLUS SEED CUSTOMER"}
    job = created(seeder, "Customer")[-1]
    assert job["Job"] is True and job["ParentRef"] == {"value": "2"}
    assert by_entity["Account"] == {
        "Name": "CAROLUS Seed COGS", "AccountType": "Cost of Goods Sold",
    }
    assert by_entity["Item"] == {
        "Name": "CAROLUS Seed Service", "Type": "Service",
        "IncomeAccountRef": {"value": "5"},
    }


# ------------------------------------------------------------ whole flow


def test_seed_all_emits_only_valid_shapes(seeder: Seeder) -> None:
    """Every payload the full seeding flow sends, checked in one sweep."""
    items = seed_all(seeder, "abc123")

    assert len(items) == 21
    assert len({item.rule_code for item in items}) == 21

    txn_payloads = [
        (entity, payload)
        for entity, payload in recorder(seeder).created
        if entity in ("Purchase", "Bill", "JournalEntry", "Invoice", "Payment")
    ]
    assert txn_payloads, "flow must create transactions"
    for entity, payload in txn_payloads:
        assert payload["TxnDate"], f"{entity} missing TxnDate"
        assert payload["PrivateNote"].startswith("CAROLUS-SEED"), (
            f"{entity} not tagged for re-discovery"
        )
        for line in payload.get("Line", []):
            detail_type = line.get("DetailType")
            assert detail_type in VALID_DETAIL_TYPES, (
                f"{entity} line has bad DetailType {detail_type!r}"
            )
            assert detail_type in line, (
                f"{entity} line missing its {detail_type} detail object"
            )
            assert line.get("Amount"), f"{entity} line missing Amount"
        bad_keys = [k for k in walk_keys(payload) if k == "Type"]
        assert not bad_keys, (
            f"{entity} payload contains uppercase 'Type' — QBO fault 2010"
        )


def test_generations_mint_unique_entities() -> None:
    """The pollution invariant: two seeding generations share NO
    history-sensitive entities, and history-sensitive amounts are salted —
    so trailing-window/first-ever rules always see clean history."""
    generations: dict[str, tuple[set[str], list[float]]] = {}
    for nonce in ("aaaaaa", "bbbbbb"):
        seeder = Seeder(RecordingQbo())  # type: ignore[arg-type]
        seed_all(seeder, nonce)
        names = {
            payload["DisplayName"]
            for entity, payload in recorder(seeder).created
            if entity in ("Vendor", "Customer")
        }
        purchase_amounts = sorted(
            payload["Line"][0]["Amount"]
            for entity, payload in recorder(seeder).created
            if entity == "Purchase"
        )
        generations[nonce] = (names, purchase_amounts)

    names_a, amounts_a = generations["aaaaaa"]
    names_b, amounts_b = generations["bbbbbb"]
    assert names_a and names_a.isdisjoint(names_b), (
        "no vendor/customer may be shared across generations"
    )
    assert all("AAAAAA" in name for name in names_a), (
        "every minted entity carries its generation nonce"
    )
    assert amounts_a != amounts_b, (
        "history-sensitive amounts must be salted per generation"
    )


def test_seed_entity_mapping_is_injective() -> None:
    """Strict entity-per-seed isolation: no vendor/customer is referenced
    by more than ONE rule seed. (R010's twin pair shares its vendor by
    design — both its transactions carry seed number 1, so the mapping
    stays injective.) This is the invariant whose violation made R026's
    backdated bill steal R021's first-ever-transaction slot."""
    import re

    seeder = Seeder(RecordingQbo())  # type: ignore[arg-type]
    seed_all(seeder, "abc123")

    entity_names: dict[str, str] = {}
    for index, (entity, payload) in enumerate(recorder(seeder).created, 1):
        if entity in ("Vendor", "Customer"):
            entity_names[str(index)] = payload["DisplayName"]

    seed_number = re.compile(r"CAROLUS-SEED-(?:SUPPORT-)?(\d+)")
    used_by: dict[str, set[str]] = {}
    for entity, payload in recorder(seeder).created:
        note = payload.get("PrivateNote", "")
        match = seed_number.search(note)
        if not match:
            continue
        seed = match.group(1)
        refs: list[str] = []
        for key in ("EntityRef", "VendorRef", "CustomerRef"):
            if key in payload:
                refs.append(payload[key]["value"])
        for line in payload.get("Line", []):
            detail = line.get("AccountBasedExpenseLineDetail", {})
            if "CustomerRef" in detail:
                refs.append(detail["CustomerRef"]["value"])
        for ref in refs:
            if ref in entity_names:  # ignore account refs
                used_by.setdefault(ref, set()).add(seed)

    offenders = {entity_names[ref]: sorted(seeds)
                 for ref, seeds in used_by.items() if len(seeds) > 1}
    assert offenders == {}, f"entities shared across rule seeds: {offenders}"
    assert used_by, "sanity: the mapping is non-empty"


# ------------------------------------------------------------ cleanup


class CleanupQbo(RecordingQbo):
    """Scripted live states for the generation-cleanup pass."""

    def __init__(self, live: dict[str, dict[str, Any]]) -> None:
        super().__init__()
        self.live = live

    def query(self, entity: str, where: str | None = None) -> list[dict[str, Any]]:
        assert where is not None and where.startswith("Id = '")
        qbo_id = where.split("'")[1]
        payload = self.live.get(qbo_id)
        return [payload] if payload is not None else []


def test_cleanup_voids_credits_and_skips(conn: Any) -> None:
    """Open invoice -> voided; open bill -> zero-out VendorCredit tagged
    CLEANUP; paid/vanished artifacts skipped with reasons; untagged and
    already-CLEANUP rows never even queried."""
    from psycopg.types.json import Jsonb

    from tests.conftest import make_client

    client_id = make_client(conn)

    def stage(entity: str, qbo_id: str, note: str) -> None:
        conn.execute(
            "INSERT INTO qbo_raw (client_id, entity_type, qbo_id, payload)"
            " VALUES (%s, %s, %s, %s)",
            (client_id, entity, qbo_id,
             Jsonb({"Id": qbo_id, "PrivateNote": note})),
        )

    stage("Invoice", "901", "CAROLUS-SEED-16 stale receivable")
    stage("Bill", "902", "CAROLUS-SEED-17 aged payable")
    stage("Invoice", "903", "CAROLUS-SEED-11 AR concentration")  # paid off
    stage("Invoice", "904", "CAROLUS-SEED-16 older gen")  # gone from QBO
    stage("Invoice", "905", "client uploaded, untouchable")  # untagged
    stage("Bill", "906", "CAROLUS-SEED-CLEANUP bill 88")  # cleanup artifact
    conn.commit()

    qbo = CleanupQbo({
        "901": {"Id": "901", "SyncToken": "3", "Balance": 1500.37},
        "902": {"Id": "902", "SyncToken": "1", "Balance": 1200.0,
                "VendorRef": {"value": "77"},
                "Line": [{"AccountBasedExpenseLineDetail":
                          {"AccountRef": {"value": "64"}}}]},
        "903": {"Id": "903", "SyncToken": "5", "Balance": 0},
        "905": {"Id": "905", "SyncToken": "1", "Balance": 999.0},
    })
    seeder = Seeder(qbo)  # type: ignore[arg-type]

    summary = cleanup_prior_generations(seeder, conn, client_id)

    assert summary["voided"] == 1
    assert summary["credited"] == 1
    assert summary["skipped"] == [
        ("Invoice 903", "no open balance"),
        ("Invoice 904", "no longer exists in QBO"),
    ]

    by_entity = {(entity, payload.get("Id")): (payload, params)
                 for (entity, payload), params in zip(
                     qbo.created, qbo.created_params, strict=True)}
    void_payload, void_params = by_entity[("Invoice", "901")]
    assert void_params == {"operation": "void"}
    assert void_payload == {"Id": "901", "SyncToken": "3"}

    (credit_payload, credit_params) = by_entity[("VendorCredit", None)]
    assert credit_params is None
    assert credit_payload["VendorRef"] == {"value": "77"}
    assert credit_payload["Line"][0]["Amount"] == 1200.0
    assert credit_payload["Line"][0]["AccountBasedExpenseLineDetail"] == {
        "AccountRef": {"value": "64"},
    }
    assert "CAROLUS-SEED-CLEANUP" in credit_payload["PrivateNote"]


# ------------------------------------------------------------ error reporting


def test_seed_failure_identifies_step_and_fault() -> None:
    class Rejecting(RecordingQbo):
        def create(self, entity: str, payload: dict[str, Any],
                   *, params: dict[str, str] | None = None) -> dict[str, Any]:
            raise QboRequestError(400, json.dumps({
                "Fault": {"Error": [{
                    "Message": "Request has invalid or unsupported property",
                    "Detail": "Property Name:Type specified is unsupported",
                    "code": "2010",
                }]},
            }))

    seeder = Seeder(Rejecting())  # type: ignore[arg-type]
    seeder.step("SEED-1 R010 duplicate purchase pair")
    with pytest.raises(SeedFailure) as excinfo:
        seeder.purchase(amount=750.0, txn_date=TXN_DATE, bank="35",
                        expense="64", note="CAROLUS-SEED-1", vendor="56")
    message = str(excinfo.value)
    assert "SEED-1 R010" in message, "failure names the seed item"
    assert "Purchase" in message
    assert "code 2010" in message and "unsupported" in message
    assert "payload sent" in message


def test_format_qbo_fault_falls_back_on_unparseable_body() -> None:
    exc = QboRequestError(400, "<html>gateway error</html>")
    assert format_qbo_fault(exc) == "HTTP 400: <html>gateway error</html>"
