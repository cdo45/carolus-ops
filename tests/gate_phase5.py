"""PHASE 5 GATE — the integrated nightly loop, LIVE against the QBO SANDBOX.

NOT part of CI (a script, not a test_*; pytest does not collect it). Offline
integration of the loop is covered by tests/test_nightly.py; this is the live
end-to-end certification, in the style of gate_phase2 / gate_phase4.

It seeds ONE deterministic violation — a round-number $5,000 JournalEntry (the
R012 signature, no history/window dependence) — via the real seeder + a
QboClient, then calls the REAL run_nightly, which under the carolus_agent wall
syncs (incremental CDC) -> runs rules -> projects flags into the queue ->
closes. Four named checks prove the integrated loop; PASS only if all four pass.

SELF-SCOPING (like gate_phase4): every run mints a nonce, tags the seeded JE's
PrivateNote with it, and scopes EVERY assertion to THIS run's artifact (its
qbo_id -> its canonical txn -> its R012 flag -> its queue item) — never
client-wide counts. It must pass on the dirty sandbox (hundreds of pre-existing
flags + queue items).

Note on provenance: R012 is an ENGINE rule, so its flag's source_ref is the
canonical transactions.id (the engine convention), with the JE's qbo_id carried
in detail — distinct from sync flags' 'qbo:<Type>:<Id>'. The gate resolves the
seeded JE by qbo_id and scopes to that canonical row.

Prereq: the sandbox client has had an initial full_sync — run_nightly's default
sync is incremental (CDC), which needs a cursor to pull the freshly-seeded JE.
REFUSES unless QBO_ENVIRONMENT=sandbox (the gate writes a real JournalEntry).

Usage:
  uv run python -m tests.gate_phase5 --realm <sandbox_realm_id>
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from datetime import date, timedelta
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from db.migrate import migrate  # noqa: E402
from db.tenant import agent_connection  # noqa: E402
from routines.nightly import run_nightly  # noqa: E402
from routines.queue import triage  # noqa: E402
from sync.qbo_client import QboClient  # noqa: E402
from tests.seed_errors import Seeder  # noqa: E402

TODAY = date.today()


def seed_round_je(
    conn: psycopg.Connection, client_id: UUID, realm: str, nonce: str
) -> str:
    """Plant one round-number $5,000 JournalEntry (R012) via the API.

    The nonce rides in the PrivateNote for human traceability; the qbo_id is
    returned so every assertion can scope to THIS run's artifact.
    """
    seeder = Seeder(QboClient(conn, client_id, realm))
    seeder.step(f"gate_phase5 R012 round JE {nonce}")
    bank = seeder.first_bank_account()
    office = seeder.account("CAROLUS Seed Office", "Expense")
    je_id = seeder.journal_entry(
        amount=5000.00, txn_date=TODAY - timedelta(days=3),
        debit=office, credit=bank, note=f"CAROLUS-SEED-P5-{nonce} round JE gate",
    )
    conn.commit()  # flush any token refresh before run_nightly's own connection
    return je_id


def run_checks(
    conn: psycopg.Connection, database_url: str, client_id: UUID,
    je_id: str, nonce: str,
) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    # ---- first nightly: sync (CDC pulls the seeded JE) -> rules (R012) ->
    # project_flags -> close, all on the carolus_agent connection.
    summary1 = run_nightly(database_url, client_id)

    txn = conn.execute(
        "SELECT id FROM transactions WHERE client_id = %s AND qbo_id = %s"
        " AND txn_type = 'JournalEntry'",
        (client_id, je_id),
    ).fetchone()
    txn_id = txn[0] if txn else None

    # the full chain, scoped to the seeded JE: queue item -> R012 flag whose
    # source_ref is the JE's canonical txn (which must exist — provenance).
    row = None
    if txn_id is not None:
        row = conn.execute(
            """
            SELECT rq.id, f.id
            FROM review_queue rq
            JOIN flags f ON f.id::text = rq.source_ref
            WHERE rq.client_id = %s AND rq.kind = 'flag' AND rq.status = 'open'
              AND f.rule_code = 'R012' AND f.status = 'open'
              AND f.source_type = 'transaction' AND f.source_ref = %s
              AND EXISTS (SELECT 1 FROM transactions t
                          WHERE t.id = f.source_ref::uuid
                            AND t.client_id = f.client_id)
            """,
            (client_id, str(txn_id)),
        ).fetchone()
    item_id = row[0] if row else None
    flag_id = row[1] if row else None

    # 1. PROJECTION + PROVENANCE
    results.append((
        "1. projection+provenance: the seeded JE's R012 flag is in the queue,"
        " its source_ref resolving to a real canonical transaction",
        row is not None,
        f"nightly={summary1.get('status')}; je qbo_id={je_id} -> txn={txn_id};"
        f" queue item={item_id}, flag={flag_id}",
    ))

    # 2. TENANT ISOLATION under carolus_agent (self-scoped probe)
    throwaway_client: UUID | None = None
    try:
        created = conn.execute(
            "INSERT INTO clients (name, qbo_realm_id) VALUES (%s, %s) RETURNING id",
            (f"Gate P5 Throwaway {nonce}", f"gate-p5-throwaway-{nonce}"),
        ).fetchone()
        assert created is not None
        throwaway_client = created[0]
        conn.execute(
            "INSERT INTO flags (client_id, rule_code, severity, status,"
            " source_type, source_ref) VALUES (%s, 'R000', 'warn', 'open',"
            " 'transaction', 'throwaway')",
            (throwaway_client,),
        )
        conn.commit()

        with agent_connection(database_url, client_id) as agent:
            other = agent.execute(
                "SELECT count(*) FROM flags WHERE client_id = %s",
                (throwaway_client,),
            ).fetchone()
            mine = (
                agent.execute(
                    "SELECT count(*) FROM flags WHERE id = %s", (flag_id,)
                ).fetchone()
                if flag_id is not None else (0,)
            )
        isolation_ok = other == (0,) and mine == (1,)
        results.append((
            "2. tenant isolation: the agent scoped to the sandbox sees zero of a"
            " throwaway client's flags, and sees its own R012 flag",
            isolation_ok,
            f"throwaway flags visible to agent={other[0]} (want 0);"
            f" seeded flag visible={mine[0]} (want 1)",
        ))
    finally:
        if throwaway_client is not None:
            conn.execute("DELETE FROM flags WHERE client_id = %s", (throwaway_client,))
            conn.execute("DELETE FROM clients WHERE id = %s", (throwaway_client,))
            conn.commit()

    # 3. IDEMPOTENT: a second nightly is a no-op for this artifact
    summary2 = run_nightly(database_url, client_id)
    if txn_id is not None and flag_id is not None:
        flags_n = conn.execute(
            "SELECT count(*) FROM flags WHERE client_id = %s AND rule_code = 'R012'"
            " AND source_ref = %s",
            (client_id, str(txn_id)),
        ).fetchone()
        items_n = conn.execute(
            "SELECT count(*) FROM review_queue WHERE client_id = %s AND kind = 'flag'"
            " AND source_ref = %s AND status = 'open'",
            (client_id, str(flag_id)),
        ).fetchone()
        assert flags_n is not None and items_n is not None
        idempotent_ok = flags_n == (1,) and items_n == (1,)
        detail3 = (f"nightly={summary2.get('status')}; R012 flags for JE={flags_n[0]},"
                   f" open queue items={items_n[0]} (want 1 / 1)")
    else:
        idempotent_ok = False
        detail3 = "no seeded flag/item resolved in check 1"
    results.append((
        "3. idempotent: a second nightly adds no duplicate flag or queue item",
        idempotent_ok, detail3,
    ))

    # 4. DISMISS SURVIVES: triage-dismiss, then a third nightly must not reopen
    if item_id is not None and flag_id is not None:
        triage(conn, item_id, action="dismiss", by=f"gate-p5-{nonce}")
        conn.commit()
        summary3 = run_nightly(database_url, client_id)
        status = conn.execute(
            "SELECT status FROM flags WHERE id = %s", (flag_id,)
        ).fetchone()
        reopened = conn.execute(
            "SELECT count(*) FROM review_queue WHERE client_id = %s AND kind = 'flag'"
            " AND source_ref = %s AND status = 'open'",
            (client_id, str(flag_id)),
        ).fetchone()
        assert status is not None and reopened is not None
        dismiss_ok = status == ("dismissed",) and reopened == (0,)
        detail4 = (f"nightly={summary3.get('status')}; flag status={status[0]}"
                   f" (want dismissed), open items after={reopened[0]} (want 0)")
    else:
        dismiss_ok = False
        detail4 = "no seeded flag/item resolved in check 1"
    results.append((
        "4. dismiss survives: a dismissed flag stays dismissed and re-surfaces no item",
        dismiss_ok, detail4,
    ))

    return results


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Phase 5 gate (live sandbox)")
    parser.add_argument("--realm", required=True, help="connected sandbox realm id")
    args = parser.parse_args(argv)

    if os.environ.get("QBO_ENVIRONMENT") != "sandbox":
        print("REFUSING: QBO_ENVIRONMENT must be 'sandbox' (the gate seeds a real"
              " JournalEntry via the API)", file=sys.stderr)
        return 2
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    nonce = secrets.token_hex(4)
    migrate(database_url)  # schema currency first (owner; idempotent)
    print(f"PHASE 5 GATE — realm {args.realm}, run {nonce}")

    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT id FROM clients WHERE qbo_realm_id = %s", (args.realm,)
        ).fetchone()
        if row is None:
            print(f"no client for realm {args.realm} — run sync.connect first",
                  file=sys.stderr)
            return 2
        client_id: UUID = row[0]

        print("  seeding one round-number $5,000 JournalEntry (R012) ...")
        je_id = seed_round_je(conn, client_id, args.realm, nonce)
        print(f"  seeded JE qbo_id={je_id}; running the nightly loop ...")
        results = run_checks(conn, database_url, client_id, je_id, nonce)

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
