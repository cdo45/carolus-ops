"""PHASE 4 GATE — checksum, bank rec, matching, idempotent intake. No LLM.

Two modes, same checks:

  uv run python -m tests.gate_phase4 --realm <realm_id> [--period YYYY-MM]
      Live mode: runs against DATABASE_URL for a client whose sandbox
      sync has already been done (sync first; the gate makes no QBO
      calls). Receipts/statements are synthesized from that client's
      canonical transactions.

  CAROLUS_TEST_DB=... uv run python -m tests.gate_phase4 --fixture
      Fixture mode: scratch-database run (refuses DATABASE_URL), seeded
      with the FakeQbo company + factory transactions — fully local,
      deterministic, CI-runnable.

SELF-SCOPING / RE-RUNNABILITY: the gate must pass on a DIRTY database
containing previous gate runs. Each run mints a nonce; fixture files
carry it in their names AND their bytes (so sha256 identities are new on
purpose where intended), factory entities/amounts are minted per run,
and EVERY assertion is scoped to artifacts created by THIS run (document
ids, this run's phantom statement for R040, this run's receipt set) —
never to client-wide counts or filenames.

Checks:
  (a) this run's four statements land validated/escalated with the exact
      reasons; the corrupted one parses NOTHING; none of the four sits in
      any other state
  (b) this run's clean statement recs tied to the penny; this run's
      phantom line raises exactly one R040 pointing at this run's
      phantom document
  (c) this run's six receipts: 4 matched (their txns backed),
      1 ambiguous_match, 1 no_matching_txn — exact terminal states
  (d) the request list contains this run's orphan receipt and this run's
      known undocumented transactions
  (e) re-ingesting this run's files resolves every one back to this
      run's document ids with zero new rows (sha256 proof)
"""

from __future__ import annotations

import os
import secrets
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
    nonce: str


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
    conn, client_id, nonce = ctx.conn, ctx.client_id, ctx.nonce
    ctx.workdir.mkdir(parents=True, exist_ok=True)

    # ---- build the four statements from canonical activity; the nonce
    # goes into names AND bytes so this run's documents are its own
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
              "beginning": BEGINNING,
              "bank_name": f"First Interstate Bank (gate run {nonce})"}
    files = {
        "clean": statement_pdf(ctx.workdir / f"statement-clean-{nonce}.pdf",
                               ending=clean_end, lines=lines,
                               stated_count=len(lines), **common),
        "corrupted": statement_pdf(
            ctx.workdir / f"statement-corrupted-{nonce}.pdf",
            ending=clean_end + Decimal("100.00"),
            lines=lines, stated_count=len(lines), **common),
        "phantom": statement_pdf(
            ctx.workdir / f"statement-phantom-{nonce}.pdf",
            ending=ending_balance(BEGINNING, phantom_lines),
            lines=phantom_lines, stated_count=len(phantom_lines), **common),
        "image": image_only_pdf(ctx.workdir / f"statement-image-{nonce}.pdf",
                                salt=int(nonce, 16)),
    }

    # (a) terminal states — judged over THIS run's four documents only
    outcomes = {name: _pipeline_statement(ctx, path)
                for name, path in files.items()}
    statement_doc_ids = [outcome[0] for outcome in outcomes.values()]
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
        WHERE id = ANY(%s) AND status NOT IN ('validated', 'escalated')
        """,
        (statement_doc_ids,),
    ).fetchone()
    assert stray is not None
    results.append((
        "(a) statement terminals exact; corrupted parsed nothing",
        terminal_ok and nothing_parsed and stray[0] == 0,
        ", ".join(f"{k}={v[1]}/{v[2] or '-'}" for k, v in outcomes.items()),
    ))

    # (b) rec: clean ties; THIS run's phantom raises R040 on THIS run's doc
    clean_rec = rec(conn, client_id, ctx.bank_account_id, outcomes["clean"][0])
    phantom_doc = outcomes["phantom"][0]
    phantom_rec = rec(conn, client_id, ctx.bank_account_id, phantom_doc)
    r040 = conn.execute(
        """
        SELECT f.source_ref,
               EXISTS (SELECT 1 FROM documents d
                       WHERE d.id = f.source_ref::uuid
                         AND d.client_id = f.client_id) AS resolves
        FROM flags f
        WHERE f.client_id = %s AND f.rule_code = 'R040' AND f.status = 'open'
          AND f.source_ref = %s
        """,
        (client_id, str(phantom_doc)),
    ).fetchall()
    results.append((
        "(b) clean rec ties to the penny; phantom raises R040 w/ valid ref",
        (clean_rec.tied and clean_rec.unmatched_statement_lines == []
         and not phantom_rec.tied
         and len(phantom_rec.unmatched_statement_lines) == 1
         and len(r040) == 1
         and r040[0][1] is True),
        f"clean tied={clean_rec.tied} matched={clean_rec.matched_count};"
        f" phantom unmatched={len(phantom_rec.unmatched_statement_lines)},"
        f" R040 flags on this run's doc={len(r040)}",
    ))

    # (c) receipts: ingest -> classify -> extract -> match — terminal
    # states judged per THIS run's document ids
    receipt_terminal: dict[str, tuple[str, str | None]] = {}
    receipt_docs: list[tuple[Path, UUID]] = []
    for plan in ctx.receipts:
        path = receipt_pdf(ctx.workdir / plan.filename, merchant=plan.merchant,
                           txn_date=plan.txn_date, total=plan.total,
                           footer=f"GATE RUN {nonce}")
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
    receipt_doc_ids = [doc_id for _, doc_id in receipt_docs]
    backed = conn.execute(
        """
        SELECT count(*) FROM documents d
        JOIN transactions t ON t.id = d.matched_txn
        WHERE d.id = ANY(%s) AND t.doc_status = 'backed'
        """,
        (receipt_doc_ids,),
    ).fetchone()
    assert backed is not None
    expected_matched = sum(1 for p in ctx.receipts if p.expect == "matched")
    results.append((
        "(c) receipt terminals exact; matched txns backed",
        receipts_ok and backed[0] == expected_matched,
        ", ".join(f"{name}={status}/{reason or '-'}"
                  for name, (status, reason) in receipt_terminal.items()),
    ))

    # (d) request list contains THIS run's orphan + undocumented txns
    requests = generate_request_list(conn, client_id, ctx.period_start,
                                     ctx.period_end)
    listed_qbo_ids = {txn["qbo_id"] for txn in requests.undocumented_txns}
    orphan_files = {plan.filename for plan in ctx.receipts
                    if plan.expect == "no_matching_txn"}
    listed_files = {doc["filename"] for doc in requests.unmatched_documents}
    results.append((
        "(d) request list: this run's orphan receipt + undocumented txns",
        orphan_files <= listed_files
        and ctx.expected_undocumented_qbo_ids <= listed_qbo_ids
        and bool(requests.undocumented_txns),
        f"this run's expectations ⊆ list: undocumented"
        f"={sorted(ctx.expected_undocumented_qbo_ids) or 'shape-only'},"
        f" orphans={sorted(orphan_files)}",
    ))

    # (e) duplicate re-ingest of THIS run's files: every one resolves back
    # to this run's document id, zero new rows
    this_run_files = {path: doc_id for name, path in files.items()
                      for doc_id in [outcomes[name][0]]}
    this_run_files.update(dict(receipt_docs))
    before = conn.execute(
        "SELECT count(*) FROM documents WHERE client_id = %s", (client_id,)
    ).fetchone()
    duplicates = {
        path: ingest(conn, client_id, path, "gate-again", storage=ctx.storage)
        for path in this_run_files
    }
    after = conn.execute(
        "SELECT count(*) FROM documents WHERE client_id = %s", (client_id,)
    ).fetchone()
    mapped_back = all(
        result.created is False
        and result.document_id == this_run_files[path]
        for path, result in duplicates.items()
    )
    results.append((
        "(e) duplicate re-ingest resolves to this run's ids, zero new rows",
        mapped_back and before == after,
        f"{len(this_run_files)} files re-ingested onto their own ids,"
        f" documents {before[0]} -> {after[0]}",  # type: ignore[index]
    ))
    return results


# ------------------------------------------------------------ fixture mode


def unique_amount(
    conn: psycopg.Connection, client_id: UUID, base: Decimal
) -> Decimal:
    """Bump until no spend transaction of this client carries the amount —
    keeps receipt targets unique across ALL prior gate generations."""
    amount = base.quantize(Decimal("0.01"))
    while conn.execute(
        "SELECT 1 FROM transactions WHERE client_id = %s AND amount = %s"
        " AND txn_type IN ('Purchase', 'Bill', 'BillPayment')",
        (client_id, amount),
    ).fetchone() is not None:
        amount += Decimal("0.97")
    return amount


def seed_fixture_client(conn: psycopg.Connection, nonce: str) -> GateContext:
    from sync.full_sync import run_full_sync
    from tests.factories import balanced_purchase, make_entity
    from tests.qbo_fixtures import FakeQbo

    existing = conn.execute(
        "SELECT id FROM clients WHERE qbo_realm_id = 'gate-p4'"
    ).fetchone()
    if existing is not None:
        client_id: UUID = existing[0]  # dirty DB: reuse, never reset
    else:
        row = conn.execute(
            "INSERT INTO clients (name, qbo_realm_id) VALUES"
            " ('Gate Four Constructors', 'gate-p4') RETURNING id"
        ).fetchone()
        assert row is not None
        client_id = row[0]
        conn.commit()
    run_full_sync(conn, client_id, "gate-p4", qbo=FakeQbo())  # idempotent

    def account(qbo_id: str) -> UUID:
        found = conn.execute(
            "SELECT id FROM accounts WHERE client_id = %s AND qbo_id = %s",
            (client_id, qbo_id),
        ).fetchone()
        assert found is not None
        return found[0]

    bank, cogs = account("1"), account("40")
    vendor = make_entity(conn, client_id, kind="vendor",
                         name=f"Gate Supply {nonce}",
                         qbo_id=f"V{nonce}")
    salt = Decimal(int(nonce, 16) % 89) / 100
    sequence = iter(range(1, 100))

    def spend(amount: Decimal, day: int) -> UUID:
        return balanced_purchase(
            conn, client_id, amount=str(amount),
            txn_date=date(2026, 5, day), entity_id=vendor,
            qbo_id=f"G{nonce}-{next(sequence)}",
            bank=bank, expense=cogs,
        )

    clean_targets: list[tuple[Decimal, int]] = []
    for base, day in ((Decimal("61.20"), 4), (Decimal("77.35"), 8),
                      (Decimal("142.10"), 15), (Decimal("53.80"), 20)):
        amount = unique_amount(conn, client_id, base + salt)
        spend(amount, day)
        conn.commit()
        clean_targets.append((amount, day))

    ambiguous_amount = unique_amount(conn, client_id, Decimal("66.10") + salt)
    spend(ambiguous_amount, 18)
    conn.commit()
    spend(ambiguous_amount, 19)
    request_amount = unique_amount(conn, client_id, Decimal("150.00") + salt)
    request_seed = spend(request_amount, 22)  # known undocumented, >= $75
    conn.commit()
    orphan_amount = unique_amount(conn, client_id, Decimal("123.99") + salt)
    request_seed_qbo = conn.execute(
        "SELECT qbo_id FROM transactions WHERE id = %s", (request_seed,)
    ).fetchone()
    assert request_seed_qbo is not None

    receipts = [
        ReceiptPlan(f"receipt-{index}-{nonce}.pdf", f"GATE SUPPLY {nonce}",
                    date(2026, 5, day), amount, "matched")
        for index, (amount, day) in enumerate(clean_targets, start=1)
    ]
    receipts.append(ReceiptPlan(f"receipt-5-{nonce}.pdf",
                                f"GATE SUPPLY {nonce}", date(2026, 5, 18),
                                ambiguous_amount, "ambiguous_match"))
    receipts.append(ReceiptPlan(f"receipt-6-{nonce}.pdf", "ROADSIDE DINER",
                                date(2026, 5, 9), orphan_amount,
                                "no_matching_txn"))
    return GateContext(
        conn=conn, client_id=client_id, bank_account_id=bank,
        period_start=date(2026, 5, 1), period_end=date(2026, 5, 31),
        receipts=receipts,
        # fixture company spend that stays unbacked (5001/2001) + THIS
        # run's seeded purchase
        expected_undocumented_qbo_ids={"5001", "2001", request_seed_qbo[0]},
        storage=LocalFSStorage(root=WORKDIR / "docstore"),
        workdir=WORKDIR / nonce,
        nonce=nonce,
    )


# ------------------------------------------------------------ live mode


def live_context(
    conn: psycopg.Connection, realm: str, period: str | None, nonce: str
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

    # Clean-match targets must be unique UNDER THE MATCHER'S OWN CRITERIA
    # (amount exact, date ±5d, vendor trigram narrowing, receipt candidate
    # types) — amount-uniqueness alone is not enough: seeded duplicate
    # twins and Bill/BillPayment same-amount pairs both produce legitimate
    # ambiguity. Verify each target by querying with the matcher itself.
    from docpipe.matching import _candidates

    pool = conn.execute(
        """
        SELECT t.id, t.txn_date, t.amount, COALESCE(e.name, 'VENDOR')
        FROM transactions t
        LEFT JOIN entities e ON e.id = t.entity_id
        WHERE t.client_id = %s AND t.txn_type IN ('Purchase', 'Bill')
          AND t.txn_date BETWEEN %s AND %s AND t.amount > 0
          AND t.qbo_deleted_at IS NULL
        ORDER BY t.amount DESC, t.txn_date, t.qbo_id
        """,
        (client_id, period_start, period_end),
    ).fetchall()

    receipts: list[ReceiptPlan] = []
    for txn_id, txn_date, amount, vendor in pool:
        if len(receipts) == 4:
            break
        candidates = _candidates(conn, client_id, "receipt", amount,
                                 txn_date, vendor)
        if len(candidates) == 1 and candidates[0]["id"] == txn_id:
            receipts.append(ReceiptPlan(
                f"receipt-{len(receipts) + 1}-{nonce}.pdf", vendor, txn_date,
                amount, "matched",
            ))
    if len(receipts) < 4:
        raise SystemExit(
            f"only {len(receipts)} transaction(s) in"
            f" {period_start:%Y-%m} have a UNIQUE match signature"
            " (amount exact, date ±5d, vendor) — the gate needs 4"
            " clean-match receipt targets. Pick a different month with"
            " --period YYYY-MM, or add distinct-amount spend to the sandbox."
        )

    # The deliberately-ambiguous receipt comes FROM the seeded R010 twin
    # pair (same vendor+amount days apart) — the perfect ambiguity fixture.
    twin = conn.execute(
        """
        SELECT DISTINCT t.txn_date, t.amount, COALESCE(e.name, 'VENDOR')
        FROM qbo_raw r
        JOIN transactions t ON t.client_id = r.client_id
                           AND t.qbo_id = r.qbo_id AND t.txn_type = r.entity_type
        LEFT JOIN entities e ON e.id = t.entity_id
        WHERE r.client_id = %s
          AND r.payload ->> 'PrivateNote' LIKE 'CAROLUS-SEED-1 %%'
          AND t.qbo_deleted_at IS NULL
        ORDER BY t.txn_date DESC LIMIT 1
        """,
        (client_id,),
    ).fetchone()
    if twin is None:  # seeds absent: any matcher-verified ambiguous signature
        for txn_id, txn_date, amount, vendor in pool:
            if len(_candidates(conn, client_id, "receipt", amount, txn_date,
                               vendor)) >= 2:
                twin = (txn_date, amount, vendor)
                break
    if twin is None:
        raise SystemExit(
            "no ambiguous-match signature found (need two same-vendor,"
            " same-amount txns within the window) — run tests/seed_errors.py"
            " (the R010 pair provides this) or pick another --period"
        )
    twin_date, twin_amount, twin_vendor = twin
    if len(_candidates(conn, client_id, "receipt", twin_amount, twin_date,
                       twin_vendor)) < 2:
        raise SystemExit(
            "seeded twin no longer yields >= 2 match candidates — reseed"
            " with tests/seed_errors.py"
        )
    receipts.append(ReceiptPlan(f"receipt-5-{nonce}.pdf", twin_vendor,
                                twin_date, twin_amount, "ambiguous_match"))
    receipts.append(ReceiptPlan(f"receipt-6-{nonce}.pdf", "ROADSIDE DINER",
                                period_start + timedelta(days=8),
                                Decimal("9123.47"), "no_matching_txn"))
    return GateContext(
        conn=conn, client_id=client_id, bank_account_id=bank[0],
        period_start=period_start, period_end=period_end, receipts=receipts,
        expected_undocumented_qbo_ids=set(),  # live books vary; (d) checks shape
        storage=LocalFSStorage(),
        workdir=WORKDIR / nonce,
        nonce=nonce,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    load_dotenv()
    parser = argparse.ArgumentParser(description="Phase 4 gate")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--realm", help="live mode: synced sandbox realm id")
    mode.add_argument("--fixture", action="store_true",
                      help="fixture mode: scratch-db run (re-runnable)")
    parser.add_argument("--period", default=None, help="YYYY-MM (live mode)")
    args = parser.parse_args(argv)

    nonce = secrets.token_hex(4)
    if args.fixture:
        url = os.environ.get("CAROLUS_TEST_DB")
        if not url:
            print("CAROLUS_TEST_DB is not set", file=sys.stderr)
            return 2
        if url == os.environ.get("DATABASE_URL"):
            print("REFUSING: CAROLUS_TEST_DB equals DATABASE_URL",
                  file=sys.stderr)
            return 2
        print(f"PHASE 4 GATE — fixture mode (scratch database, run {nonce})")
        migrate(url)  # idempotent; dirty databases are EXPECTED and kept
        with psycopg.connect(url) as conn:
            results = run_checks(seed_fixture_client(conn, nonce))
    else:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            print("DATABASE_URL is not set", file=sys.stderr)
            return 2
        print(f"PHASE 4 GATE — live mode (realm {args.realm}, run {nonce})")
        with psycopg.connect(database_url) as conn:
            results = run_checks(
                live_context(conn, args.realm, args.period, nonce)
            )

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
