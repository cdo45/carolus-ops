"""Disbursement KPIs.

compute_disbursements(conn) -> list[KPIValue], over the full imported period.

A client is one of two archetypes, recorded in every KPI's detail:
  * "ap_driven" — runs an accounts-payable cycle: bills_payments rows exist or
    the latest A/P aging snapshot is non-empty. Vendor-cycle KPIs apply.
  * "direct_pay" — pays vendors straight from cash with no A/P ledger. The
    A/P-only KPIs (open_ap_due, vendor_pay_lag, job_cash_demand) are omitted
    and vendor concentration is inferred from cash outflows by payee.

recurring_outflow_base and the cash-side KPIs (payroll_load,
card_cycle_exposure) compute for both archetypes.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import statistics
from collections import defaultdict

from core.kpi.base import (
    KPIValue,
    NOT_EXCLUDED,
    balance_as_of,
    load_beginning_balances,
    period_bounds,
)
from core.kpi.receivables import match_pairings
from core.parsers.common import extract_job_prefix

PAYROLL_KEYWORDS = (
    "payroll",
    "paychex",
    "adp",
    "gusto",
    "paycheck",
    "dir dep",
    "direct deposit",
)

# (cadence, low_days, high_days, monthly multiplier). Bands are disjoint.
_CADENCE_BANDS = (
    ("weekly", 5, 9, 4.33),
    ("biweekly", 12, 16, 2.17),
    ("monthly", 27, 34, 1.0),
)
RECUR_MIN_OCCURRENCES = 3
RECUR_BAND_SHARE = 0.60
AMOUNT_TOLERANCE = 0.10


def _archetype(conn: sqlite3.Connection) -> str:
    has_bills = (
        conn.execute("SELECT 1 FROM bills_payments LIMIT 1").fetchone()
        is not None
    )
    latest_ap = conn.execute(
        "SELECT id FROM ap_aging_snapshots ORDER BY as_of_date DESC, id DESC "
        "LIMIT 1"
    ).fetchone()
    ap_nonempty = latest_ap is not None and (
        conn.execute(
            "SELECT 1 FROM ap_aging_rows WHERE snapshot_id = ? LIMIT 1",
            (latest_ap["id"],),
        ).fetchone()
        is not None
    )
    return "ap_driven" if (has_bills or ap_nonempty) else "direct_pay"


def _latest_ap_snapshot(
    conn: sqlite3.Connection,
) -> tuple[int | None, str | None]:
    row = conn.execute(
        "SELECT id, as_of_date FROM ap_aging_snapshots "
        "ORDER BY as_of_date DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None, None
    return row["id"], row["as_of_date"]


def _amount_clusters(items: list[tuple[str, float]]) -> list[list[tuple[str, float]]]:
    """Greedy ±10% amount clusters within a (name, type) group. Each incoming
    amount joins the first cluster within tolerance of its running mean."""
    clusters: list[dict] = []
    for date, amount in sorted(items, key=lambda x: x[1]):
        for c in clusters:
            mean = c["sum"] / c["n"]
            if mean and abs(amount - mean) <= AMOUNT_TOLERANCE * mean:
                c["items"].append((date, amount))
                c["sum"] += amount
                c["n"] += 1
                break
        else:
            clusters.append({"items": [(date, amount)], "sum": amount, "n": 1})
    return [c["items"] for c in clusters]


def _detect_cadence(dates: list[str]) -> str | None:
    """Cadence label when >= 60% of consecutive-date intervals fall in one
    band, else None. Caller guarantees >= 3 dates."""
    ordered = sorted(dt.date.fromisoformat(d) for d in dates)
    intervals = [
        (ordered[i + 1] - ordered[i]).days for i in range(len(ordered) - 1)
    ]
    if not intervals:
        return None
    for cadence, low, high, _ in _CADENCE_BANDS:
        in_band = sum(1 for gap in intervals if low <= gap <= high)
        if in_band / len(intervals) >= RECUR_BAND_SHARE:
            return cadence
    return None


def _cash_negative_rows(conn: sqlite3.Connection, start: str, end: str):
    return conn.execute(
        f"""
        SELECT t.name, t.txn_type, t.split, t.description, t.txn_date,
               t.amount
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE a.category = 'CASH' AND {NOT_EXCLUDED}
          AND t.amount < 0 AND t.txn_date BETWEEN ? AND ?
        """,
        (start, end),
    ).fetchall()


def compute_disbursements(conn: sqlite3.Connection) -> list[KPIValue]:
    start, end = period_bounds(conn)
    today = dt.date.today()
    as_of = end or today.isoformat()
    start = start or as_of
    beginnings = load_beginning_balances(conn)
    archetype = _archetype(conn)
    ap_driven = archetype == "ap_driven"

    def detail(extra: dict | None = None) -> dict:
        base = {"archetype": archetype}
        if extra:
            base.update(extra)
        return base

    kpis: list[KPIValue] = []
    snapshot_id, snap_as_of = _latest_ap_snapshot(conn)
    ap_rows = (
        conn.execute(
            "SELECT vendor, due_date, num, open_balance FROM ap_aging_rows "
            "WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchall()
        if snapshot_id is not None
        else []
    )

    # --- open_ap_due (AP-driven only) ----------------------------------------
    if ap_driven and snapshot_id is not None and snap_as_of:
        as_of_date = dt.date.fromisoformat(snap_as_of)
        total_open = due_14 = due_30 = 0.0
        by_vendor: dict[str, float] = defaultdict(float)
        for r in ap_rows:
            open_bal = r["open_balance"] or 0.0
            total_open += open_bal
            by_vendor[r["vendor"]] += open_bal
            if r["due_date"]:
                days_to_due = (
                    dt.date.fromisoformat(r["due_date"]) - as_of_date
                ).days
                if days_to_due <= 14:
                    due_14 += open_bal
                if days_to_due <= 30:
                    due_30 += open_bal
        kpis.append(
            KPIValue(
                key="open_ap_due",
                label="Open A/P Due",
                value=round(total_open, 2),
                unit="currency",
                confidence="high",
                detail=detail(
                    {
                        "due_within_14": round(due_14, 2),
                        "due_within_30": round(due_30, 2),
                        "by_vendor": {
                            v: round(amt, 2) for v, amt in by_vendor.items()
                        },
                    }
                ),
            )
        )

    # --- vendor_pay_lag (AP-driven only) -------------------------------------
    if ap_driven:
        matches, matched_count, unmatched_count = match_pairings(
            conn, "bills_payments", "vendor", ("payment", "credit")
        )
        all_matched = [pair for lst in matches.values() for pair in lst]
        total_amt = sum(a for a, _ in all_matched)
        value = (
            round(sum(a * lag for a, lag in all_matched) / total_amt, 2)
            if total_amt
            else None
        )
        by_vendor_lag = {
            vendor: round(sum(lag for _, lag in lst) / len(lst), 2)
            for vendor, lst in matches.items()
            if lst
        }
        kpis.append(
            KPIValue(
                key="vendor_pay_lag",
                label="Vendor Payment Lag",
                value=value,
                unit="weeks",  # days, in the shared duration channel
                confidence="high",
                detail=detail(
                    {
                        "unit_is": "days",
                        "by_vendor": by_vendor_lag,
                        "matched_count": matched_count,
                        "unmatched_count": unmatched_count,
                    }
                ),
            )
        )

    # --- top5_vendor_concentration -------------------------------------------
    if ap_driven:
        rows = conn.execute(
            """
            SELECT vendor AS name, SUM(ABS(amount)) AS total
            FROM bills_payments WHERE row_type = 'invoice'
            GROUP BY vendor
            """
        ).fetchall()
        basis = "bill amounts"
    else:
        rows = conn.execute(
            f"""
            SELECT t.name AS name, SUM(ABS(t.amount)) AS total
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE a.category = 'CASH' AND {NOT_EXCLUDED}
              AND t.amount < 0 AND t.name IS NOT NULL
              AND lower(COALESCE(t.txn_type, '')) != 'transfer'
              AND t.txn_date BETWEEN ? AND ?
            GROUP BY t.name
            """,
            (start, end),
        ).fetchall()
        basis = "cash outflow by payee"
    grand = sum(r["total"] for r in rows)
    top = sorted(rows, key=lambda r: r["total"], reverse=True)[:5]
    concentration = KPIValue(
        key="top5_vendor_concentration",
        label="Top-5 Vendor Concentration",
        value=round(top[0]["total"] / grand * 100, 2) if grand else None,
        unit="percent",
        confidence="high" if ap_driven else "medium",
        detail=detail(
            {
                "basis": basis,
                "by_vendor": {
                    r["name"]: round(r["total"] / grand * 100, 2) for r in top
                }
                if grand
                else {},
            }
        ),
    )
    if not grand:
        concentration.notes.append("no vendor outflows in the period")
    kpis.append(concentration)

    # --- recurring_outflow_base (both archetypes) ----------------------------
    groups: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    for r in _cash_negative_rows(conn, start, end):
        if not r["name"]:
            continue
        groups[(r["name"], r["txn_type"] or "")].append(
            (r["txn_date"], abs(r["amount"]))
        )
    recurring: list[dict] = []
    monthly_total = 0.0
    for (name, _type), items in groups.items():
        for cluster in _amount_clusters(items):
            if len(cluster) < RECUR_MIN_OCCURRENCES:
                continue
            cadence = _detect_cadence([d for d, _ in cluster])
            if cadence is None:
                continue
            multiplier = next(m for c, _, _, m in _CADENCE_BANDS if c == cadence)
            typical = statistics.mean(a for _, a in cluster)
            monthly_equiv = round(typical * multiplier, 2)
            monthly_total += monthly_equiv
            recurring.append(
                {
                    "payee": name,
                    "cadence": cadence,
                    "typical_amount": round(typical, 2),
                    "monthly_equiv": monthly_equiv,
                    "n_seen": len(cluster),
                }
            )
    kpis.append(
        KPIValue(
            key="recurring_outflow_base",
            label="Recurring Outflow (monthly base)",
            value=round(monthly_total, 2),
            unit="currency",
            confidence="high",
            detail=detail({"recurring": recurring}),
        )
    )

    # --- payroll_load --------------------------------------------------------
    payroll_account_names = {
        (r["qbo_name"] or "").strip().lower()
        for r in conn.execute(
            "SELECT qbo_name FROM accounts WHERE category IN ('OH-PAY', 'DL')"
        )
    }
    payroll_total = total_outflow = 0.0
    payroll_keyword_hits = payroll_split_hits = 0
    for r in _cash_negative_rows(conn, start, end):
        amount = abs(r["amount"])
        total_outflow += amount
        text = f"{r['name'] or ''} {r['description'] or ''}".lower()
        keyword = any(k in text for k in PAYROLL_KEYWORDS)
        split_leaf = (r["split"] or "").split(":")[-1].strip().lower()
        split_hit = split_leaf in payroll_account_names and bool(split_leaf)
        if keyword or split_hit:
            payroll_total += amount
            payroll_keyword_hits += int(keyword)
            payroll_split_hits += int(split_hit and not keyword)
    period_weeks = max(1.0, (_days_span(start, end) + 1) / 7)
    payroll = KPIValue(
        key="payroll_load",
        label="Payroll Load",
        value=round(payroll_total / total_outflow, 4) if total_outflow else None,
        unit="percent",
        confidence="high",
        detail=detail(
            {
                "weekly_payroll_avg": round(payroll_total / period_weeks, 2),
                "match_basis": {
                    "keyword": payroll_keyword_hits,
                    "split": payroll_split_hits,
                },
            }
        ),
    )
    if not total_outflow:
        payroll.notes.append("no cash outflow in the period")
    kpis.append(payroll)

    # --- card_cycle_exposure -------------------------------------------------
    cc_accounts = conn.execute(
        f"SELECT a.id, a.qbo_name FROM accounts a "
        f"WHERE a.category = 'CC' AND {NOT_EXCLUDED}"
    ).fetchall()
    cc_total = 0.0
    cc_detail: dict[str, dict] = {}
    for acct in cc_accounts:
        balance = balance_as_of(conn, ["CC"], as_of, beginnings)[1].get(
            acct["id"], beginnings.get(acct["id"], 0.0)
        )
        cc_total += balance
        payment_days = [
            dt.date.fromisoformat(r["txn_date"]).day
            for r in conn.execute(
                "SELECT txn_date FROM transactions "
                "WHERE account_id = ? AND amount < 0 AND txn_date <= ?",
                (acct["id"], as_of),
            )
        ]
        cc_detail[acct["qbo_name"]] = {
            "balance": round(balance, 2),
            "payment_count": len(payment_days),
            "cycle_anchor_day": int(statistics.median(payment_days))
            if payment_days
            else None,
        }
    kpis.append(
        KPIValue(
            key="card_cycle_exposure",
            label="Card Cycle Exposure",
            value=round(cc_total, 2),
            unit="currency",
            confidence="high",
            detail=detail({"by_card": cc_detail}),
        )
    )

    # --- job_cash_demand (AP-driven, only with job prefixes) -----------------
    if ap_driven:
        by_job: dict[str, float] = defaultdict(float)
        for r in ap_rows:
            prefix = extract_job_prefix(r["num"])
            if prefix:
                by_job[prefix] += r["open_balance"] or 0.0
        if by_job:
            kpis.append(
                KPIValue(
                    key="job_cash_demand",
                    label="Job Cash Demand (open A/P by job)",
                    value=round(sum(by_job.values()), 2),
                    unit="currency",
                    confidence="high",
                    detail=detail(
                        {"by_job": {j: round(v, 2) for j, v in by_job.items()}}
                    ),
                )
            )

    return kpis


def _days_span(start: str, end: str) -> int:
    return (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days
