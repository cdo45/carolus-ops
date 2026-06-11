"""Curate the client's sales-tax liability account.

Usage:
    uv run python -m sync.set_tax_account --realm <realm_id>
        list the GlobalTaxPayable candidates (qbo_id, name) and the
        current setting

    uv run python -m sync.set_tax_account --realm <realm_id> --account <qbo_id>
        set clients.sales_tax_account_id to that account (curated — sync
        never writes it) and print confirmation

Why this exists: charts of accounts can carry MULTIPLE GlobalTaxPayable
accounts (e.g. two state agencies); transform resolution correctly
refuses to guess between them, leaving taxed transactions flagged. This
is the one-command human answer.
"""

from __future__ import annotations

import argparse
import os
import sys
from uuid import UUID

import psycopg
from dotenv import load_dotenv


def list_candidates(
    conn: psycopg.Connection, client_id: UUID
) -> list[tuple[str, str, bool]]:
    """(qbo_id, name, is_current) for every GlobalTaxPayable account."""
    rows = conn.execute(
        """
        SELECT a.qbo_id, a.name,
               a.id = c.sales_tax_account_id AS is_current
        FROM accounts a
        JOIN clients c ON c.id = a.client_id
        WHERE a.client_id = %s AND a.acct_subtype = 'GlobalTaxPayable'
          AND a.qbo_deleted_at IS NULL
        ORDER BY a.qbo_id
        """,
        (client_id,),
    ).fetchall()
    return [(qbo_id, name, bool(current)) for qbo_id, name, current in rows]


def set_account(
    conn: psycopg.Connection, client_id: UUID, qbo_id: str
) -> tuple[str, str | None]:
    """Point the curated fk at the account; returns (name, acct_subtype)."""
    row = conn.execute(
        "SELECT id, name, acct_subtype FROM accounts"
        " WHERE client_id = %s AND qbo_id = %s",
        (client_id, qbo_id),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"no account with qbo_id {qbo_id!r} for this client — run a"
            " sync first, or check the id against the candidate list"
        )
    account_id, name, subtype = row
    conn.execute(
        "UPDATE clients SET sales_tax_account_id = %s WHERE id = %s",
        (account_id, client_id),
    )
    conn.commit()
    return name, subtype


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Curate the client's sales-tax liability account"
    )
    parser.add_argument("--realm", required=True, help="QBO realm id")
    parser.add_argument("--account", default=None,
                        help="qbo_id of the account to set")
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT id, name FROM clients WHERE qbo_realm_id = %s",
            (args.realm,),
        ).fetchone()
        if row is None:
            print(f"no client for realm {args.realm}", file=sys.stderr)
            return 1
        client_id, client_name = row

        if args.account is None:
            candidates = list_candidates(conn, client_id)
            print(f"GlobalTaxPayable candidates — {client_name}:")
            if not candidates:
                print("  (none found — sync first, or the chart has no"
                      " sales-tax liability account)")
                return 1
            for qbo_id, name, is_current in candidates:
                marker = "  * " if is_current else "    "
                print(f"{marker}qbo {qbo_id}: {name}")
            print("(* = currently curated; set with --account <qbo_id>,"
                  " then run sync.retransform)")
            return 0

        try:
            name, subtype = set_account(conn, client_id, args.account)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"sales tax account for {client_name} set to:"
              f" {name} (qbo {args.account})")
        if subtype != "GlobalTaxPayable":
            print(f"  note: account subtype is {subtype!r}, not"
                  " GlobalTaxPayable — set deliberately?")
        print("run `uv run python -m sync.retransform --realm"
              f" {args.realm}` to repair flagged transactions")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
