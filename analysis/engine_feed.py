"""carolus canonical Postgres → KPI-engine SQLite feed (Part 1: COA + GL).

Per client and reporting period this builds a fresh engine SQLite database
(the schema in ``kpi_engine/core/db.py``) and fills the three inputs the
balance-sheet / P&L KPIs read:

  * ``accounts``      — one row per canonical account; category/confidence are
                        filled by the engine's own classifier, never here.
  * ``transactions``  — one row per canonical journal line in the period;
                        ``amount`` is SIGNED (debit → +, credit → −), the
                        engine's convention.
  * opening balances  — per account, the signed GL balance as of
                        (period_start − 1 day), written as the ``gl_balances``
                        audit row that ``kpi_engine/core/kpi/base.py`` reads.

The engine is a vendored black box: we put ``kpi_engine/`` on ``sys.path`` and
call ``core.db`` / ``core.classify`` — we do not touch its internals, and we do
NOT wire its ``core.parsers`` / ``core.importer`` as the feed.

All canonical reads are scoped to one ``client_id`` (Principle: RLS / one
engine DB per company). The feed is idempotent: it clears its three target
tables before repopulating, so re-running produces zero duplicates and zero
drift.

AR/AP sub-ledger projection (``ar_aging_*`` / ``invoice_payments`` …) and the
write-back into carolus tables are Part 2 — not built here.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:  # psycopg is a runtime dep; only the type import is guarded.
    import psycopg

# Where the gl_balances audit row records that the opening balance came from
# carolus canonical GL (the engine's CSV importer writes "declared"/"computed").
BEGINNING_BALANCE_SOURCE = "carolus-canonical-gl"

_KPI_ENGINE_ROOT = Path(__file__).resolve().parent.parent / "kpi_engine"


@lru_cache(maxsize=1)
def _engine() -> SimpleNamespace:
    """Import the vendored engine's modules with ``kpi_engine/`` on sys.path.

    Done lazily and cached so merely importing this module does not mutate
    sys.path. The engine packages resolve ``import core.*`` against the engine
    root; carolus has no top-level ``core`` package, so there is no collision.
    """
    root = str(_KPI_ENGINE_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from core import classify as engine_classify  # type: ignore[import-not-found]
    from core import db as engine_db  # type: ignore[import-not-found]
    from core.kpi import base as engine_base  # type: ignore[import-not-found]

    return SimpleNamespace(db=engine_db, classify=engine_classify, base=engine_base)


@dataclass
class FeedReport:
    """Outcome of one feed, for orchestration/logging and idempotency checks."""

    client_id: str
    period_start: str
    period_end: str
    accounts: int = 0
    transactions: int = 0
    opening_rows: int = 0
    skipped_undated_lines: int = 0
    # canonical account uuid (str) → engine integer account id.
    account_id_map: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested without a database)                               #
# --------------------------------------------------------------------------- #


def signed_amount(amount: Decimal | float | str, posting_type: str) -> float:
    """Canonical (magnitude, posting_type) → the engine's SIGNED amount.

    Canonical ``journal_lines.amount`` is a non-negative magnitude; the sign
    lives in ``posting_type``. The engine sums signed amounts and never flips
    by category, so a debit is +magnitude and a credit is −magnitude. Returned
    rounded to cents to keep SQLite REAL math stable.
    """
    posting = posting_type.strip().lower()
    if posting not in ("debit", "credit"):
        raise ValueError(f"unknown posting_type {posting_type!r}")
    magnitude = Decimal(str(amount))
    signed = magnitude if posting == "debit" else -magnitude
    return float(round(signed, 2))


def gl_balances_value(beginning_balance: float) -> str:
    """The ``new_value`` JSON for a ``gl_balances`` audit row.

    Same shape the engine's GL importer writes; ``kpi_engine/core/kpi/base.py``
    reads only ``beginning_balance``. ``upload_id``/``declared_net_activity``
    are not applicable to the canonical feed and are recorded NULL.
    """
    return json.dumps(
        {
            "upload_id": None,
            "beginning_balance": round(float(beginning_balance), 2),
            "beginning_balance_source": BEGINNING_BALANCE_SOURCE,
            "declared_net_activity": None,
        },
        separators=(",", ":"),
    )


def resolve_qbo_names(
    rows: list[dict[str, object]],
) -> dict[str, str]:
    """Map each canonical account uuid → a unique engine ``qbo_name``.

    The engine's ``accounts`` table is ``UNIQUE(qbo_name)``. Canonical
    ``accounts.name`` is the QBO *leaf* name (``Name``), which is normally
    unique within a chart of accounts, so the leaf is used as-is (spec:
    qbo_name ← name). On the rare collision the row falls back to its
    fully-qualified name, then to ``name (qbo_id)`` — deterministic either way.
    Rows are processed in input order, so the first holder keeps the bare name.
    """
    used: set[str] = set()
    out: dict[str, str] = {}
    for r in rows:
        uid = str(r["id"])
        name = str(r["name"])
        candidates = [name]
        fqn = r.get("fqn")
        if fqn:
            candidates.append(str(fqn))
        candidates.append(f"{name} ({r['qbo_id']})")
        chosen = next((c for c in candidates if c not in used), candidates[-1])
        used.add(chosen)
        out[uid] = chosen
    return out


# --------------------------------------------------------------------------- #
# Canonical reads (scoped to one client)                                       #
# --------------------------------------------------------------------------- #


def _scope(pg: "psycopg.Connection", client_id: str | UUID) -> None:
    """Pin the RLS tenant GUC for this connection (defense in depth).

    The pipeline connects as the table owner, which bypasses RLS, so every
    query below ALSO filters by ``client_id`` explicitly. Setting the GUC keeps
    the read correct if the same code runs as the least-privilege
    ``carolus_app`` role.
    """
    pg.execute("SELECT set_config('app.current_client', %s, false)", (str(client_id),))


def _read_accounts(
    pg: "psycopg.Connection", client_id: str | UUID
) -> list[dict[str, object]]:
    """All accounts for the client, with AcctNum / FullyQualifiedName pulled
    from the latest staged QBO ``Account`` payload when present."""
    rows = pg.execute(
        """
        SELECT a.id, a.qbo_id, a.name, a.acct_type, a.acct_subtype, a.active,
               r.payload ->> 'AcctNum'            AS acct_num,
               r.payload ->> 'FullyQualifiedName' AS fqn
        FROM accounts a
        LEFT JOIN LATERAL (
            SELECT q.payload
            FROM qbo_raw q
            WHERE q.client_id = a.client_id
              AND q.entity_type = 'Account'
              AND q.qbo_id = a.qbo_id
            ORDER BY q.fetched_at DESC, q.id DESC
            LIMIT 1
        ) r ON true
        WHERE a.client_id = %s
        ORDER BY a.name, a.qbo_id
        """,
        (str(client_id),),
    ).fetchall()
    cols = ("id", "qbo_id", "name", "acct_type", "acct_subtype", "active",
            "acct_num", "fqn")
    return [dict(zip(cols, row)) for row in rows]


def _read_balances(
    pg: "psycopg.Connection", client_id: str | UUID, cutoff: str, *, strict: str
) -> dict[str, float]:
    """Signed GL balance per canonical account id over dated lines.

    ``strict='lt'`` sums lines with ``txn_date < cutoff`` (opening balance as of
    cutoff−1 day); ``strict='le'`` sums ``txn_date <= cutoff`` (closing
    balance). Undated transactions cannot be placed in time and are excluded.
    """
    op = "<" if strict == "lt" else "<="
    rows = pg.execute(
        f"""
        SELECT jl.account_id,
               SUM(CASE WHEN jl.posting_type = 'debit'
                        THEN jl.amount ELSE -jl.amount END) AS balance
        FROM journal_lines jl
        JOIN transactions t ON t.id = jl.transaction_id
        WHERE t.client_id = %s
          AND t.txn_date IS NOT NULL
          AND t.txn_date {op} %s
        GROUP BY jl.account_id
        """,
        (str(client_id), cutoff),
    ).fetchall()
    return {str(account_id): float(round(balance, 2)) for account_id, balance in rows}


def _read_period_lines(
    pg: "psycopg.Connection",
    client_id: str | UUID,
    period_start: str,
    period_end: str,
) -> list[dict[str, object]]:
    """One row per journal line whose transaction is dated within the period,
    joined to its transaction header and (optionally) its entity."""
    rows = pg.execute(
        """
        SELECT jl.account_id, jl.amount, jl.posting_type, jl.description,
               t.txn_date, t.txn_type, t.doc_number,
               e.name AS entity_name
        FROM journal_lines jl
        JOIN transactions t ON t.id = jl.transaction_id
        LEFT JOIN entities e ON e.id = t.entity_id
        WHERE t.client_id = %s
          AND t.txn_date IS NOT NULL
          AND t.txn_date BETWEEN %s AND %s
        ORDER BY t.txn_date, t.id, jl.line_no
        """,
        (str(client_id), period_start, period_end),
    ).fetchall()
    cols = ("account_id", "amount", "posting_type", "description", "txn_date",
            "txn_type", "doc_number", "entity_name")
    return [dict(zip(cols, row)) for row in rows]


def _count_undated_lines(
    pg: "psycopg.Connection", client_id: str | UUID
) -> int:
    """Journal lines on undated transactions — excluded from the feed; counted
    so a non-tie-out can be explained (Principle 2: no silent drops)."""
    row = pg.execute(
        """
        SELECT count(*)
        FROM journal_lines jl
        JOIN transactions t ON t.id = jl.transaction_id
        WHERE t.client_id = %s AND t.txn_date IS NULL
        """,
        (str(client_id),),
    ).fetchone()
    return int(row[0])


# --------------------------------------------------------------------------- #
# Engine-SQLite writes                                                         #
# --------------------------------------------------------------------------- #


def _to_iso(value: object) -> str:
    """A canonical date column → 'YYYY-MM-DD' text (the engine stores TEXT)."""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()[:10]
    return str(value)


def _populate_accounts(
    engine_conn: sqlite3.Connection,
    accounts: list[dict[str, object]],
    closing: dict[str, float],
) -> dict[str, int]:
    """Insert canonical accounts into the engine; return uuid → engine id.

    ``qbo_name`` is the (disambiguated) account name, ``qbo_type`` ← acct_type,
    ``detail_type`` ← acct_subtype, ``account_number`` ← QBO AcctNum.
    ``coa_balance`` carries the account's closing GL balance (informational;
    ``dormant`` stays 0 so nothing is excluded). category/confidence are left
    NULL here and filled by the engine's classifier next.
    """
    qbo_names = resolve_qbo_names(accounts)
    id_map: dict[str, int] = {}
    for a in accounts:
        uid = str(a["id"])
        cur = engine_conn.execute(
            """
            INSERT INTO accounts
                (qbo_name, full_path, account_number, qbo_type, detail_type,
                 dormant, coa_balance)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (
                qbo_names[uid],
                a.get("fqn") or a["name"],
                a.get("acct_num"),
                a.get("acct_type"),
                a.get("acct_subtype"),
                closing.get(uid, 0.0),
            ),
        )
        id_map[uid] = int(cur.lastrowid)
    return id_map


def _populate_transactions(
    engine_conn: sqlite3.Connection,
    lines: list[dict[str, object]],
    id_map: dict[str, int],
) -> int:
    """Insert one engine transaction row per canonical journal line, signed."""
    payload = []
    for ln in lines:
        uid = str(ln["account_id"])
        if uid not in id_map:  # FK guarantees this never happens; fail loud.
            raise KeyError(f"journal line references unknown account {uid}")
        payload.append(
            (
                id_map[uid],
                _to_iso(ln["txn_date"]),
                ln.get("txn_type"),
                ln.get("doc_number"),
                ln.get("entity_name"),
                ln.get("description"),
                signed_amount(ln["amount"], str(ln["posting_type"])),
            )
        )
    engine_conn.executemany(
        """
        INSERT INTO transactions
            (account_id, txn_date, txn_type, num, name, description, amount)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    return len(payload)


def _write_opening_balances(
    engine_conn: sqlite3.Connection,
    id_map: dict[str, int],
    beginnings: dict[str, float],
    *,
    now: str,
) -> int:
    """Write one ``gl_balances`` audit row per account (Principle 2: every
    account's opening balance is recorded, default 0.0 for no prior activity)."""
    rows = [
        (
            now,
            engine_id,
            gl_balances_value(beginnings.get(uid, 0.0)),
        )
        for uid, engine_id in id_map.items()
    ]
    engine_conn.executemany(
        """
        INSERT INTO audit_log
            (ts, entity, entity_id, field, new_value, source)
        VALUES (?, 'accounts', ?, 'gl_balances', ?, 'import')
        """,
        rows,
    )
    return len(rows)


def _clear_targets(engine_conn: sqlite3.Connection) -> None:
    """Drop the three feed-owned datasets so a re-run leaves zero drift.
    transactions first (FK to accounts); gl_balances audit rows; then accounts."""
    engine_conn.execute("DELETE FROM transactions")
    engine_conn.execute(
        "DELETE FROM audit_log WHERE entity = 'accounts' AND field = 'gl_balances'"
    )
    engine_conn.execute("DELETE FROM accounts")


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #


def feed_engine(
    pg: "psycopg.Connection",
    engine_conn: sqlite3.Connection,
    global_conn: sqlite3.Connection,
    client_id: str | UUID,
    period_start: str,
    period_end: str,
    *,
    now: str | None = None,
) -> FeedReport:
    """Fill one engine DB from canonical Postgres for ``client_id`` over the
    reporting period. Idempotent: clears its targets first, one transaction.
    """
    engine = _engine()
    now = now or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    report = FeedReport(str(client_id), period_start, period_end)

    _scope(pg, client_id)
    accounts = _read_accounts(pg, client_id)
    closing = _read_balances(pg, client_id, period_end, strict="le")
    beginnings = _read_balances(pg, client_id, period_start, strict="lt")
    lines = _read_period_lines(pg, client_id, period_start, period_end)
    report.skipped_undated_lines = _count_undated_lines(pg, client_id)

    try:
        _clear_targets(engine_conn)
        id_map = _populate_accounts(engine_conn, accounts, closing)
        report.transactions = _populate_transactions(engine_conn, lines, id_map)
        report.opening_rows = _write_opening_balances(
            engine_conn, id_map, beginnings, now=now
        )
        engine_conn.commit()
    except Exception:
        engine_conn.rollback()
        raise

    # Classifier runs in its own transaction (engine-owned); fills category.
    engine.classify.classify_client(engine_conn, global_conn)

    report.accounts = len(id_map)
    report.account_id_map = id_map
    return report


def build_engine_db(
    pg: "psycopg.Connection",
    client_id: str | UUID,
    period_start: str,
    period_end: str,
    base_dir: str | Path,
    *,
    slug: str | None = None,
    now: str | None = None,
) -> tuple[sqlite3.Connection, sqlite3.Connection, FeedReport]:
    """Open a fresh per-client engine DB under ``base_dir`` and feed it.

    Returns the open ``(engine_conn, global_conn, report)``; the caller owns
    closing them. ``slug`` defaults to the client id (the engine keys its
    on-disk DB by slug — one company per DB).
    """
    engine = _engine()
    slug = slug or str(client_id)
    engine_conn = engine.db.get_client_db(slug, base_dir=base_dir)
    global_conn = engine.db.get_global_db(base_dir=base_dir)
    report = feed_engine(
        pg, engine_conn, global_conn, client_id, period_start, period_end, now=now
    )
    return engine_conn, global_conn, report


def main(argv: list[str] | None = None) -> int:
    """Manual entry: feed an engine DB for one client/period from canonical PG.

        uv run python -m analysis.engine_feed <client_id> <start> <end> [outdir]

    Reads DATABASE_URL from the environment (Doppler-sourced). Prints a summary
    only — never client data.
    """
    import argparse
    import os
    import tempfile

    import psycopg
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="Feed the KPI engine from canonical PG")
    parser.add_argument("client_id")
    parser.add_argument("period_start", help="YYYY-MM-DD")
    parser.add_argument("period_end", help="YYYY-MM-DD")
    parser.add_argument("base_dir", nargs="?", default=None,
                        help="engine appdata dir (default: a temp dir)")
    args = parser.parse_args(argv)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("missing required env var: DATABASE_URL", file=sys.stderr)
        return 2

    base_dir = args.base_dir or tempfile.mkdtemp(prefix="carolus-kpi-")
    with psycopg.connect(database_url) as pg:
        engine_conn, global_conn, report = build_engine_db(
            pg, args.client_id, args.period_start, args.period_end, base_dir
        )
    engine_conn.close()
    global_conn.close()
    print(
        f"fed engine DB at {base_dir}: "
        f"{report.accounts} accounts, {report.transactions} txn lines, "
        f"{report.opening_rows} opening balances"
        + (f", {report.skipped_undated_lines} undated lines skipped"
           if report.skipped_undated_lines else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
