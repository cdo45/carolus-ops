"""check_provenance must handle every source_type table shape — including
clients, which has no client_id column (the R034 crash: a client-scoped
flag made the generic predicate reference a nonexistent column)."""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg

from tests.conftest import make_client
from tests.factories import balanced_purchase, make_account
from tests.gate_phase2 import check_provenance


def flag(
    conn: psycopg.Connection, client_id: UUID, *, rule_code: str,
    source_type: str, source_ref: str,
) -> None:
    conn.execute(
        """
        INSERT INTO flags (client_id, rule_code, severity, status,
                           source_type, source_ref, detail)
        VALUES (%s, %s, 'warn', 'open', %s, %s, '{}')
        """,
        (client_id, rule_code, source_type, source_ref),
    )
    conn.commit()


def test_client_scoped_flag_passes_provenance(conn: psycopg.Connection) -> None:
    """The R034 case: source_type='client', source_ref = the client uuid."""
    client_id = make_client(conn)
    flag(conn, client_id, rule_code="R034", source_type="client",
         source_ref=str(client_id))

    assert check_provenance(conn, client_id) == []


def test_client_scoped_flag_for_wrong_client_is_dangling(
    conn: psycopg.Connection,
) -> None:
    client_id = make_client(conn)
    other = make_client(conn, realm="other-realm")
    flag(conn, client_id, rule_code="R034", source_type="client",
         source_ref=str(other))  # points at a DIFFERENT client's row

    problems = check_provenance(conn, client_id)
    assert len(problems) == 1 and "clients" in problems[0]


def test_mixed_types_still_validate(conn: psycopg.Connection) -> None:
    client_id = make_client(conn)
    bank = make_account(conn, client_id, name="Checking", acct_type="Bank")
    expense = make_account(conn, client_id, name="Office", acct_type="Expense")
    txn = balanced_purchase(conn, client_id, amount="100.00",
                            bank=bank, expense=expense)
    conn.commit()
    flag(conn, client_id, rule_code="R016", source_type="transaction",
         source_ref=str(txn))
    flag(conn, client_id, rule_code="R034", source_type="client",
         source_ref=str(client_id))
    flag(conn, client_id, rule_code="R013", source_type="account",
         source_ref=str(uuid4()))  # dangling on purpose

    problems = check_provenance(conn, client_id)
    assert len(problems) == 1
    assert problems[0].startswith("R013"), "only the dangling ref is reported"
