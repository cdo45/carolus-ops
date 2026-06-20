"""The nightly routine: the deterministic backbone of the per-client loop.

Wires the already-built, separately-tested steps — incremental (CDC) sync,
the rules engine, flag projection, and the monthly close checklist — into
one recorded run per client. It only CALLS those steps; it owns none of
their logic. Email intake, bookkeeping auto-post, and the LLM brief are
later Phase 5 increments and deliberately absent here.

Runs under the OWNER connection with app-layer client_id scoping, exactly
like sync and the rules engine: both write journal_lines, a table the
least-privilege carolus_app role has no grant on, so the routine does NOT
SET ROLE. Structural RLS isolation is gated separately on the queue/flags
layer in the Phase 5 gate chunk.

The single live-QBO seam is `sync`, injected so the whole routine is
testable offline; its default is an incremental sync for the client.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from datetime import date
from typing import Any
from uuid import UUID

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from db.migrate import migrate
from routines.flag_queue import project_flags
from rules.close_checklist import evaluate_close, parse_period, persist_close
from rules.engine import run_rules
from sync.incremental import NoCursor, run_incremental_sync

# the sync seam: given a client id, pull that client's latest QBO activity.
SyncFn = Callable[[UUID], Any]


def _default_sync(conn: psycopg.Connection) -> SyncFn:
    """The production seam: an incremental (CDC) sync for the client.

    Incremental — not full_sync — is the production path. It raises NoCursor
    for a brand-new client that has never had an initial full_sync, which
    run_nightly surfaces as a clear 'needs initial full_sync' outcome rather
    than an opaque crash.
    """

    def sync(client_id: UUID) -> Any:
        row = conn.execute(
            "SELECT qbo_realm_id FROM clients WHERE id = %s", (client_id,)
        ).fetchone()
        realm_id = row[0] if row else None
        if not realm_id:
            raise NoCursor(
                f"client {client_id}: no QBO realm on record — run an initial"
                " full_sync before the nightly routine"
            )
        return run_incremental_sync(conn, client_id, realm_id)

    return sync


def _finish_run(
    conn: psycopg.Connection,
    run_id: UUID,
    *,
    status: str,
    actions: dict[str, Any],
) -> None:
    """Stamp a run terminal (succeeded/failed) with its actions summary."""
    conn.execute(
        "UPDATE runs SET finished_at = now(), status = %s, actions = %s"
        " WHERE id = %s",
        (status, Jsonb(actions), run_id),
    )
    conn.commit()


def run_nightly(
    database_url: str,
    client_id: UUID,
    *,
    sync: SyncFn | None = None,
) -> dict[str, Any]:
    """Run one client's nightly routine inside a recorded 'nightly' run.

    Mirrors run_rules' run lifecycle: open a 'nightly' run, drive the built
    steps (sync -> rules -> flag projection -> close checklist) on the owner
    connection (no SET ROLE), then finish the run succeeded/failed with a
    compact actions summary. Returns the summary.
    """
    migrate(database_url)  # schema currency before any work; idempotent

    with psycopg.connect(database_url) as conn:
        sync = sync or _default_sync(conn)

        run_row = conn.execute(
            "INSERT INTO runs (client_id, routine) VALUES (%s, 'nightly')"
            " RETURNING id",
            (client_id,),
        ).fetchone()
        assert run_row is not None
        run_id: UUID = run_row[0]
        conn.commit()  # the run is on the record before any step runs

        try:
            sync(client_id)  # the live-QBO seam; default = incremental sync
            rules = run_rules(conn, client_id)  # makes its own run, commits
            project_flags(conn, client_id)  # open flags -> open queue items
            period_start, period_end = parse_period(date.today().strftime("%Y-%m"))
            close = evaluate_close(conn, client_id, period_start, period_end)
            persist_close(conn, client_id, close)
            conn.commit()

            queued_row = conn.execute(
                "SELECT count(*) FROM review_queue WHERE client_id = %s"
                " AND kind = 'flag' AND status = 'open'",
                (client_id,),
            ).fetchone()
            assert queued_row is not None
            actions: dict[str, Any] = {
                "rules": rules["totals"],
                "queued": queued_row[0],
                "close": close.status,
            }
            _finish_run(conn, run_id, status="succeeded", actions=actions)
            return {"run_id": run_id, "status": "succeeded", **actions}
        except NoCursor as exc:
            # New client with no baseline: a clear operational outcome, not a
            # bug — record it and surface it, never crash opaquely.
            conn.rollback()
            _finish_run(
                conn,
                run_id,
                status="failed",
                actions={"error": "NoCursor", "needs": "initial full_sync"},
            )
            return {"run_id": run_id, "status": "needs_full_sync", "message": str(exc)}
        except Exception as exc:
            conn.rollback()
            _finish_run(
                conn, run_id, status="failed", actions={"error": type(exc).__name__}
            )
            raise


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Run the nightly routine for one client"
    )
    parser.add_argument("--realm", required=True, help="QBO realm id")
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT id, name FROM clients WHERE qbo_realm_id = %s", (args.realm,)
        ).fetchone()
        if row is None:
            print(f"no client for realm {args.realm}", file=sys.stderr)
            return 1
        client_id, name = row

    summary = run_nightly(database_url, client_id)
    print(f"nightly — {name}: {summary['status']}")
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
