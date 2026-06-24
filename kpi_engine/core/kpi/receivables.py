"""Receivables KPIs.

compute_receivables(conn) -> list[KPIValue], over the full imported period.

Three data sources feed these metrics:
  * the latest A/R aging snapshot (by as_of_date) — drives aging distribution,
    at-risk, and the 13-week collection forecast;
  * invoice_payments pairing rows — drive true DSO, per-customer payment lag,
    and customer concentration via amount-based matching;
  * GL A/R activity + beginning balances — drive total A/R and collection
    effectiveness.

Confidence keys off the aging snapshot: "high" when one exists and is <= 35
days old, "medium" when older, "low" when none exists (the snapshot-derived
KPIs then return value None with an explanatory note).
"""

from __future__ import annotations

import datetime as dt
import itertools
import math
import sqlite3
from collections import defaultdict

from core.kpi.base import (
    KPIValue,
    NOT_EXCLUDED,
    activity_sum,
    balance_as_of,
    load_beginning_balances,
    period_bounds,
)

# Aging bucket -> (collection haircut, extra day shift on the expected date).
# 31-60 carries a two-week shift on top of the customer's lag; the deeper
# buckets discount the dollars rather than the date.
_BUCKET_FACTOR: dict[str, tuple[float, int]] = {
    "CURRENT": (1.0, 0),
    "1 - 30 days past due": (1.0, 0),
    "31 - 60 days past due": (1.0, 14),
    "61 - 90 days past due": (0.75, 0),
    "91 or more days past due": (0.50, 0),
}
AT_RISK_BUCKET = "91 or more days past due"
PARTY_ROW_CAP = 200
FORECAST_WEEKS = 13
STALE_DAYS = 35

# Already-overdue invoices won't all be collected next week. We spread each
# bucket's expected (haircut-adjusted) amount evenly across a recovery window —
# recent overdue lands soon, stale overdue dribbles in later.
_RECOVERY_WINDOW: dict[str, tuple[int, int]] = {
    "CURRENT": (1, 2),
    "1 - 30 days past due": (1, 3),
    "31 - 60 days past due": (1, 4),
    "61 - 90 days past due": (2, 6),
    "91 or more days past due": (4, 13),
}


def _days(later_iso: str, earlier_iso: str) -> int:
    return (
        dt.date.fromisoformat(later_iso) - dt.date.fromisoformat(earlier_iso)
    ).days


def _median(values: list[float]) -> float | None:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def _find_combo(avail: list[dict], target: float) -> tuple | None:
    """First combination of 2-4 available invoices summing to `target`."""
    for size in (2, 3, 4):
        if len(avail) < size:
            break
        for combo in itertools.combinations(avail, size):
            if abs(sum(i["amount"] for i in combo) - target) < 0.005:
                return combo
    return None


def _match_party(
    invoices: list[tuple[str, float]], payments: list[tuple[str, float]]
) -> tuple[list[tuple[float, int]], int]:
    """Match a party's invoices to its payments by amount.

    Exact one-to-one first (nearest payment date wins on ties), then payments
    equal to the sum of 2-4 still-unmatched invoices. Returns the list of
    (invoice_amount, lag_days) for matched invoices and a count of those left
    unmatched. Parties exceeding PARTY_ROW_CAP rows are skipped wholesale.
    """
    if len(invoices) + len(payments) > PARTY_ROW_CAP:
        return [], len(invoices)
    inv = [{"date": d, "amount": a, "used": False} for d, a in invoices]
    pay = [{"date": d, "amount": a, "used": False} for d, a in payments]
    matched: list[tuple[float, int]] = []

    for p in pay:
        cands = [
            i
            for i in inv
            if not i["used"] and abs(i["amount"] - p["amount"]) < 0.005
        ]
        if not cands:
            continue
        cands.sort(key=lambda i: abs(_days(p["date"], i["date"])))
        chosen = cands[0]
        chosen["used"] = True
        p["used"] = True
        matched.append((chosen["amount"], _days(p["date"], chosen["date"])))

    for p in pay:
        if p["used"]:
            continue
        avail = sorted(
            (i for i in inv if not i["used"]), key=lambda i: i["date"]
        )
        combo = _find_combo(avail, p["amount"])
        if combo:
            for i in combo:
                i["used"] = True
                matched.append((i["amount"], _days(p["date"], i["date"])))
            p["used"] = True

    unmatched = sum(1 for i in inv if not i["used"])
    return matched, unmatched


def match_pairings(
    conn: sqlite3.Connection,
    table: str,
    party_col: str,
    payment_types: tuple[str, ...],
) -> tuple[dict[str, list[tuple[float, int]]], int, int]:
    """Pair invoices to the payments that settled them, per party.

    Primary signal is the report's own grouping (``group_key``): QBO lists each
    payment with the invoices it applied to, so within a cluster every invoice
    is paired with that cluster's payment date. Clusters that hold only
    invoices or only payments fall through to amount-based matching (exact,
    then 2-4 invoice combinations), which also recovers things like a vendor
    credit offsetting a bill in a different cluster.

    Returns (party -> [(invoice_amount, lag_days)], matched, unmatched).
    Shared by receivables (DSO) and disbursements (vendor pay lag).
    """
    rows = conn.execute(
        f"SELECT {party_col} AS party, row_type, date, amount, group_key "
        f"FROM {table}"
    ).fetchall()
    parties: dict[str, dict[str, dict[str, list[tuple[str, float]]]]] = \
        defaultdict(lambda: defaultdict(lambda: {"inv": [], "pay": []}))
    for r in rows:
        if r["date"] is None or r["amount"] is None:
            continue
        cluster = parties[r["party"]][r["group_key"] or ""]
        if r["row_type"] == "invoice":
            cluster["inv"].append((r["date"], abs(r["amount"])))
        elif r["row_type"] in payment_types:
            cluster["pay"].append((r["date"], abs(r["amount"])))

    matches: dict[str, list[tuple[float, int]]] = {}
    total_matched = total_unmatched = 0
    for party, clusters in parties.items():
        party_matched: list[tuple[float, int]] = []
        leftover_inv: list[tuple[str, float]] = []
        leftover_pay: list[tuple[str, float]] = []
        for cluster in clusters.values():
            if cluster["pay"] and cluster["inv"]:
                pay_date = max(d for d, _ in cluster["pay"])
                for inv_date, amount in cluster["inv"]:
                    lag = _days(pay_date, inv_date)
                    if lag >= 0:
                        party_matched.append((amount, lag))
                    else:  # payment predates invoice — let amounts decide
                        leftover_inv.append((inv_date, amount))
            else:
                leftover_inv += cluster["inv"]
                leftover_pay += cluster["pay"]
        fb_matched, fb_unmatched = _match_party(leftover_inv, leftover_pay)
        party_matched += fb_matched
        matches[party] = party_matched
        total_matched += len(party_matched)
        total_unmatched += fb_unmatched
    return matches, total_matched, total_unmatched


def _latest_ar_snapshot(
    conn: sqlite3.Connection,
) -> tuple[int | None, str | None]:
    row = conn.execute(
        "SELECT id, as_of_date FROM ar_aging_snapshots "
        "ORDER BY as_of_date DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None, None
    return row["id"], row["as_of_date"]


def compute_receivables(conn: sqlite3.Connection) -> list[KPIValue]:
    today = dt.date.today()
    start, end = period_bounds(conn)
    as_of = end or today.isoformat()
    start = start or as_of
    beginnings = load_beginning_balances(conn)

    snapshot_id, snap_date = _latest_ar_snapshot(conn)
    if snapshot_id is None:
        conf = "low"
        snap_age = None
    else:
        snap_age = (today - dt.date.fromisoformat(snap_date)).days
        conf = "high" if snap_age <= STALE_DAYS else "medium"

    snap_rows = (
        conn.execute(
            "SELECT customer, invoice_date, due_date, num, open_balance, "
            "bucket FROM ar_aging_rows WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchall()
        if snapshot_id is not None
        else []
    )

    kpis: list[KPIValue] = []

    # --- total_ar ------------------------------------------------------------
    total_ar, _ = balance_as_of(conn, ["AR"], as_of, beginnings)
    revenue = round(abs(activity_sum(conn, ["REV"], start, end)), 2)
    avg_monthly_rev = round(revenue / 12, 2) if revenue else 0.0
    ratio = round(total_ar / avg_monthly_rev, 2) if revenue else None
    kpis.append(
        KPIValue(
            key="total_ar",
            label="Total A/R",
            value=total_ar,
            unit="currency",
            confidence=conf,
            detail={
                "avg_monthly_revenue": avg_monthly_rev,
                "ratio_to_monthly_revenue": ratio,
            },
        )
    )

    # --- pairing-based matching (shared by DSO + payment lag) ----------------
    matches, matched_count, unmatched_count = match_pairings(
        conn, "invoice_payments", "customer", ("payment",)
    )
    all_matched = [pair for lst in matches.values() for pair in lst]
    total_amt = sum(a for a, _ in all_matched)
    dso_value = (
        round(sum(a * lag for a, lag in all_matched) / total_amt, 2)
        if total_amt
        else None
    )
    pairs_seen = matched_count + unmatched_count
    kpis.append(
        KPIValue(
            key="dso_true",
            label="DSO (true, payment-matched)",
            value=dso_value,
            unit="weeks",  # days, expressed in the shared duration channel
            confidence=conf,
            detail={
                "unit_is": "days",
                "matched_count": matched_count,
                "unmatched_count": unmatched_count,
                "match_rate": round(matched_count / pairs_seen, 4)
                if pairs_seen
                else None,
            },
        )
    )

    # --- customer_payment_lag ------------------------------------------------
    per_party_lags = {
        party: [lag for _, lag in lst] for party, lst in matches.items()
    }
    client_median = _median([lag for lags in per_party_lags.values() for lag in lags])
    lag_detail: dict[str, dict] = {}
    effective_lag: dict[str, float | None] = {}
    for party, lags in per_party_lags.items():
        n = len(lags)
        avg = round(sum(lags) / n, 2) if n else None
        entry: dict = {"avg_lag_days": avg, "n_paid": n}
        if n < 3:
            entry["flag"] = "insufficient_history"
            entry["effective_lag"] = client_median
            effective_lag[party] = client_median
        else:
            effective_lag[party] = avg
        lag_detail[party] = entry
    kpis.append(
        KPIValue(
            key="customer_payment_lag",
            label="Customer Payment Lag",
            value=None,
            unit="weeks",
            confidence=conf,
            detail={"by_customer": lag_detail, "client_median_lag": client_median},
        )
    )

    # --- aging_distribution --------------------------------------------------
    aging = KPIValue(
        key="aging_distribution",
        label="A/R Aging Distribution",
        value=None,
        unit="percent",
        confidence=conf,
    )
    if not snap_rows:
        aging.notes.append("no A/R aging uploaded")
    else:
        bucket_totals: dict[str, float] = defaultdict(float)
        for r in snap_rows:
            if r["open_balance"]:
                bucket_totals[r["bucket"]] += r["open_balance"]
        total_open = sum(bucket_totals.values())
        aging.detail = {
            bucket: {
                "open_total": round(amount, 2),
                "pct_of_open": round(amount / total_open * 100, 2)
                if total_open
                else None,
            }
            for bucket, amount in bucket_totals.items()
        }
    kpis.append(aging)

    # --- ar_at_risk ----------------------------------------------------------
    at_risk = KPIValue(
        key="ar_at_risk",
        label="A/R at Risk (91+ days)",
        value=None,
        unit="currency",
        confidence=conf,
    )
    if snapshot_id is None:
        at_risk.notes.append("no A/R aging uploaded")
    else:
        risk_rows = [
            r
            for r in snap_rows
            if r["bucket"] == AT_RISK_BUCKET and r["open_balance"]
        ]
        at_risk.value = round(sum(r["open_balance"] for r in risk_rows), 2)
        at_risk.detail = {
            "invoices": [
                {
                    "customer": r["customer"],
                    "invoice_date": r["invoice_date"],
                    "num": r["num"],
                    "open_balance": round(r["open_balance"], 2),
                }
                for r in sorted(
                    risk_rows, key=lambda r: r["open_balance"], reverse=True
                )
            ]
        }
    kpis.append(at_risk)

    # --- expected_collections_13wk -------------------------------------------
    expected = KPIValue(
        key="expected_collections_13wk",
        label="Expected Collections (13 weeks)",
        value=None,
        unit="currency",
        confidence=conf,
    )
    if snapshot_id is None:
        expected.notes.append("no A/R aging uploaded")
    else:
        weekly = [0.0] * FORECAST_WEEKS
        haircut_log: list[dict] = []
        total_expected = 0.0
        for r in snap_rows:
            open_bal = r["open_balance"]
            if not open_bal or open_bal <= 0 or not r["invoice_date"]:
                continue
            factor, shift = _BUCKET_FACTOR.get(r["bucket"], (1.0, 0))
            lag = effective_lag.get(r["customer"])
            if lag is None:
                lag = client_median or 0
            inv_date = dt.date.fromisoformat(r["invoice_date"])
            expected_date = inv_date + dt.timedelta(days=round(lag) + shift)
            amount = open_bal * factor
            delta_days = (expected_date - today).days

            if delta_days > 0:
                # Not yet due: land it in the week its payment is expected.
                week = math.ceil(delta_days / 7)
                weeks_span = [week] if week <= FORECAST_WEEKS else []
            else:
                # Already overdue: spread recovery across the bucket's window.
                win_start, win_end = _RECOVERY_WINDOW.get(r["bucket"], (1, 3))
                weeks_span = list(
                    range(win_start, min(win_end, FORECAST_WEEKS) + 1)
                )
            if not weeks_span:
                continue

            k = len(weeks_span)
            per = round(amount / k, 2)
            for j, week in enumerate(weeks_span):
                # Put the rounding remainder on the last week so the invoice's
                # weekly slices sum back to its exact amount.
                slice_amt = per if j < k - 1 else round(amount - per * (k - 1), 2)
                weekly[week - 1] += slice_amt
            total_expected += amount
            haircut_log.append(
                {"invoice": r["num"], "bucket": r["bucket"], "factor": factor}
            )
        expected.value = round(total_expected, 2)
        expected.detail = {
            "weekly": [round(w, 2) for w in weekly],
            "haircut_log": haircut_log,
        }
    kpis.append(expected)

    # --- collection_effectiveness --------------------------------------------
    ar_ids = [
        r["id"]
        for r in conn.execute(
            f"SELECT a.id FROM accounts a WHERE a.category = 'AR' AND {NOT_EXCLUDED}"
        )
    ]
    beginning_ar = round(sum(beginnings.get(i, 0.0) for i in ar_ids), 2)
    flow = conn.execute(
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
    new_billings = round(flow["billed"], 2)
    collected = round(abs(flow["collected"]), 2)
    denom = beginning_ar + new_billings
    effectiveness = KPIValue(
        key="collection_effectiveness",
        label="Collection Effectiveness",
        value=round(collected / denom, 4) if denom else None,
        unit="percent",
        confidence=conf,
        detail={
            "collected": collected,
            "new_billings": new_billings,
            "beginning_ar": beginning_ar,
        },
    )
    if not denom:
        effectiveness.notes.append("no opening A/R or billings in the period")
    kpis.append(effectiveness)

    # --- customer_concentration (deferred from 6A) ---------------------------
    invoiced = conn.execute(
        """
        SELECT customer, SUM(ABS(amount)) AS total
        FROM invoice_payments
        WHERE row_type = 'invoice' AND date BETWEEN ? AND ?
        GROUP BY customer
        """,
        (start, end),
    ).fetchall()
    total_invoiced = sum(r["total"] for r in invoiced)
    top = sorted(invoiced, key=lambda r: r["total"], reverse=True)[:5]
    concentration = KPIValue(
        key="customer_concentration",
        label="Customer Concentration (top 5)",
        value=None,
        unit="percent",
        confidence=conf,
        detail={
            r["customer"]: round(r["total"] / total_invoiced * 100, 2)
            for r in top
        }
        if total_invoiced
        else {},
    )
    if total_invoiced:
        concentration.value = round(top[0]["total"] / total_invoiced * 100, 2)
    else:
        concentration.notes.append("no invoices in the period")
    kpis.append(concentration)

    return kpis
