"""Revenue and margin KPIs.

compute_revenue(conn) -> list[KPIValue], over the full imported period.

Sign conventions: in QBO GL exports, income-account activity that increases
income is negative in some exports and positive in others depending on
report settings. Revenue is therefore computed as ABS(sum of REV activity),
with the direction verified by majority sign and recorded in
detail["sign_convention"]. The same convention applies to OI, and expense
categories use their natural sign (ABS of the category sum).
"""

from __future__ import annotations

import calendar
import sqlite3

from core.kpi.base import (
    KPIValue,
    NOT_EXCLUDED,
    activity_sum,
    confidence_for,
    period_bounds,
    unmapped_share,
)

DIRECT_CATEGORIES = ("DL", "SUB", "DMAT")
# DEP (non-cash) and TAXE are deliberately NOT overhead burn.
OVERHEAD_CATEGORIES = ("OH", "OH-PAY", "OH-INS", "OH-OCC")

_CONFIDENCE_DROP = {"high": "medium", "medium": "low", "low": "low"}


def _month_range(start: str, end: str) -> list[str]:
    """Calendar months "YYYY-MM" from start's month through end's month."""
    year, month = int(start[:4]), int(start[5:7])
    end_year, end_month = int(end[:4]), int(end[5:7])
    months = []
    while (year, month) <= (end_year, end_month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            month = 1
            year += 1
    return months


def _monthly_sums(
    conn: sqlite3.Connection, categories: list[str], start: str, end: str
) -> dict[str, float]:
    """Signed per-month activity sums, zero-filled across the month range."""
    qmarks = ",".join("?" * len(categories))
    rows = conn.execute(
        f"""
        SELECT substr(t.txn_date, 1, 7) AS month, SUM(t.amount) AS total
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE a.category IN ({qmarks}) AND {NOT_EXCLUDED}
          AND t.txn_date BETWEEN ? AND ?
        GROUP BY month
        """,
        [*categories, start, end],
    ).fetchall()
    sums = {m: 0.0 for m in _month_range(start, end)}
    for r in rows:
        sums[r["month"]] = round(r["total"], 2)
    return sums


def _sign_convention(
    conn: sqlite3.Connection, start: str, end: str
) -> str:
    """Majority sign of REV transaction amounts: "negative" when income
    appears as negative amounts (ties included), else "positive"."""
    row = conn.execute(
        f"""
        SELECT SUM(CASE WHEN t.amount < 0 THEN 1 ELSE 0 END) AS neg,
               SUM(CASE WHEN t.amount > 0 THEN 1 ELSE 0 END) AS pos
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE a.category = 'REV' AND {NOT_EXCLUDED}
          AND t.txn_date BETWEEN ? AND ?
        """,
        (start, end),
    ).fetchone()
    return "negative" if (row["neg"] or 0) >= (row["pos"] or 0) else "positive"


def compute_revenue(conn: sqlite3.Connection) -> list[KPIValue]:
    start, end = period_bounds(conn)
    if start is None:
        raise ValueError("No transactions imported; nothing to compute.")

    base_conf = confidence_for(unmapped_share(conn, "PL"))
    kpis: list[KPIValue] = []

    convention = _sign_convention(conn, start, end)
    sign = -1.0 if convention == "negative" else 1.0
    revenue = round(abs(activity_sum(conn, ["REV"], start, end)), 2)
    monthly_revenue = {
        month: round(sign * total, 2)
        for month, total in _monthly_sums(conn, ["REV"], start, end).items()
    }

    # --- revenue_t12 ---------------------------------------------------------
    kpis.append(
        KPIValue(
            key="revenue_t12",
            label="Revenue (trailing 12 months)",
            value=revenue,
            unit="currency",
            confidence=base_conf,
            detail={
                "monthly": monthly_revenue,
                "sign_convention": convention,
            },
        )
    )

    # --- revenue_trend_3mo ---------------------------------------------------
    full_months = [
        month
        for month in monthly_revenue
        if f"{month}-01" >= start
        and (
            f"{month}-"
            f"{calendar.monthrange(int(month[:4]), int(month[5:7]))[1]:02d}"
        )
        <= end
    ]
    trend = KPIValue(
        key="revenue_trend_3mo",
        label="Revenue Trend (3-month)",
        value=None,
        unit="percent",
        confidence=base_conf,
    )
    if len(full_months) < 6:
        trend.notes.append("fewer than 6 full months of data")
    else:
        last_3, prior_3 = full_months[-3:], full_months[-6:-3]
        last_avg = round(sum(monthly_revenue[m] for m in last_3) / 3, 2)
        prior_avg = round(sum(monthly_revenue[m] for m in prior_3) / 3, 2)
        trend.detail = {
            "last_3_months": last_3,
            "prior_3_months": prior_3,
            "last_avg": last_avg,
            "prior_avg": prior_avg,
        }
        if prior_avg == 0:
            trend.notes.append("prior 3 months have no revenue")
        else:
            trend.value = round((last_avg - prior_avg) / prior_avg * 100, 2)
    kpis.append(trend)

    # --- revenue_mix ---------------------------------------------------------
    mix_rows = conn.execute(
        f"""
        SELECT a.qbo_name, SUM(t.amount) AS total
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE a.category = 'REV' AND {NOT_EXCLUDED}
          AND t.txn_date BETWEEN ? AND ?
        GROUP BY a.id
        """,
        (start, end),
    ).fetchall()
    mix = KPIValue(
        key="revenue_mix",
        label="Revenue Mix",
        value=None,
        unit="percent",
        confidence=base_conf,
        detail={
            r["qbo_name"]: round(abs(r["total"]) / revenue * 100, 2)
            for r in mix_rows
        }
        if revenue
        else {},
    )
    if not revenue:
        mix.notes.append("no revenue in the period")
    kpis.append(mix)

    # --- gross_margin --------------------------------------------------------
    direct = round(
        abs(activity_sum(conn, list(DIRECT_CATEGORIES), start, end)), 2
    )
    margin_conf = base_conf
    margin_notes: list[str] = []
    missing = [
        category
        for category in DIRECT_CATEGORIES
        if conn.execute(
            f"SELECT COUNT(*) FROM accounts a "
            f"WHERE a.category = ? AND {NOT_EXCLUDED}",
            (category,),
        ).fetchone()[0]
        == 0
    ]
    unmapped_cogs = conn.execute(
        f"""
        SELECT COUNT(*) FROM accounts a
        WHERE lower(COALESCE(a.qbo_type, '')) = 'cost of goods sold'
          AND a.category IS NULL AND {NOT_EXCLUDED}
        """
    ).fetchone()[0]
    if missing and unmapped_cogs:
        margin_conf = _CONFIDENCE_DROP[margin_conf]
        margin_notes.append(
            f"{', '.join(missing)} have no mapped accounts while "
            f"{unmapped_cogs} COGS-type account(s) sit unmapped — direct "
            "costs may be understated."
        )
    margin = KPIValue(
        key="gross_margin",
        label="Gross Margin",
        value=None,
        unit="ratio",
        confidence=margin_conf,
        detail={"revenue": revenue, "direct_costs": direct},
        notes=margin_notes,
    )
    if revenue:
        margin.value = round((revenue - direct) / revenue, 4)
    else:
        margin.notes.append("no revenue in the period")
    kpis.append(margin)

    # --- direct_labor_pct ----------------------------------------------------
    labor = round(abs(activity_sum(conn, ["DL", "SUB"], start, end)), 2)
    labor_pct = KPIValue(
        key="direct_labor_pct",
        label="Direct Labor %",
        value=None,
        unit="percent",
        confidence=base_conf,
        detail={"direct_labor": labor, "revenue": revenue},
    )
    if revenue:
        labor_pct.value = round(labor / revenue * 100, 2)
    else:
        labor_pct.notes.append("no revenue in the period")
    kpis.append(labor_pct)

    # --- overhead_burn -------------------------------------------------------
    overhead_total = round(
        abs(activity_sum(conn, list(OVERHEAD_CATEGORIES), start, end)), 2
    )
    months_in_period = len(_month_range(start, end))
    burn_value = round(overhead_total / months_in_period, 2)
    kpis.append(
        KPIValue(
            key="overhead_burn",
            label="Overhead Burn (monthly)",
            value=burn_value,
            unit="currency",
            confidence=base_conf,
            detail={
                "monthly": {
                    month: round(abs(total), 2)
                    for month, total in _monthly_sums(
                        conn, list(OVERHEAD_CATEGORIES), start, end
                    ).items()
                },
                "by_category": {
                    category: round(
                        abs(activity_sum(conn, [category], start, end)), 2
                    )
                    for category in OVERHEAD_CATEGORIES
                },
                "months": months_in_period,
            },
            notes=["excludes DEP (non-cash) and TAXE"],
        )
    )

    # --- breakeven_revenue ---------------------------------------------------
    breakeven = KPIValue(
        key="breakeven_revenue",
        label="Breakeven Revenue (monthly)",
        value=None,
        unit="currency",
        confidence=margin_conf,
        detail={"overhead_burn": burn_value, "gross_margin": margin.value},
    )
    if margin.value is not None and margin.value > 0:
        breakeven.value = round(burn_value / margin.value, 2)
    else:
        breakeven.notes.append(
            "gross margin unavailable or non-positive; breakeven undefined"
        )
    kpis.append(breakeven)

    # --- revenue_per_job (only when job prefixes exist) ----------------------
    if conn.execute(
        "SELECT 1 FROM transactions WHERE job_prefix IS NOT NULL LIMIT 1"
    ).fetchone():
        job_rows = conn.execute(
            f"""
            SELECT t.job_prefix, SUM(t.amount) AS total
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE a.category = 'REV' AND t.job_prefix IS NOT NULL
              AND {NOT_EXCLUDED} AND t.txn_date BETWEEN ? AND ?
            GROUP BY t.job_prefix
            """,
            (start, end),
        ).fetchall()
        kpis.append(
            KPIValue(
                key="revenue_per_job",
                label="Revenue per Job",
                value=None,
                unit="currency",
                confidence=base_conf,
                detail={
                    r["job_prefix"]: round(abs(r["total"]), 2)
                    for r in job_rows
                },
            )
        )

    return kpis
