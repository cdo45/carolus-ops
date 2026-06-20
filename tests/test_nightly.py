"""The nightly routine wires the built steps into one recorded run.

DB-backed (the conn fixture seeds and asserts; scratch_db_url is the
database_url run_nightly opens its own owner connection on). The live-QBO
sync is the only injected seam — a fake keeps the whole routine offline, so
these tests exercise the run lifecycle, flag projection, idempotency, and
the failure path without touching QuickBooks.
"""

from __future__ import annotations

from uuid import UUID

import psycopg
import pytest

from routines.nightly import run_nightly
from routines.queue import open_items
from tests.conftest import make_client


def _seed_open_sync_flag(conn: psycopg.Connection, client_id: UUID) -> None:
    """One open flag the rules engine never touches (transform_warning is
    written by sync, outside the engine's registry), so it survives a nightly
    run and is observably projected into the queue."""
    conn.execute(
        "INSERT INTO flags (client_id, rule_code, severity, status, source_type,"
        " source_ref, detail) VALUES (%s, 'transform_warning', 'warn', 'open',"
        " 'transaction', 'qbo:Invoice:1', 'unbalanced lines: 5.00 != 4.00')",
        (client_id,),
    )
    conn.commit()


def test_nightly_succeeds_and_projects_open_flags(
    conn: psycopg.Connection, scratch_db_url: str
) -> None:
    client_id = make_client(conn)
    _seed_open_sync_flag(conn, client_id)

    calls: list[UUID] = []

    def fake_sync(cid: UUID) -> None:
        calls.append(cid)

    summary = run_nightly(scratch_db_url, client_id, sync=fake_sync)

    assert calls == [client_id], "the injected sync seam ran once for this client"
    assert summary["status"] == "succeeded"
    assert summary["queued"] == 1

    run = conn.execute(
        "SELECT status, actions FROM runs WHERE client_id = %s AND routine = 'nightly'",
        (client_id,),
    ).fetchone()
    assert run is not None
    status, actions = run
    assert status == "succeeded", "the run row is stamped succeeded"
    assert set(actions) == {"rules", "queued", "close"}, "compact actions summary"
    assert actions["queued"] == 1

    flag_items = [item for item in open_items(conn, client_id) if item["kind"] == "flag"]
    assert [item["title"] for item in flag_items] == ["transform_warning"], (
        "the surviving sync flag is projected into the queue"
    )


def test_nightly_is_idempotent(
    conn: psycopg.Connection, scratch_db_url: str
) -> None:
    client_id = make_client(conn)
    _seed_open_sync_flag(conn, client_id)

    def fake_sync(cid: UUID) -> None:  # no new data on either pass
        return None

    run_nightly(scratch_db_url, client_id, sync=fake_sync)
    run_nightly(scratch_db_url, client_id, sync=fake_sync)

    queue_items = conn.execute(
        "SELECT count(*) FROM review_queue WHERE client_id = %s AND kind = 'flag'"
        " AND status = 'open'",
        (client_id,),
    ).fetchone()
    assert queue_items == (1,), "the second run refreshes in place — no duplicate item"

    runs = conn.execute(
        "SELECT count(*) FROM runs WHERE client_id = %s AND routine = 'nightly'"
        " AND status = 'succeeded'",
        (client_id,),
    ).fetchone()
    assert runs == (2,), "both nightly runs are recorded"


def test_nightly_failure_marks_run_failed_and_reraises(
    conn: psycopg.Connection, scratch_db_url: str
) -> None:
    client_id = make_client(conn)

    def boom(cid: UUID) -> None:
        raise RuntimeError("sync exploded")

    with pytest.raises(RuntimeError, match="sync exploded"):
        run_nightly(scratch_db_url, client_id, sync=boom)

    run = conn.execute(
        "SELECT status, actions FROM runs WHERE client_id = %s AND routine = 'nightly'",
        (client_id,),
    ).fetchone()
    assert run == ("failed", {"error": "RuntimeError"}), (
        "the run is stamped failed with the error type, and run_nightly re-raises"
    )
