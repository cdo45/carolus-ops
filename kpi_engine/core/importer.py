"""Import pipeline.

Writes parsed COA and GL results into a client database.

COA imports upsert accounts by qbo_name and never overwrite fields a user
may have touched (category, status, confidence, proposed_number). GL imports
use replace-by-range semantics: existing transactions inside the upload's
period are captured (gzipped JSON) for recovery, deleted, and replaced by
the new set — all inside one database transaction.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field

NUMBER_PREFIX_RE = re.compile(r"^\d{3,6}(\.\d+)?\s+")

from core.parsers.aging import AgingParseResult
from core.parsers.coa import COAParseResult
from core.parsers.gl import GLParseResult
from core.parsers.pairings import PairingsParseResult

DIFF_SAMPLE_LIMIT = 10


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class CoaImportReport:
    new_accounts: list[str] = field(default_factory=list)
    updated_count: int = 0
    total: int = 0


@dataclass
class GlDiff:
    changed_count: int = 0
    added_count: int = 0
    removed_count: int = 0
    changed_samples: list[dict] = field(default_factory=list)


@dataclass
class AgingImportReport:
    upload_id: int
    snapshot_id: int
    replaced: bool = False  # True when an existing as-of snapshot was replaced
    inserted_count: int = 0
    deleted_count: int = 0


@dataclass
class PairingsImportReport:
    upload_id: int
    inserted_count: int = 0
    deleted_count: int = 0
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    diff: GlDiff = field(default_factory=GlDiff)


@dataclass
class GlImportReport:
    upload_id: int
    inserted_count: int = 0
    deleted_count: int = 0
    matched_count: int = 0
    stubbed_accounts: list[str] = field(default_factory=list)
    skipped_parent_headers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    diff: GlDiff = field(default_factory=GlDiff)


def _compute_diff(
    old_keys: Counter, new_keys: Counter, party_label: str
) -> GlDiff:
    """Multiset diff on (party, date, num, amount) keys.

    Pairs of removed+added sharing (party, date, num) count as changes;
    leftovers are removals/additions. Identical sets diff to all zeros.
    """
    diff = GlDiff()
    removed = old_keys - new_keys
    added = new_keys - old_keys
    by_identity_removed: dict[tuple, list] = defaultdict(list)
    by_identity_added: dict[tuple, list] = defaultdict(list)
    for (party, date, num, amount), n in removed.items():
        by_identity_removed[(party, date, num)].extend([amount] * n)
    for (party, date, num, amount), n in added.items():
        by_identity_added[(party, date, num)].extend([amount] * n)
    for identity in set(by_identity_removed) | set(by_identity_added):
        olds = by_identity_removed.get(identity, [])
        news = by_identity_added.get(identity, [])
        pairs = min(len(olds), len(news))
        diff.changed_count += pairs
        for i in range(pairs):
            if len(diff.changed_samples) < DIFF_SAMPLE_LIMIT:
                party, date, num = identity
                diff.changed_samples.append(
                    {
                        party_label: party,
                        "txn_date": date,
                        "num": num,
                        "old_amount": olds[i],
                        "new_amount": news[i],
                    }
                )
        diff.removed_count += len(olds) - pairs
        diff.added_count += len(news) - pairs
    return diff


def _capture_superseded(
    conn: sqlite3.Connection,
    upload_id: int,
    report_type: str,
    period_start: str,
    period_end: str,
    old_rows: list[sqlite3.Row],
    entity: str,
) -> None:
    """Stash rows about to be replaced: gzipped JSON into the previous
    covering upload's superseded_data, or an audit_log row if none exists."""
    if not old_rows:
        return
    payload = gzip.compress(
        json.dumps([dict(r) for r in old_rows], separators=(",", ":")).encode(
            "utf-8"
        )
    )
    prior = conn.execute(
        """
        SELECT id FROM uploads
        WHERE report_type = ? AND id != ?
          AND period_start IS NOT NULL AND period_end IS NOT NULL
          AND period_start <= ? AND period_end >= ?
        ORDER BY id DESC LIMIT 1
        """,
        (report_type, upload_id, period_end, period_start),
    ).fetchone()
    if prior:
        conn.execute(
            "UPDATE uploads SET superseded_data = ? WHERE id = ?",
            (payload, prior["id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO audit_log (ts, entity, field, old_value, source)
            VALUES (?, ?, 'superseded', ?, 'import')
            """,
            (_now(), entity, payload),
        )


def import_coa(conn: sqlite3.Connection, coa_result: COAParseResult) -> CoaImportReport:
    """Upsert COA accounts. Existing rows keep every user-editable field."""
    report = CoaImportReport(total=len(coa_result.accounts))
    try:
        for acct in coa_result.accounts:
            existing = conn.execute(
                "SELECT id FROM accounts WHERE qbo_name = ?", (acct.qbo_name,)
            ).fetchone()
            inactive = 1 if acct.deleted_marker else 0
            if existing:
                conn.execute(
                    """
                    UPDATE accounts
                    SET full_path = ?, qbo_type = ?, detail_type = ?,
                        account_number = ?, coa_balance = ?,
                        inactive_candidate = MAX(inactive_candidate, ?)
                    WHERE id = ?
                    """,
                    (
                        acct.full_path,
                        acct.qbo_type,
                        acct.detail_type,
                        acct.account_number,
                        acct.balance,
                        inactive,
                        existing["id"],
                    ),
                )
                report.updated_count += 1
            else:
                conn.execute(
                    """
                    INSERT INTO accounts
                        (qbo_name, full_path, qbo_type, detail_type,
                         account_number, coa_balance, status,
                         inactive_candidate, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
                    """,
                    (
                        acct.qbo_name,
                        acct.full_path,
                        acct.qbo_type,
                        acct.detail_type,
                        acct.account_number,
                        acct.balance,
                        inactive,
                        _now(),
                    ),
                )
                report.new_accounts.append(acct.qbo_name)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return report


def _backfill_account_number(
    conn: sqlite3.Connection, account_id: int, number: str
) -> None:
    if conn.execute(
        "SELECT account_number FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()["account_number"] is None:
        conn.execute(
            "UPDATE accounts SET account_number = ? WHERE id = ?",
            (number, account_id),
        )


def _account_index(conn: sqlite3.Connection):
    """Snapshot accounts with computed leaves, composite keys, and parent flags."""
    rows = conn.execute(
        "SELECT id, qbo_name, full_path, account_number FROM accounts"
    ).fetchall()
    paths = {r["full_path"] or r["qbo_name"] for r in rows}
    by_name: dict[str, int] = {}
    by_leaf: dict[str, list[sqlite3.Row]] = defaultdict(list)
    by_composite: dict[str, int] = {}
    parents: set[int] = set()
    for r in rows:
        path = r["full_path"] or r["qbo_name"]
        by_name[r["qbo_name"]] = r["id"]
        by_leaf[path.split(":")[-1].strip()].append(r)
        if r["account_number"]:
            by_composite[f"{r['account_number']} {r['qbo_name']}"] = r["id"]
        if any(p.startswith(path + ":") for p in paths):
            parents.add(r["id"])
    return by_name, by_leaf, by_composite, parents


def import_gl(
    conn: sqlite3.Connection, gl_result: GLParseResult, filename: str
) -> GlImportReport:
    """Replace-by-range GL import. Atomic: any failure rolls everything back."""
    period_start = gl_result.period_start
    period_end = gl_result.period_end
    all_dates = [
        t.txn_date for a in gl_result.accounts for t in a.transactions
    ]
    derived_period = False
    if period_start is None or period_end is None:
        if not all_dates:
            raise ValueError(
                "GL file has no period line and no transactions — nothing "
                "to import."
            )
        period_start = period_start or min(all_dates)
        period_end = period_end or max(all_dates)
        derived_period = True

    try:
        cur = conn.execute(
            """
            INSERT INTO uploads
                (report_type, period_start, period_end, filename, row_count,
                 uploaded_at)
            VALUES ('GL', ?, ?, ?, ?, ?)
            """,
            (period_start, period_end, filename, gl_result.row_count, _now()),
        )
        upload_id = cur.lastrowid
        report = GlImportReport(upload_id=upload_id)
        if derived_period:
            report.warnings.append(
                "Period line missing or partial; range derived from "
                f"transaction dates: {period_start}-{period_end}."
            )

        by_name, by_leaf, by_composite, parents = _account_index(conn)

        # --- account matching (6-step waterfall) ------------------------
        matched: list[tuple[int, object]] = []  # (account_id, GLAccount)
        for section in gl_result.accounts:
            sname = section.name

            # Zero-tx parent guard: hierarchy header with no data rows.
            if not section.transactions:
                leaf_rows_check = by_leaf.get(sname, [])
                exact_id_check = by_name.get(sname)
                candidate_ids = {r["id"] for r in leaf_rows_check}
                if exact_id_check is not None:
                    candidate_ids.add(exact_id_check)
                if candidate_ids & parents:
                    report.skipped_parent_headers.append(sname)
                    report.notes.append(
                        f"Section {sname!r} is a parent hierarchy "
                        "header; skipped."
                    )
                    continue

            # Step a: exact match on section name.
            exact_id = by_name.get(sname)
            if exact_id is not None:
                matched.append((exact_id, section))
                report.matched_count += 1
                continue

            # Step b: number-composite (account_number + " " + qbo_name).
            composite_id = by_composite.get(sname)
            if composite_id is not None:
                matched.append((composite_id, section))
                report.matched_count += 1
                continue

            # Step c: unique leaf match.
            leaf_rows = by_leaf.get(sname, [])
            if len(leaf_rows) == 1:
                matched.append((leaf_rows[0]["id"], section))
                report.matched_count += 1
                continue

            # Step d: strip leading number prefix, retry steps a and c.
            m = NUMBER_PREFIX_RE.match(sname)
            if m:
                stripped = sname[m.end():]
                numeric_token = sname[:m.end()].strip()

                exact_id2 = by_name.get(stripped)
                if exact_id2 is not None:
                    _backfill_account_number(conn, exact_id2, numeric_token)
                    matched.append((exact_id2, section))
                    report.matched_count += 1
                    continue

                leaf_rows2 = by_leaf.get(stripped, [])
                if len(leaf_rows2) == 1:
                    _backfill_account_number(
                        conn, leaf_rows2[0]["id"], numeric_token
                    )
                    matched.append((leaf_rows2[0]["id"], section))
                    report.matched_count += 1
                    continue

            # Step e: multiple leaf candidates → stub with warning.
            # Step f: no match at all → stub with warning.
            if len(leaf_rows) > 1:
                names = ", ".join(sorted(r["qbo_name"] for r in leaf_rows))
                report.warnings.append(
                    f"GL section {sname!r} matches multiple accounts "
                    f"({names}); created a new account rather than guessing."
                )
            else:
                report.warnings.append(
                    f"GL section {sname!r} has no matching account; "
                    "created a new one."
                )
            cur = conn.execute(
                """
                INSERT INTO accounts (qbo_name, status, created_at)
                VALUES (?, 'proposed', ?)
                """,
                (sname, _now()),
            )
            matched.append((cur.lastrowid, section))
            report.stubbed_accounts.append(sname)

        # --- diff old vs new over the range (before deletion) ------------
        old_rows = conn.execute(
            """
            SELECT t.id, a.qbo_name AS account, t.txn_date, t.txn_type,
                   t.num, t.name, t.description, t.split, t.amount,
                   t.running_balance, t.job_prefix, t.upload_id
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE t.txn_date BETWEEN ? AND ?
            """,
            (period_start, period_end),
        ).fetchall()
        account_names = {
            aid: conn.execute(
                "SELECT qbo_name FROM accounts WHERE id = ?", (aid,)
            ).fetchone()["qbo_name"]
            for aid, _ in matched
        }
        new_keys = Counter(
            (account_names[aid], t.txn_date, t.num, round(t.amount, 2))
            for aid, section in matched
            for t in section.transactions
        )
        old_keys = Counter(
            (r["account"], r["txn_date"], r["num"], round(r["amount"], 2))
            for r in old_rows
        )
        report.diff = _compute_diff(old_keys, new_keys, "account")

        # --- supersede capture -------------------------------------------
        _capture_superseded(
            conn, upload_id, "GL", period_start, period_end, old_rows,
            entity="transactions",
        )

        # --- period-shortening guard --------------------------------------
        retained = conn.execute(
            "SELECT MAX(txn_date) AS max_date FROM transactions "
            "WHERE txn_date > ?",
            (period_end,),
        ).fetchone()
        if retained and retained["max_date"]:
            report.notes.append(
                f"Previous data through {retained['max_date']} retained; "
                f"this upload covers {period_start}-{period_end}."
            )

        # --- replace -------------------------------------------------------
        cur = conn.execute(
            "DELETE FROM transactions WHERE txn_date BETWEEN ? AND ?",
            (period_start, period_end),
        )
        report.deleted_count = cur.rowcount
        for aid, section in matched:
            conn.executemany(
                """
                INSERT INTO transactions
                    (account_id, txn_date, txn_type, num, name, description,
                     split, amount, running_balance, job_prefix, upload_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        aid,
                        t.txn_date,
                        t.txn_type,
                        t.num,
                        t.name,
                        t.description,
                        t.split,
                        t.amount,
                        t.running_balance,
                        t.job_prefix,
                        upload_id,
                    )
                    for t in section.transactions
                ],
            )
            report.inserted_count += len(section.transactions)
            # Derived values deserve an audit trail.
            conn.execute(
                """
                INSERT INTO audit_log
                    (ts, entity, entity_id, field, new_value, source)
                VALUES (?, 'accounts', ?, 'gl_balances', ?, 'import')
                """,
                (
                    _now(),
                    aid,
                    json.dumps(
                        {
                            "upload_id": upload_id,
                            "beginning_balance": section.beginning_balance,
                            "beginning_balance_source": section.beginning_balance_source,
                            "declared_net_activity": section.declared_net_activity,
                        },
                        separators=(",", ":"),
                    ),
                ),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return report


def import_aging(
    conn: sqlite3.Connection, result: AgingParseResult, filename: str
) -> AgingImportReport:
    """Snapshot semantics: one snapshot per as-of date. Re-importing the
    same date replaces that snapshot's rows; a new date accumulates a new
    snapshot alongside. Atomic either way."""
    if not result.as_of_date:
        raise ValueError("Aging report has no as-of date; cannot snapshot.")
    if result.side == "AR":
        snap_table, rows_table, party_col = (
            "ar_aging_snapshots", "ar_aging_rows", "customer")
    else:
        snap_table, rows_table, party_col = (
            "ap_aging_snapshots", "ap_aging_rows", "vendor")
    report_type = f"{result.side}_AGING"

    try:
        cur = conn.execute(
            """
            INSERT INTO uploads
                (report_type, as_of_date, filename, row_count, uploaded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (report_type, result.as_of_date, filename, result.row_count,
             _now()),
        )
        upload_id = cur.lastrowid

        existing = conn.execute(
            f"SELECT id FROM {snap_table} WHERE as_of_date = ?",
            (result.as_of_date,),
        ).fetchone()
        replaced = False
        deleted = 0
        if existing:
            snapshot_id = existing["id"]
            cur = conn.execute(
                f"DELETE FROM {rows_table} WHERE snapshot_id = ?",
                (snapshot_id,),
            )
            deleted = cur.rowcount
            conn.execute(
                f"UPDATE {snap_table} SET upload_id = ? WHERE id = ?",
                (upload_id, snapshot_id),
            )
            replaced = True
        else:
            cur = conn.execute(
                f"INSERT INTO {snap_table} (as_of_date, upload_id) "
                "VALUES (?, ?)",
                (result.as_of_date, upload_id),
            )
            snapshot_id = cur.lastrowid

        inserted = 0
        for bucket in result.buckets:
            conn.executemany(
                f"""
                INSERT INTO {rows_table}
                    (snapshot_id, {party_col}, invoice_date, due_date, num,
                     amount, open_balance, bucket)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot_id,
                        r.party,
                        r.txn_date,
                        r.due_date,
                        r.num,
                        r.amount,
                        r.open_balance,
                        bucket.name,
                    )
                    for r in bucket.rows
                ],
            )
            inserted += len(bucket.rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return AgingImportReport(
        upload_id=upload_id,
        snapshot_id=snapshot_id,
        replaced=replaced,
        inserted_count=inserted,
        deleted_count=deleted,
    )


def import_pairings(
    conn: sqlite3.Connection, result: PairingsParseResult, filename: str
) -> PairingsImportReport:
    """Replace-by-range for invoice/bill pairing rows, same pattern as
    import_gl: diff before deletion, supersede capture, one transaction."""
    if result.side == "AR":
        table, party_col, report_type = (
            "invoice_payments", "customer", "INVOICES_PAYMENTS")
    else:
        table, party_col, report_type = (
            "bills_payments", "vendor", "BILLS_PAYMENTS")

    period_start = result.period_start
    period_end = result.period_end
    all_dates = [r.txn_date for p in result.parties for r in p.rows]
    derived_period = False
    if period_start is None or period_end is None:
        if not all_dates:
            raise ValueError(
                "Pairings file has no period line and no rows — nothing to "
                "import."
            )
        period_start = period_start or min(all_dates)
        period_end = period_end or max(all_dates)
        derived_period = True

    def amount_key(amount):
        return round(amount, 2) if amount is not None else None

    try:
        cur = conn.execute(
            """
            INSERT INTO uploads
                (report_type, period_start, period_end, filename, row_count,
                 uploaded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (report_type, period_start, period_end, filename,
             result.row_count, _now()),
        )
        upload_id = cur.lastrowid
        report = PairingsImportReport(upload_id=upload_id)
        if derived_period:
            report.warnings.append(
                "Period line missing or partial; range derived from row "
                f"dates: {period_start}-{period_end}."
            )

        old_rows = conn.execute(
            f"""
            SELECT id, {party_col} AS party, row_type, date, num, amount,
                   group_key, upload_id
            FROM {table} WHERE date BETWEEN ? AND ?
            """,
            (period_start, period_end),
        ).fetchall()
        old_keys = Counter(
            (r["party"], r["date"], r["num"], amount_key(r["amount"]))
            for r in old_rows
        )
        new_keys = Counter(
            (p.name, r.txn_date, r.num, amount_key(r.amount))
            for p in result.parties
            for r in p.rows
        )
        report.diff = _compute_diff(old_keys, new_keys, "party")

        _capture_superseded(
            conn, upload_id, report_type, period_start, period_end, old_rows,
            entity=table,
        )

        retained = conn.execute(
            f"SELECT MAX(date) AS max_date FROM {table} WHERE date > ?",
            (period_end,),
        ).fetchone()
        if retained and retained["max_date"]:
            report.notes.append(
                f"Previous data through {retained['max_date']} retained; "
                f"this upload covers {period_start}-{period_end}."
            )

        cur = conn.execute(
            f"DELETE FROM {table} WHERE date BETWEEN ? AND ?",
            (period_start, period_end),
        )
        report.deleted_count = cur.rowcount
        for party in result.parties:
            conn.executemany(
                f"""
                INSERT INTO {table}
                    ({party_col}, row_type, date, num, amount, group_key,
                     upload_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        party.name,
                        r.row_type,
                        r.txn_date,
                        r.num,
                        r.amount,
                        r.group_key,
                        upload_id,
                    )
                    for r in party.rows
                ],
            )
            report.inserted_count += len(party.rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return report


def dormancy_pass(conn: sqlite3.Connection) -> list[int]:
    """Toggle dormancy flags; returns ids of accounts that changed.

    COA-sourced accounts (qbo_type set — GL stubs don't have one) with zero
    transactions go dormant; any account with transactions wakes up. Parent
    accounts (those with children) are structure, never flagged.
    """
    rows = conn.execute(
        "SELECT id, qbo_name, full_path, qbo_type, dormant FROM accounts"
    ).fetchall()
    paths = {r["full_path"] or r["qbo_name"] for r in rows}
    toggled: list[int] = []
    try:
        for r in rows:
            path = r["full_path"] or r["qbo_name"]
            if any(p.startswith(path + ":") for p in paths):
                continue
            txn_count = conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE account_id = ?",
                (r["id"],),
            ).fetchone()[0]
            if txn_count == 0 and r["qbo_type"] and not r["dormant"]:
                conn.execute(
                    "UPDATE accounts SET dormant = 1 WHERE id = ?", (r["id"],)
                )
                toggled.append(r["id"])
            elif txn_count > 0 and r["dormant"]:
                conn.execute(
                    "UPDATE accounts SET dormant = 0 WHERE id = ?", (r["id"],)
                )
                toggled.append(r["id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return toggled
