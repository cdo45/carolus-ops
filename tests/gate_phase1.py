"""PHASE 1 GATE — runs against the LIVE QBO SANDBOX. NOT part of CI.

(Pytest ignores this file by name; the same properties are CI-tested
against fixtures in test_full_sync_db.py and test_incremental.py. This
script proves them against the real thing before the phase may close.)

Checks:
  (a) idempotency  — full sync twice; the second run must write ZERO
      canonical rows (per-row id+xmin snapshots must be identical)
  (b) token self-recovery — corrupt the stored access token ciphertext,
      then get_valid_access_token() must transparently refresh
  (c) journal integrity — every transaction's lines net to zero
      (debits = credits) OR the transaction carries an open
      transform_warning flag; nothing silently wrong

Prereqs:
  .env filled (DATABASE_URL, QBO_*, APP_ENCRYPTION_KEY), schema migrated,
  and a connected sandbox company:  uv run python -m sync.connect

Usage:
  uv run python -m tests.gate_phase1 --realm <sandbox_realm_id>

Token values are never printed.
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from sync import crypto, tokens  # noqa: E402
from sync.full_sync import run_full_sync  # noqa: E402

CANONICAL_TABLES = (
    "clients", "accounts", "entities", "jobs", "transactions",
    "journal_lines", "facts", "flags", "kpi_values", "vendor_patterns",
    "documents", "emails",
)

CheckResult = tuple[bool, str]


def snapshot(conn: psycopg.Connection) -> dict[str, set[tuple[str, str]]]:
    """Per-table set of (id, xmin): catches inserts, deletes, AND updates."""
    return {
        table: set(
            conn.execute(  # noqa: S608 - fixed table list above
                f"SELECT id::text, xmin::text FROM {table}"
            ).fetchall()
        )
        for table in CANONICAL_TABLES
    }


def check_idempotency(
    conn: psycopg.Connection, client_id: UUID, realm_id: str
) -> CheckResult:
    first = run_full_sync(conn, client_id, realm_id)
    fetched = sum(first["fetched"].values())
    before = snapshot(conn)

    second = run_full_sync(conn, client_id, realm_id)

    written = sum(second["written"].values()) + second["flags_created"]
    drifted = sorted(
        table for table, rows in snapshot(conn).items() if rows != before[table]
    )
    if written or drifted:
        return False, (
            f"second run wrote {written} rows; drifted tables: {drifted or 'none'}"
        )
    return True, (
        f"second full sync of {fetched} fetched payloads wrote 0 canonical rows"
        " (snapshots identical)"
    )


def check_token_recovery(conn: psycopg.Connection, client_id: UUID) -> CheckResult:
    # corrupt the at-rest ciphertext and push expiry out so ONLY the
    # corruption (not expiry) can force the refresh path
    conn.execute(
        """
        UPDATE sync_connections
        SET access_token_enc = 'corrupted-by-gate',
            token_expires_at = now() + interval '1 hour'
        WHERE client_id = %s
        """,
        (client_id,),
    )
    conn.commit()

    token = tokens.get_valid_access_token(conn, client_id)

    row = conn.execute(
        "SELECT status, access_token_enc FROM sync_connections WHERE client_id = %s",
        (client_id,),
    ).fetchone()
    assert row is not None
    status, stored_enc = row
    if status != "active":
        return False, f"connection status is {status!r} after recovery"
    if stored_enc == "corrupted-by-gate":
        return False, "refreshed token was not persisted"
    if crypto.decrypt(stored_enc) != token:
        return False, "persisted token does not match the returned token"
    return True, "corrupted access token transparently refreshed and re-persisted"


def check_journal_integrity(
    conn: psycopg.Connection, client_id: UUID
) -> CheckResult:
    rows = conn.execute(
        """
        SELECT t.txn_type, t.qbo_id,
               COALESCE(SUM(CASE WHEN jl.posting_type = 'debit'
                                 THEN jl.amount ELSE -jl.amount END), 0) AS net,
               EXISTS (
                   SELECT 1 FROM flags f
                   WHERE f.client_id = t.client_id
                     AND f.rule_code = 'transform_warning'
                     AND f.source_ref = 'qbo:' || t.txn_type || ':' || t.qbo_id
                     AND f.status = 'open'
               ) AS flagged
        FROM transactions t
        LEFT JOIN journal_lines jl ON jl.transaction_id = t.id
        WHERE t.client_id = %s
        GROUP BY t.id, t.txn_type, t.qbo_id, t.client_id
        """,
        (client_id,),
    ).fetchall()
    if not rows:
        return False, "no transactions synced — gate needs a populated sandbox"
    balanced = sum(1 for r in rows if r[2] == Decimal("0"))
    flagged = sum(1 for r in rows if r[2] != Decimal("0") and r[3])
    violations = [(r[0], r[1], str(r[2])) for r in rows if r[2] != 0 and not r[3]]
    if violations:
        return False, (
            f"{len(violations)} transaction(s) unbalanced AND unflagged:"
            f" {violations[:10]}"
        )
    return True, (
        f"{len(rows)} transactions — {balanced} net zero, {flagged} flagged,"
        " 0 silent violations"
    )


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Phase 1 gate (live sandbox)")
    parser.add_argument("--realm", required=True, help="connected sandbox realm id")
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT id FROM clients WHERE qbo_realm_id = %s", (args.realm,)
        ).fetchone()
        if row is None:
            print(
                f"no client for realm {args.realm} — run `uv run python -m"
                " sync.connect` first",
                file=sys.stderr,
            )
            return 2
        client_id: UUID = row[0]

        print(f"PHASE 1 GATE — realm {args.realm}")
        results: list[tuple[str, bool, str]] = []
        checks = (
            ("idempotency", lambda: check_idempotency(conn, client_id, args.realm)),
            ("token self-recovery", lambda: check_token_recovery(conn, client_id)),
            ("journal integrity", lambda: check_journal_integrity(conn, client_id)),
        )
        for name, check in checks:
            try:
                ok, detail = check()
            except Exception as exc:  # a crashed check is a failed gate
                ok, detail = False, f"crashed: {type(exc).__name__}: {exc}"
            results.append((name, ok, detail))
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")

        passed = sum(1 for _, ok, _ in results if ok)
        verdict = "PASS" if passed == len(results) else "FAIL"
        print(f"GATE: {verdict} ({passed}/{len(results)})")
        return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
