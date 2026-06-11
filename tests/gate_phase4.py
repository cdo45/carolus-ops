"""PHASE 4 GATE — checksum, bank rec, matching, idempotent intake. No LLM.

Two modes, same checks:

  uv run python -m tests.gate_phase4 --realm <realm_id> [--period YYYY-MM]
      Live mode: runs against DATABASE_URL for a client whose sandbox
      sync has already been done (sync first; the gate makes no QBO
      calls). Receipts/statements are synthesized from that client's
      canonical transactions.

  CAROLUS_TEST_DB=... uv run python -m tests.gate_phase4 --fixture
      Fixture mode: DESTRUCTIVE scratch-database run (refuses to run
      against DATABASE_URL) seeded with the FakeQbo company + factory
      transactions — fully local, deterministic, CI-runnable.

Checks:
  (a) every fixture statement lands validated or escalated with the
      correct reason; the corrupted one escalates checksum_failed and
      parses NOTHING; zero documents in any other terminal state
  (b) the clean statement recs tied to the penny; the phantom line
      raises R040 with a source_ref that resolves to the document row
  (c) six receipts: 4 matched (txns backed), 1 ambiguous_match,
      1 no_matching_txn — exact terminal states
  (d) the request list contains the orphan receipt and the known
      undocumented transactions
  (e) duplicate re-ingest of every file creates zero new rows (sha256)
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from db.migrate import migrate  # noqa: E402
from docpipe.bankrec import rec  # noqa: E402
from docpipe.classify import classify_document  # noqa: E402
from docpipe.intake import ingest  # noqa: E402
from docpipe.matching import match_document  # noqa: E402
from docpipe.pdf import pdf_text  # noqa: E402
from docpipe.receipts import DeterministicTextExtractor, extract_receipt  # noqa: E402
from docpipe.requests import generate_request_list  # noqa: E402
from docpipe.statements import extract_statement  # noqa: E402
from docpipe.storage import LocalFSStorage  # noqa: E402
from tests.fixtures.make_statements import (  # noqa: E402
    FixtureLine,
    ending_balance,
    image_only_pdf,
    receipt_pdf,
    statement_lines_from_canonical,
    statement_pdf,
)

WORKDIR = Path(__file__).resolve().parent.parent / "data" / "gate_phase4"
BEGINNING = Decimal("5000.00")


@dataclass(frozen=True)
class ReceiptPlan:
    filename: str
    merchant: str
    txn_date: date
    total: Decimal
    expect: str  # matched | ambiguous_match | no_matching_txn


@dataclass
class GateContext:
    conn: psycopg.Connection
    client_id: UUID
    bank_account_id: UUID
    period_start: date
    period_end: date
    receipts: list[ReceiptPlan]
    expected_undocumented_qbo_ids: set[str]
    storage: LocalFSStorage
    workdir: Path


def _pipeline_statement(ctx: GateContext, path: Path) -> tuple[UUID, str, str | None]:
    """ingest -> classify -> extract; returns (doc_id, status, reason)."""
    document_id = ingest(ctx.conn, ctx.client_id, path, "gate",
                         storage=ctx.storage).document_id
    text = pdf_text(path.read_bytes())
    if classify_document(ctx.conn, document_id, text=text) == "classified":
        extract_statement(ctx.conn, document_id, storage=ctx.storage)
    row = ctx.conn.execute(
        "SELECT status, escalation_reason FROM documents WHERE id = %s",
        (document_id,),
    ).fetchone()
    assert row is not None
    return document_id, row[0], row[1]


def run_checks(ctx: GateContext) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    conn, client_id = ctx.conn, ctx.client_id
    ctx.workdir.mkdir(parents=True, exist_ok=True)

    # ---- build the four statements from canonical activity
    lines = statement_lines_from_canonical(
        conn, client_id, ctx.bank_account_id, ctx.period_start, ctx.period_end
    )
    assert lines, "gate needs bank activity in the chosen period"
    clean_end = ending_balance(BEGINNING, lines)
    phantom_line = FixtureLine(
        ctx.period_start + timedelta(days=19), "ATM WITHDRAWAL 7741",
        Decimal("45.00"), "debit",
    )
    phantom_lines = [*lines, phantom_line]
    common = {"period_start": ctx.period_start, "period_end": ctx.period_end,
              "beginning": BEGINNING}
    files = {
        "clean": statement_pdf(ctx.workdir / "statement-clean.pdf",
                               ending=clean_end, lines=lines,
                               stated_count=len(lines), **common),
        "corrupted": statement_pdf(ctx.workdir / "statement-corrupted.pdf",
                                   ending=clean_end + Decimal("100.00"),
                                   lines=lines, stated_count=len(lines),
                                   **common),
        "phantom": statement_pdf(ctx.workdir / "statement-phantom.pdf",
                                 ending=ending_balance(BEGINNING, phantom_lines),
                                 lines=phantom_lines,
                                 stated_count=len(phantom_lines), **common),
        "image": image_only_pdf(ctx.workdir / "statement-image.pdf"),
    }

    # (a) terminal states
    outcomes = {name: _pipeline_statement(ctx, path)
                for name, path in files.items()}
    expected = {
        "clean": ("validated", None),
        "corrupted": ("escalated", "checksum_failed"),
        "phantom": ("validated", None),
        "image": ("escalated", "needs_ocr"),
    }
    terminal_ok = all(
        (outcomes[name][1], outcomes[name][2]) == expected[name]
        for name in expected
    )
    corrupted_doc = outcomes["corrupted"][0]
    nothing_parsed = conn.execute(
        "SELECT extracted IS NULL AND period_start IS NULL FROM documents"
        " WHERE id = %s",
        (corrupted_doc,),
    ).fetchone() == (True,)
    stray = conn.execute(
        """
        SELECT count(*) FROM documents
        WHERE client_id = %s AND doc_type = 'bank_statement'
          AND status NOT IN ('validated', 'escalated')
        """,
        (client_id,),
    ).fetchone()
    assert stray is not None
    results.append((
        "(a) statement terminals exact; corrupted parsed nothing",
        terminal_ok and nothing_parsed and stray[0] == 0,
        ", ".join(f"{k}={v[1]}/{v[2] or '-'}" for k, v in outcomes.items()),
    ))

    # (b) rec: clean ties; phantom raises R040 with resolvable source_ref
    clean_rec = rec(conn, client_id, ctx.bank_account_id, outcomes["clean"][0])
    phantom_rec = rec(conn, client_id, ctx.bank_account_id,
                      outcomes["phantom"][0])
    r040 = conn.execute(
        """
        SELECT f.source_ref,
               EXISTS (SELECT 1 FROM documents d
                       WHERE d.id = f.source_ref::uuid
                         AND d.client_id = f.client_id) AS resolves
        FROM flags f
        WHERE f.client_id = %s AND f.rule_code = 'R040' AND f.status = 'open'
        """,
        (client_id,),
    ).fetchall()
    results.append((
        "(b) clean rec ties to the penny; phantom raises R040 w/ valid ref",
        (clean_rec.tied and clean_rec.unmatched_statement_lines == []
         and not phantom_rec.tied
         and len(r040) == 1
         and r040[0][0] == str(outcomes["phantom"][0])
         and r040[0][1] is True),
        f"clean tied={clean_rec.tied} matched={clean_rec.matched_count};"
        f" phantom unmatched={len(phantom_rec.unmatched_statement_lines)},"
        f" R040 flags={len(r040)}",
    ))

    # (c) receipts: ingest -> classify -> extract -> match
    receipt_terminal: dict[str, tuple[str, str | None]] = {}
    receipt_docs: list[tuple[Path, UUID]] = []
    for plan in ctx.receipts:
        path = receipt_pdf(ctx.workdir / plan.filename, merchant=plan.merchant,
                           txn_date=plan.txn_date, total=plan.total)
        document_id = ingest(conn, client_id, path, "gate",
                             storage=ctx.storage).document_id
        receipt_docs.append((path, document_id))
        text = pdf_text(path.read_bytes())
        if classify_document(conn, document_id, text=text) == "classified":
            if extract_receipt(conn, document_id,
                               extractor=DeterministicTextExtractor(),
                               storage=ctx.storage) == "validated":
                match_document(conn, document_id)
        row = conn.execute(
            "SELECT status, escalation_reason FROM documents WHERE id = %s",
            (document_id,),
        ).fetchone()
        assert row is not None
        receipt_terminal[plan.filename] = (row[0], row[1])

    def _expected_terminal(plan: ReceiptPlan) -> tuple[str, str | None]:
        if plan.expect == "matched":
            return ("matched", None)
        return ("escalated", plan.expect)

    receipts_ok = all(
        receipt_terminal[plan.filename] == _expected_terminal(plan)
        for plan in ctx.receipts
    )
    backed = conn.execute(
        "SELECT count(*) FROM transactions t JOIN documents d"
        " ON d.matched_txn = t.id WHERE d.client_id = %s"
        " AND t.doc_status = 'backed'",
        (client_id,),
    ).fetchone()
    assert backed is not None
    expected_matched = sum(1 for p in ctx.receipts if p.expect == "matched")
    results.append((
        "(c) receipt terminals exact; matched txns backed",
        receipts_ok and backed[0] == expected_matched,
        ", ".join(f"{name}={status}/{reason or '-'}"
                  for name, (status, reason) in receipt_terminal.items()),
    ))

    # (d) request list: orphan receipt + known undocumented transactions
    requests = generate_request_list(conn, client_id, ctx.period_start,
                                     ctx.period_end)
    listed_qbo_ids = {txn["qbo_id"] for txn in requests.undocumented_txns}
    orphan_files = {plan.filename for plan in ctx.receipts
                    if plan.expect == "no_matching_txn"}
    listed_files = {doc["filename"] for doc in requests.unmatched_documents}
    results.append((
        "(d) request list: orphan receipt + undocumented txns",
        orphan_files <= listed_files
        and ctx.expected_undocumented_qbo_ids <= listed_qbo_ids
        and bool(requests.undocumented_txns),
        f"undocumented={sorted(listed_qbo_ids)},"
        f" unmatched_docs={sorted(listed_files)}",
    ))

    # (e) duplicate re-ingest: zero new rows
    before = conn.execute(
        "SELECT count(*) FROM documents WHERE client_id = %s", (client_id,)
    ).fetchone()
    every_file = list(files.values()) + [path for path, _ in receipt_docs]
    duplicates = [ingest(conn, client_id, path, "gate-again",
                         storage=ctx.storage) for path in every_file]
    after = conn.execute(
        "SELECT count(*) FROM documents WHERE client_id = %s", (client_id,)
    ).fetchone()
    results.append((
        "(e) duplicate re-ingest of every file: zero new rows",
        all(not result.created for result in duplicates) and before == after,
        f"{len(every_file)} files re-ingested, documents {before[0]}"  # type: ignore[index]
        f" -> {after[0]}",  # type: ignore[index]
    ))
    return results


# ------------------------------------------------------------ fixture mode


def seed_fixture_client(conn: psycopg.Connection) -> GateContext:
    from sync.full_sync import run_full_sync
    from tests.factories import balanced_purchase, make_entity
    from tests.qbo_fixtures import FakeQbo

    row = conn.execute(
        "INSERT INTO clients (name, qbo_realm_id) VALUES"
        " ('Gate Four Constructors', 'gate-p4') RETURNING id"
    ).fetchone()
    assert row is not None
    client_id: UUID = row[0]
    conn.commit()
    run_full_sync(conn, client_id, "gate-p4", qbo=FakeQbo())

    def account(qbo_id: str) -> UUID:
        found = conn.execute(
            "SELECT id FROM accounts WHERE client_id = %s AND qbo_id = %s",
            (client_id, qbo_id),
        ).fetchone()
        assert found is not None
        return found[0]

    bank, cogs = account("1"), account("40")
    vendor = make_entity(conn, client_id, kind="vendor", name="Gate Supply Co")

    def spend(amount: str, day: int) -> None:
        balanced_purchase(conn, client_id, amount=amount,
                          txn_date=date(2026, 5, day), entity_id=vendor,
                          bank=bank, expense=cogs)

    for amount, day in (("61.20", 4), ("77.35", 8), ("142.10", 15),
                        ("53.80", 20)):
        spend(amount, day)  # clean receipt targets
    spend("66.10", 18)  # ambiguous pair...
    spend("66.10", 19)
    request_seed = balanced_purchase(  # known undocumented, >= $75
        conn, client_id, amount="150.00", txn_date=date(2026, 5, 22),
        entity_id=vendor, bank=bank, expense=cogs,
    )
    conn.commit()
    request_seed_qbo = conn.execute(
        "SELECT qbo_id FROM transactions WHERE id = %s", (request_seed,)
    ).fetchone()
    assert request_seed_qbo is not None

    receipts = [
        ReceiptPlan("receipt-1.pdf", "GATE SUPPLY CO", date(2026, 5, 4),
                    Decimal("61.20"), "matched"),
        ReceiptPlan("receipt-2.pdf", "GATE SUPPLY CO", date(2026, 5, 8),
                    Decimal("77.35"), "matched"),
        ReceiptPlan("receipt-3.pdf", "GATE SUPPLY CO", date(2026, 5, 15),
                    Decimal("142.10"), "matched"),
        ReceiptPlan("receipt-4.pdf", "GATE SUPPLY CO", date(2026, 5, 20),
                    Decimal("53.80"), "matched"),
        ReceiptPlan("receipt-5.pdf", "GATE SUPPLY CO", date(2026, 5, 18),
                    Decimal("66.10"), "ambiguous_match"),
        ReceiptPlan("receipt-6.pdf", "ROADSIDE DINER", date(2026, 5, 9),
                    Decimal("123.99"), "no_matching_txn"),
    ]
    return GateContext(
        conn=conn, client_id=client_id, bank_account_id=bank,
        period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
        receipts=receipts,
        # fixture company spend that stays unbacked: purchase 5001 ($89.99),
        # bill 2001 ($320), and the seeded $150 purchase
        expected_undocumented_qbo_ids={"5001", "2001", request_seed_qbo[0]},
        storage=LocalFSStorage(root=WORKDIR / "docstore"),
        workdir=WORKDIR,
    )


# ------------------------------------------------------------ live mode


def live_context(
    conn: psycopg.Connection, realm: str, period: str | None
) -> GateContext:
    from rules.close_checklist import parse_period

    row = conn.execute(
        "SELECT id FROM clients WHERE qbo_realm_id = %s", (realm,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"no client for realm {realm} — sync first")
    client_id: UUID = row[0]

    if period:
        period_start, period_end = parse_period(period)
    else:  # busiest bank month
        busiest = conn.execute(
            """
            SELECT date_trunc('month', t.txn_date)::date
            FROM journal_lines jl
            JOIN transactions t ON t.id = jl.transaction_id
            JOIN accounts a ON a.id = jl.account_id
            WHERE t.client_id = %s AND a.acct_type = 'Bank'
            GROUP BY 1 ORDER BY count(*) DESC, 1 DESC LIMIT 1
            """,
            (client_id,),
        ).fetchone()
        if busiest is None:
            raise SystemExit("no bank activity found — sync first")
        period_start = busiest[0]
        period_end = parse_period(period_start.strftime("%Y-%m"))[1]

    bank = conn.execute(
        """
        SELECT a.id FROM accounts a
        JOIN journal_lines jl ON jl.account_id = a.id
        WHERE a.client_id = %s AND a.acct_type = 'Bank'
        GROUP BY a.id ORDER BY count(*) DESC LIMIT 1
        """,
        (client_id,),
    ).fetchone()
    if bank is None:
        raise SystemExit("no bank account with activity — sync first")

    uniques = conn.execute(
        """
        SELECT min(t.txn_date), t.amount,
               min(COALESCE(e.name, 'VENDOR')) AS vendor
        FROM transactions t
        LEFT JOIN entities e ON e.id = t.entity_id
        WHERE t.client_id = %s AND t.txn_type IN ('Purchase', 'Bill')
          AND t.txn_date BETWEEN %s AND %s AND t.qbo_deleted_at IS NULL
        GROUP BY t.amount HAVING count(*) = 1
        ORDER BY 2 DESC LIMIT 4
        """,
        (client_id, period_start, period_end),
    ).fetchall()
    duplicated = conn.execute(
        """
        SELECT min(t.txn_date), t.amount
        FROM transactions t
        WHERE t.client_id = %s AND t.txn_type IN ('Purchase', 'Bill')
          AND t.txn_date BETWEEN %s AND %s AND t.qbo_deleted_at IS NULL
        GROUP BY t.amount HAVING count(*) > 1
        ORDER BY 2 DESC LIMIT 1
        """,
        (client_id, period_start, period_end),
    ).fetchone()
    if len(uniques) < 4 or duplicated is None:
        raise SystemExit(
            "need >= 4 unique-amount and 1 duplicated-amount spend txns in"
            f" {period_start:%Y-%m} — run tests/seed_errors.py first"
        )
    receipts = [
        ReceiptPlan(f"receipt-{index}.pdf", vendor, txn_date, amount, "matched")
        for index, (txn_date, amount, vendor) in enumerate(uniques, start=1)
    ]
    receipts.append(ReceiptPlan("receipt-5.pdf", "SEED VENDOR",
                                duplicated[0], duplicated[1],
                                "ambiguous_match"))
    receipts.append(ReceiptPlan("receipt-6.pdf", "ROADSIDE DINER",
                                period_start + timedelta(days=8),
                                Decimal("9123.47"), "no_matching_txn"))
    return GateContext(
        conn=conn, client_id=client_id, bank_account_id=bank[0],
        period_start=period_start, period_end=period_end, receipts=receipts,
        expected_undocumented_qbo_ids=set(),  # live books vary; (d) checks shape
        storage=LocalFSStorage(),
        workdir=WORKDIR,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    load_dotenv()
    parser = argparse.ArgumentParser(description="Phase 4 gate")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--realm", help="live mode: synced sandbox realm id")
    mode.add_argument("--fixture", action="store_true",
                      help="fixture mode: destructive scratch-db run")
    parser.add_argument("--period", default=None, help="YYYY-MM (live mode)")
    args = parser.parse_args(argv)

    if args.fixture:
        url = os.environ.get("CAROLUS_TEST_DB")
        if not url:
            print("CAROLUS_TEST_DB is not set", file=sys.stderr)
            return 2
        if url == os.environ.get("DATABASE_URL"):
            print("REFUSING: CAROLUS_TEST_DB equals DATABASE_URL",
                  file=sys.stderr)
            return 2
        print("PHASE 4 GATE — fixture mode (scratch database)")
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute("DROP SCHEMA public CASCADE")
            admin.execute("CREATE SCHEMA public")
        migrate(url)
        with psycopg.connect(url) as conn:
            results = run_checks(seed_fixture_client(conn))
    else:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            print("DATABASE_URL is not set", file=sys.stderr)
            return 2
        print(f"PHASE 4 GATE — live mode (realm {args.realm})")
        with psycopg.connect(database_url) as conn:
            results = run_checks(live_context(conn, args.realm, args.period))

    print()
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        print(f"        {detail}")
    passed = sum(1 for _, ok, _ in results if ok)
    verdict = "PASS" if passed == len(results) else "FAIL"
    print(f"\nGATE: {verdict} ({passed}/{len(results)})")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
