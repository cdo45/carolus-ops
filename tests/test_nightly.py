"""The nightly routine wires the built steps into one recorded run, with the
per-client data steps confined to the tenant by RLS.

DB-backed (the conn fixture seeds and asserts as the owner; scratch_db_url is
the database_url run_nightly opens its OWN owner + carolus_agent connections
on). The live-QBO sync is the only injected seam — a fake keeps the whole
routine offline. Beyond the run lifecycle, flag projection, idempotency, and
the failure path, these tests prove the wall holds THROUGH the routine: a run
for A leaves B untouched, and the routine's own agent connection physically
cannot reach another tenant.
"""

from __future__ import annotations

from uuid import UUID

import psycopg
import pytest

from db.tenant import agent_connection
from routines.nightly import run_nightly
from routines.queue import open_items
from tests.conftest import make_client
from tests.factories import make_account, make_txn


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


def _seed_client_with_work(conn: psycopg.Connection, *, realm: str) -> UUID:
    """A client with routine-touched data: a transaction (+ journal lines) and
    one open sync-owned flag the routine projects into the queue."""
    client_id = make_client(conn, realm)
    account = make_account(conn, client_id, name="Checking", acct_type="Bank")
    make_txn(
        conn, client_id, txn_type="Deposit", amount="100.00",
        lines=[{"account": account, "amount": "100.00", "posting": "debit"},
               {"account": account, "amount": "100.00", "posting": "credit"}],
    )
    _seed_open_sync_flag(conn, client_id)  # commits
    return client_id


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


def test_nightly_for_one_client_leaves_others_untouched(
    conn: psycopg.Connection, scratch_db_url: str
) -> None:
    """The wall holds THROUGH the routine: running for A projects A's flag and
    leaves B's rows — flags, queue, transactions — completely untouched, because
    the data steps run on an agent connection RLS-scoped to A."""
    client_a = _seed_client_with_work(conn, realm="agent-A")
    client_b = _seed_client_with_work(conn, realm="agent-B")

    def fake_sync(cid: UUID) -> None:
        return None

    summary = run_nightly(scratch_db_url, client_a, sync=fake_sync)
    assert summary["status"] == "succeeded"

    a_flag_items = [i for i in open_items(conn, client_a) if i["kind"] == "flag"]
    assert [i["title"] for i in a_flag_items] == ["transform_warning"], (
        "A's surviving flag is projected into A's queue"
    )

    # B is completely untouched — the routine never ran for B and could not
    # have reached it even if it had a bug passing B's id.
    assert open_items(conn, client_b) == [], "B got no queue items"
    b_flag = conn.execute(
        "SELECT status FROM flags WHERE client_id = %s AND rule_code ="
        " 'transform_warning'",
        (client_b,),
    ).fetchone()
    assert b_flag == ("open",), "B's flag is unchanged"
    b_txns = conn.execute(
        "SELECT count(*) FROM transactions WHERE client_id = %s", (client_b,)
    ).fetchone()
    assert b_txns == (1,), "B's transaction is intact"
    b_runs = conn.execute(
        "SELECT count(*) FROM runs WHERE client_id = %s", (client_b,)
    ).fetchone()
    assert b_runs == (0,), "no run rows were written for B"


def test_routine_agent_connection_cannot_reach_another_tenant(
    conn: psycopg.Connection, scratch_db_url: str
) -> None:
    """Fail-closed through the agent path: the routine's own scoped connection
    physically cannot reach another tenant — scoped to A, B's flags read as zero
    rows and a write for B is rejected by Postgres."""
    client_a = _seed_client_with_work(conn, realm="agent-A")
    client_b = _seed_client_with_work(conn, realm="agent-B")

    with agent_connection(scratch_db_url, client_a) as agent:
        b_visible = agent.execute(
            "SELECT count(*) FROM flags WHERE client_id = %s", (client_b,)
        ).fetchone()
        assert b_visible == (0,), "scoped to A, B's flags read as zero rows"
        a_visible = agent.execute("SELECT count(*) FROM flags").fetchone()
        assert a_visible == (1,), "the agent sees exactly A's one flag"

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            agent.execute(
                "INSERT INTO flags (client_id, rule_code, severity, status,"
                " source_type, source_ref) VALUES (%s, 'R000', 'warn', 'open',"
                " 'transaction', 'y')",
                (client_b,),
            )


def test_agent_connection_ends_its_transaction_on_exit(
    conn: psycopg.Connection, scratch_db_url: str
) -> None:
    """agent_connection must EXPLICITLY end its transaction on exit, not rely on
    the server rolling back on disconnect — a bare close leaves the backend
    `idle in transaction` holding the data steps' locks, which deadlocks the next
    per-client run_nightly (the scheduler runs them sequentially). Proven by a
    trailing write the caller never commits: an explicit commit-on-exit keeps it;
    a bare close loses it to the disconnect rollback. This is the deterministic
    bare-close gate — it fails on a bare close and passes on the fix."""
    client_id = make_client(conn)
    with agent_connection(scratch_db_url, client_id) as agent:
        agent.execute(
            "INSERT INTO flags (client_id, rule_code, severity, status,"
            " source_type, source_ref) VALUES (%s, 'R000', 'warn', 'open',"
            " 'transaction', 'exit-commit-probe')",
            (client_id,),
        )
        # the block exits with this INSERT uncommitted by the caller
    conn.rollback()  # fresh snapshot on the owner connection
    persisted = conn.execute(
        "SELECT count(*) FROM flags WHERE client_id = %s"
        " AND source_ref = 'exit-commit-probe'",
        (client_id,),
    ).fetchone()
    assert persisted == (1,), (
        "agent_connection must commit (explicitly end) its transaction on exit;"
        " a bare close loses the write to disconnect-rollback and leaves the"
        " backend idle in transaction"
    )


def test_run_nightly_sequential_calls_leave_no_idle_in_transaction(
    conn: psycopg.Connection, scratch_db_url: str
) -> None:
    """The production failure mode as a gate: the scheduler runs run_nightly per
    client sequentially, and a leaked `idle in transaction` agent connection
    holds the sync's locks and deadlocks the next call. Three sequential runs
    must all complete with nothing left idle in transaction. (On a local server a
    bare close terminates the backend promptly, so here this asserts the
    invariant; the deterministic bare-close gate is the commit-on-exit test.)"""
    client_id = make_client(conn)
    _seed_open_sync_flag(conn, client_id)

    def fake_sync(cid: UUID) -> None:
        return None

    run_nightly(scratch_db_url, client_id, sync=fake_sync)
    run_nightly(scratch_db_url, client_id, sync=fake_sync)
    third = run_nightly(scratch_db_url, client_id, sync=fake_sync)
    assert third["status"] == "succeeded", "the third sequential nightly returned (no hang)"

    conn.rollback()
    idle = conn.execute(
        "SELECT count(*) FROM pg_stat_activity"
        " WHERE datname = current_database() AND state = 'idle in transaction'"
        " AND pid <> pg_backend_pid()"
    ).fetchone()
    assert idle == (0,), "no agent connection left idle in transaction after the runs"
