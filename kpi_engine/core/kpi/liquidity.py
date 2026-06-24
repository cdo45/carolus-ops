"""Liquidity KPIs.

compute_liquidity(conn, as_of=None) -> list[KPIValue]. as_of defaults to
the period end (latest transaction date). All liquidity KPIs share one
confidence rating, driven by the balance-sheet unmapped share.
"""

from __future__ import annotations

import calendar
import datetime as dt
import sqlite3

from core.kpi.base import (
    KPIValue,
    NOT_EXCLUDED,
    activity_sum,
    balance_as_of,
    confidence_for,
    load_beginning_balances,
    period_bounds,
    unmapped_share,
)

ASSET_CATEGORIES = ("CASH", "AR", "OCA")
LIABILITY_CATEGORIES = ("AP", "CC", "TAXL", "OCL")
OUTFLOW_WEEKS = 13


def _months_back(date: dt.date, n: int) -> dt.date:
    """Same day-of-month `n` calendar months earlier, clamped to month end."""
    month = date.month - n
    year = date.year
    while month <= 0:
        month += 12
        year -= 1
    day = min(date.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def _cash_outflow(conn: sqlite3.Connection, start: str, end: str) -> float:
    """Absolute sum of negative amounts (money leaving) in CASH-category
    accounts over [start, end]."""
    total = conn.execute(
        f"""
        SELECT COALESCE(SUM(t.amount), 0)
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE a.category = 'CASH' AND {NOT_EXCLUDED}
          AND t.amount < 0 AND t.txn_date BETWEEN ? AND ?
        """,
        (start, end),
    ).fetchone()[0]
    return round(abs(total), 2)


def compute_liquidity(
    conn: sqlite3.Connection, as_of: str | None = None
) -> list[KPIValue]:
    _, period_end = period_bounds(conn)
    as_of = as_of or period_end
    if as_of is None:
        raise ValueError("No transactions imported; nothing to compute.")

    beginnings = load_beginning_balances(conn)
    conf = confidence_for(unmapped_share(conn, "BS"))
    names = {
        r["id"]: r["qbo_name"]
        for r in conn.execute("SELECT id, qbo_name FROM accounts")
    }
    kpis: list[KPIValue] = []

    # --- cash_on_hand ------------------------------------------------------
    cash_total, cash_per_account = balance_as_of(
        conn, ["CASH"], as_of, beginnings
    )
    kpis.append(
        KPIValue(
            key="cash_on_hand",
            label="Cash on Hand",
            value=cash_total,
            unit="currency",
            confidence=conf,
            detail={names[i]: b for i, b in cash_per_account.items()},
        )
    )

    # --- weeks_of_cash ------------------------------------------------------
    as_of_date = dt.date.fromisoformat(as_of)
    window_start = (
        as_of_date - dt.timedelta(days=OUTFLOW_WEEKS * 7 - 1)
    ).isoformat()
    avg_weekly_outflow = round(
        _cash_outflow(conn, window_start, as_of) / OUTFLOW_WEEKS, 2
    )
    weeks = KPIValue(
        key="weeks_of_cash",
        label="Weeks of Cash",
        value=None,
        unit="weeks",
        confidence=conf,
        detail={
            "avg_weekly_outflow": avg_weekly_outflow,
            "window_start": window_start,
            "window_end": as_of,
        },
    )
    if avg_weekly_outflow > 0:
        weeks.value = round(cash_total / avg_weekly_outflow, 2)
    else:
        weeks.notes.append("no outflow history")
    kpis.append(weeks)

    # --- net_working_capital / current_ratio --------------------------------
    assets = {"CASH": cash_total}
    for category in ASSET_CATEGORIES[1:]:
        assets[category] = balance_as_of(
            conn, [category], as_of, beginnings
        )[0]
    liabilities = {
        category: balance_as_of(conn, [category], as_of, beginnings)[0]
        for category in LIABILITY_CATEGORIES
    }
    asset_total = round(sum(assets.values()), 2)
    liability_total = round(sum(liabilities.values()), 2)
    kpis.append(
        KPIValue(
            key="net_working_capital",
            label="Net Working Capital",
            value=round(asset_total - liability_total, 2),
            unit="currency",
            confidence=conf,
            detail={
                "assets": assets,
                "liabilities": liabilities,
                "as_of": as_of,
            },
        )
    )
    ratio = KPIValue(
        key="current_ratio",
        label="Current Ratio",
        value=None,
        unit="ratio",
        confidence=conf,
        detail={"assets": asset_total, "liabilities": liability_total},
    )
    if liability_total != 0:
        ratio.value = round(asset_total / liability_total, 4)
    else:
        ratio.notes.append("no current liabilities; ratio undefined")
    kpis.append(ratio)

    # --- card_debt_load ------------------------------------------------------
    revenue_window_start = (
        _months_back(as_of_date, 3) + dt.timedelta(days=1)
    ).isoformat()
    avg_monthly_revenue = round(
        abs(activity_sum(conn, ["REV"], revenue_window_start, as_of)) / 3, 2
    )
    card = KPIValue(
        key="card_debt_load",
        label="Card Debt Load",
        value=None,
        unit="months",
        confidence=conf,
        detail={
            "cc_balance": liabilities["CC"],
            "avg_monthly_revenue": avg_monthly_revenue,
            "window_start": revenue_window_start,
            "window_end": as_of,
        },
    )
    if avg_monthly_revenue > 0:
        card.value = round(liabilities["CC"] / avg_monthly_revenue, 2)
    else:
        card.notes.append("no revenue in the trailing 3 months")
    kpis.append(card)

    # --- quick_burn_check ----------------------------------------------------
    four_weeks_outflow = round(avg_weekly_outflow * 4, 2)
    quick_burn = round(
        (cash_total + assets["AR"])
        - (liabilities["AP"] + liabilities["CC"] + four_weeks_outflow),
        2,
    )
    kpis.append(
        KPIValue(
            key="quick_burn_check",
            label="Quick Burn Check",
            value=quick_burn,
            unit="currency",
            confidence=conf,
            detail={
                "cash": cash_total,
                "ar": assets["AR"],
                "ap": liabilities["AP"],
                "cc": liabilities["CC"],
                "four_weeks_outflow": four_weeks_outflow,
            },
            notes=[
                "positive: cash plus receivables cover payables, cards, and "
                "four weeks of outflow"
                if quick_burn >= 0
                else "negative: cash plus receivables fall short of payables, "
                "cards, and four weeks of outflow"
            ],
        )
    )
    return kpis
