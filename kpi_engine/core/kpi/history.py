"""GL-backfilled monthly history for KPI trend charts.

The General Ledger carries dated transactions across the whole period, so we
can reconstruct each metric at every past month-end — a real historical trend
straight from the books, available on the very first dashboard.

Balance-sheet metrics are reconstructed as point-in-time balances. Flow
metrics (margin, overhead, collection effectiveness, DSO) are computed on a
trailing-quarter (3-month) rolling window and only emitted once a full window
of data exists, so early, partial-window months don't show misleading values.
"""

from __future__ import annotations

import calendar
import datetime as dt
import sqlite3
from collections import defaultdict

from core.kpi.base import (
    NOT_EXCLUDED,
    activity_sum,
    balance_as_of,
    load_beginning_balances,
    period_bounds,
)

_LIABILITIES = ("AP", "CC", "TAXL", "OCL")
_DIRECT = ("DL", "SUB", "DMAT")
_OVERHEAD = ("OH", "OH-PAY", "OH-INS", "OH-OCC")
_ROLL_MONTHS = 3


def _month_ends(start_iso: str, end_iso: str) -> list[str]:
    start = dt.date.fromisoformat(start_iso)
    end = dt.date.fromisoformat(end_iso)
    out: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        last = calendar.monthrange(year, month)[1]
        out.append(min(dt.date(year, month, last), end).isoformat())
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return out


def _months_back(d: dt.date, n: int) -> dt.date:
    month, year = d.month - n, d.year
    while month <= 0:
        month += 12
        year -= 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def _ar_flow(conn, start: str, end: str) -> tuple[float, float]:
    """(new billings, collected) from GL A/R activity over [start, end]."""
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END), 0)
                   AS billed,
               COALESCE(SUM(CASE WHEN t.amount < 0 THEN t.amount ELSE 0 END), 0)
                   AS collected
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE a.category = 'AR' AND {NOT_EXCLUDED}
          AND t.txn_date BETWEEN ? AND ?
        """,
        (start, end),
    ).fetchone()
    return row["billed"], abs(row["collected"])


def _ar_pairs_with_dates(conn) -> list[tuple[str, float, int]]:
    """(payment_date, invoice_amount, lag_days) from the report's own grouping
    of payments to the invoices they settled."""
    rows = conn.execute(
        "SELECT customer, row_type, date, amount, group_key FROM invoice_payments"
    ).fetchall()
    clusters: dict = defaultdict(lambda: {"inv": [], "pay": []})
    for r in rows:
        if r["date"] is None or r["amount"] is None:
            continue
        c = clusters[(r["customer"], r["group_key"] or "")]
        if r["row_type"] == "invoice":
            c["inv"].append((r["date"], abs(r["amount"])))
        elif r["row_type"] == "payment":
            c["pay"].append((r["date"], abs(r["amount"])))
    pairs: list[tuple[str, float, int]] = []
    for c in clusters.values():
        if c["pay"] and c["inv"]:
            pay_date = max(d for d, _ in c["pay"])
            pd = dt.date.fromisoformat(pay_date)
            for inv_date, amount in c["inv"]:
                lag = (pd - dt.date.fromisoformat(inv_date)).days
                if lag >= 0:
                    pairs.append((pay_date, amount, lag))
    return pairs


def monthly_history(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Reconstruct trendable KPIs at each month-end in the GL period."""
    start, end = period_bounds(conn)
    if not start or not end:
        return {}
    beginnings = load_beginning_balances(conn)
    ar_pairs = _ar_pairs_with_dates(conn)
    series: dict[str, list[dict]] = defaultdict(list)

    for month_iso in _month_ends(start, end):
        m = dt.date.fromisoformat(month_iso)

        # --- balance-sheet positions (every month) -----------------------
        cash = balance_as_of(conn, ["CASH"], month_iso, beginnings)[0]
        ar = balance_as_of(conn, ["AR"], month_iso, beginnings)[0]
        oca = balance_as_of(conn, ["OCA"], month_iso, beginnings)[0]
        cc = balance_as_of(conn, ["CC"], month_iso, beginnings)[0]
        assets = cash + ar + oca
        liabilities = sum(
            balance_as_of(conn, [c], month_iso, beginnings)[0]
            for c in _LIABILITIES
        )
        series["cash_on_hand"].append({"date": month_iso, "value": cash})
        series["total_ar"].append({"date": month_iso, "value": ar})
        series["net_working_capital"].append(
            {"date": month_iso, "value": round(assets - liabilities, 2)})
        if liabilities:
            series["current_ratio"].append(
                {"date": month_iso, "value": round(assets / liabilities, 4)})

        # --- rolling trailing-quarter window -----------------------------
        window_start = (_months_back(m, _ROLL_MONTHS)
                        + dt.timedelta(days=1)).isoformat()
        revenue = abs(activity_sum(conn, ["REV"], window_start, month_iso))
        if revenue > 0:
            if cc > 0:
                series["card_debt_load"].append(
                    {"date": month_iso,
                     "value": round(cc / (revenue / _ROLL_MONTHS), 2)})
            # P&L ratios only once the full window has data.
            if dt.date.fromisoformat(window_start) >= dt.date.fromisoformat(start):
                direct = abs(activity_sum(conn, list(_DIRECT),
                                          window_start, month_iso))
                labor = abs(activity_sum(conn, ["DL", "SUB"],
                                         window_start, month_iso))
                overhead = abs(activity_sum(conn, list(_OVERHEAD),
                                            window_start, month_iso))
                margin = (revenue - direct) / revenue
                burn = round(overhead / _ROLL_MONTHS, 2)
                series["gross_margin"].append(
                    {"date": month_iso, "value": round(margin, 4)})
                series["direct_labor_pct"].append(
                    {"date": month_iso, "value": round(labor / revenue * 100, 2)})
                series["overhead_burn"].append(
                    {"date": month_iso, "value": burn})
                if margin > 0:
                    series["breakeven_revenue"].append(
                        {"date": month_iso, "value": round(burn / margin, 2)})

        # --- rolling collection effectiveness + DSO ----------------------
        if dt.date.fromisoformat(window_start) >= dt.date.fromisoformat(start):
            prior_day = (dt.date.fromisoformat(window_start)
                         - dt.timedelta(days=1)).isoformat()
            beginning_ar = balance_as_of(conn, ["AR"], prior_day, beginnings)[0]
            billed, collected = _ar_flow(conn, window_start, month_iso)
            denom = beginning_ar + billed
            if denom > 0:
                series["collection_effectiveness"].append(
                    {"date": month_iso, "value": round(collected / denom, 4)})

        window = [(amt, lag) for pd, amt, lag in ar_pairs
                  if window_start <= pd <= month_iso]
        weight = sum(a for a, _ in window)
        if weight > 0:
            series["dso_true"].append(
                {"date": month_iso,
                 "value": round(sum(a * l for a, l in window) / weight, 2)})

    return dict(series)
