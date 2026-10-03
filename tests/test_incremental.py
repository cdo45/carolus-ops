"""Incremental (CDC) sync tests: response parsing is pure; cursor state,
re-transforms, and soft deletion run against the scratch database fixture.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import pytest

import sync.incremental as incremental
from sync.full_sync import run_full_sync
from sync.incremental import (
    CursorTooOld,
    Deletion,
    NoCursor,
    parse_cdc_response,
    run_incremental_sync,
)
from tests.conftest import make_client
from tests.qbo_fixtures import COMPANY, FakeQbo

# Sync cursors are anchored to the current time, not a fixed date:
# run_incremental_sync refuses any cursor older than CDC_MAX_AGE (29 days), so a
# hard-coded date turns these tests into a time bomb a month after they're
# written. Only the cursors meet that check; payload timestamps below are data.
_NOW = datetime.now(timezone.utc).replace(microsecond=0)
T0 = _NOW - timedelta(days=2)  # the seeded full-sync cursor
T1 = (_NOW - timedelta(days=1)).isoformat()  # first CDC response time
T2 = (_NOW - timedelta(hours=12)).isoformat()  # a later CDC response time

DELETED_PURCHASE = {
    "Id": "5001",
    "status": "Deleted",
    "MetaData": {"LastUpdatedTime": "2026-06-08T18:00:00-07:00"},
}


def changed_invoice() -> dict[str, Any]:
    invoice = copy.deepcopy(COMPANY["Invoice"][0])  # 1001
    invoice["TotalAmt"] = 1600.00
    invoice["Line"][1]["Amount"] = 600.00
    invoice["MetaData"] = {"LastUpdatedTime": "2026-06-08T17:00:00-07:00"}
    return invoice


def cdc_response(time: str = T1) -> dict[str, Any]:
    return {
        "CDCResponse": [
            {
                "QueryResponse": [
                    {"Invoice": [changed_invoice()], "startPosition": 1},
                    {"Purchase": [DELETED_PURCHASE]},
                ]
            }
        ],
        "time": time,
    }


@dataclass
class FakeCdc:
    response: dict[str, Any]
    calls: list[tuple[list[str], datetime]] = field(default_factory=list)

    def cdc(self, entities: list[str], changed_since: datetime) -> dict[str, Any]:
        self.calls.append((list(entities), changed_since))
        return self.response


# ---------------------------------------------------------------- pure


def test_parse_cdc_response_splits_upserts_and_deletions() -> None:
    changes = parse_cdc_response(cdc_response())
    assert [p["Id"] for p in changes.upserts["Invoice"]] == ["1001"]
    assert "Purchase" not in changes.upserts
    assert changes.deletions == [
        Deletion(
            "Purchase",
            "5001",
            datetime.fromisoformat("2026-06-08T18:00:00-07:00"),
        )
    ]
    assert changes.upsert_count == 1


def test_parse_cdc_response_ignores_untracked_keys() -> None:
    changes = parse_cdc_response(
        {"CDCResponse": [{"QueryResponse": [{"startPosition": 1, "maxResults": 0}]}]}
    )
    assert changes.upserts == {} and changes.deletions == []


# ---------------------------------------------------------------- DB-backed


def seed(conn: psycopg.Connection, cursor: datetime | None = T0) -> UUID:
    client_id = make_client(conn)
    run_full_sync(conn, client_id, "test-realm-1", qbo=FakeQbo())
    conn.execute(
        "INSERT INTO sync_connections (client_id, last_full_sync) VALUES (%s, %s)",
        (client_id, cursor),
    )
    conn.commit()
    return client_id


def cursor_of(conn: psycopg.Connection, client_id: UUID) -> datetime | None:
    row = conn.execute(
        "SELECT last_cdc_cursor FROM sync_connections WHERE client_id = %s",
        (client_id,),
    ).fetchone()
    return row[0] if row else None


def test_no_cursor_raises(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    with pytest.raises(NoCursor):
        run_incremental_sync(conn, client_id, "test-realm-1", qbo=FakeCdc({}))


def test_stale_cursor_raises_before_any_cdc_call(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    stale = datetime.now(timezone.utc) - timedelta(days=40)
    conn.execute(
        "INSERT INTO sync_connections (client_id, last_full_sync) VALUES (%s, %s)",
        (client_id, stale),
    )
    conn.commit()
    fake = FakeCdc({})
    with pytest.raises(CursorTooOld):
        run_incremental_sync(conn, client_id, "test-realm-1", qbo=fake)
    assert fake.calls == [], "no CDC call may happen with an unusable cursor"


def test_incremental_applies_changes_and_soft_deletes(
    conn: psycopg.Connection,
) -> None:
    client_id = seed(conn)
    fake = FakeCdc(cdc_response())

    summary = run_incremental_sync(conn, client_id, "test-realm-1", qbo=fake)

    # window: cursor (last_full_sync fallback) minus the overlap
    entities, changed_since = fake.calls[0]
    assert "Invoice" in entities and "Account" in entities
    assert changed_since == T0 - incremental.CDC_OVERLAP

    # changed invoice re-transformed: header + A/R line + item line changed
    assert summary["written"]["transactions"] == 1
    assert summary["written"]["journal_lines"] == 2
    amount = conn.execute(
        "SELECT amount FROM transactions WHERE client_id = %s AND qbo_id = '1001'",
        (client_id,),
    ).fetchone()
    assert amount is not None and amount[0] == Decimal("1600.00")

    # deleted purchase: soft-flagged with QBO's own timestamp, data intact
    deleted = conn.execute(
        """
        SELECT qbo_deleted_at, amount FROM transactions
        WHERE client_id = %s AND qbo_id = '5001' AND txn_type = 'Purchase'
        """,
        (client_id,),
    ).fetchone()
    assert deleted is not None
    assert deleted[0] == datetime.fromisoformat("2026-06-08T18:00:00-07:00")
    assert deleted[1] == Decimal("89.99"), "deletion stub must not clobber data"
    assert summary["deletions"] == 1
    flags = conn.execute(
        """
        SELECT count(*) FROM flags
        WHERE client_id = %s AND rule_code = 'qbo_deleted'
          AND source_ref = 'qbo:Purchase:5001' AND status = 'open'
        """,
        (client_id,),
    ).fetchone()
    assert flags is not None and flags[0] == 1

    # cursor advanced to the CDC response time, only on success
    assert cursor_of(conn, client_id) == datetime.fromisoformat(T1)

    status = conn.execute(
        "SELECT status FROM runs WHERE client_id = %s AND"
        " routine = 'incremental_sync'",
        (client_id,),
    ).fetchall()
    assert status == [("succeeded",)]


def test_incremental_rerun_is_idempotent(conn: psycopg.Connection) -> None:
    client_id = seed(conn)
    run_incremental_sync(conn, client_id, "test-realm-1", qbo=FakeCdc(cdc_response()))

    again = run_incremental_sync(
        conn, client_id, "test-realm-1", qbo=FakeCdc(cdc_response())
    )

    assert again["written"] == {
        "accounts": 0, "entities": 0, "jobs": 0,
        "transactions": 0, "journal_lines": 0,
    }
    assert again["deletions"] == 0, "qbo_deleted_at: first observation wins"
    flags = conn.execute(
        "SELECT count(*) FROM flags WHERE client_id = %s AND rule_code ="
        " 'qbo_deleted'",
        (client_id,),
    ).fetchone()
    assert flags is not None and flags[0] == 1, "no duplicate deletion flags"


def test_failure_does_not_advance_cursor(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_id = seed(conn)
    run_incremental_sync(conn, client_id, "test-realm-1", qbo=FakeCdc(cdc_response()))
    assert cursor_of(conn, client_id) == datetime.fromisoformat(T1)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("transform exploded")

    monkeypatch.setattr(incremental, "transform_client", boom)
    with pytest.raises(RuntimeError, match="transform exploded"):
        run_incremental_sync(
            conn, client_id, "test-realm-1", qbo=FakeCdc(cdc_response(time=T2))
        )

    assert cursor_of(conn, client_id) == datetime.fromisoformat(T1), (
        "failed run must not advance the cursor"
    )
    failed = conn.execute(
        "SELECT count(*) FROM runs WHERE client_id = %s AND status = 'failed'",
        (client_id,),
    ).fetchone()
    assert failed is not None and failed[0] == 1
