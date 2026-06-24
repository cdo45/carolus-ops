"""13-week cashflow forecast engine.

compute_forecast(conn, scenario="BASE", weeks=13) -> ForecastResult.

The forecast is anchored to the latest cash-account transaction date; week 1
covers the seven days after the anchor, and ending cash chains week to week
from the cash balance as of the anchor. Each row family carries a confidence
tier: "scheduled" (known obligations: payroll, recurring bills, A/P, taxes),
"behavioral" (pattern-projected: collections, card payments, owner draws,
recurring revenue), or "manual" (operator-supplied billing schedule).

Three scenarios ship as module constants and are overridable per-parameter
through config keys ``scenario.<name>.<param>``:

  BASE     no lag shift, 61-90 collected at 0.75, 91+ at 0.50, draws as
           detected, discretionary spend at 100%.
  STRETCH  collections pulled a week earlier, gentler haircuts, draws on,
           discretionary 100%.
  CRUNCH   collections pushed two weeks out, harsh haircuts, draws paused,
           discretionary spend cut to 75%.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from core.kpi.base import (
    NOT_EXCLUDED,
    balance_as_of,
    load_beginning_balances,
)
from core.kpi.disbursements import (
    PAYROLL_KEYWORDS,
    _amount_clusters,
    _detect_cadence,
)
from core.kpi.receivables import (
    _BUCKET_FACTOR,
    _RECOVERY_WINDOW,
    _median,
    match_pairings,
)

SCENARIOS: dict[str, dict] = {
    "BASE": {
        "lag_shift_weeks": 0,
        "haircut_61_90": 0.75,
        "haircut_91": 0.50,
        "draws": "as_detected",
        "discretionary": 1.00,
    },
    "STRETCH": {
        "lag_shift_weeks": -1,
        "haircut_61_90": 0.90,
        "haircut_91": 0.75,
        "draws": "as_detected",
        "discretionary": 1.00,
    },
    "CRUNCH": {
        "lag_shift_weeks": 2,
        "haircut_61_90": 0.50,
        "haircut_91": 0.00,
        "draws": "paused",
        "discretionary": 0.75,
    },
}
SCENARIO_KEYS = tuple(SCENARIOS)

_NUMERIC_PARAMS = {
    "lag_shift_weeks": int,
    "haircut_61_90": float,
    "haircut_91": float,
    "discretionary": float,
}

_CADENCE_DAYS = {"weekly": 7, "biweekly": 14, "monthly": 30}
DRAW_KEYWORDS = ("owner draw", "draw", "distribution", "owner pay")
PAYROLL_TAX_KEYWORDS = ("eftps", "edd", "irs", "payroll tax", "941", "940")
PROTECTED_CATEGORIES = {"OH-OCC", "OH-INS", "LTD"}
DEFAULT_AP_TERMS_DAYS = 30

INFLOW_FAMILIES = ("collections", "new_billings", "other")
OUTFLOW_FAMILIES = (
    "payroll",
    "recurring",
    "ap_scheduled",
    "card_payments",
    "taxes",
    "owner_draws",
)


@dataclass
class WeekRow:
    index: int
    start_date: str
    end_date: str
    inflows: dict
    outflows: dict
    net: float
    ending_cash: float
    confidence_tier: dict


@dataclass
class ForecastResult:
    scenario: str
    anchor_date: str
    beginning_cash: float
    weeks: list[WeekRow]
    floor: float = 0.0
    breach_weeks: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    drivers: dict = field(default_factory=dict)


# ── config + scenario params ────────────────────────────────────────────────

def _config(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM config WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def _scenario_params(conn: sqlite3.Connection, scenario: str) -> dict:
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown scenario {scenario!r}.")
    params = dict(SCENARIOS[scenario])
    for param in params:
        override = _config(conn, f"scenario.{scenario}.{param}")
        if override is None:
            continue
        caster = _NUMERIC_PARAMS.get(param)
        params[param] = caster(override) if caster else override
    return params


# ── date helpers ────────────────────────────────────────────────────────────

def _iso(d: dt.date) -> str:
    return d.isoformat()


def _add_month(d: dt.date) -> dt.date:
    month = d.month + 1
    year = d.year
    if month > 12:
        month = 1
        year += 1
    last = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(d.day, last))


def _next_month_end(anchor: dt.date) -> dt.date:
    last = dt.date(
        anchor.year, anchor.month,
        calendar.monthrange(anchor.year, anchor.month)[1],
    )
    if last > anchor:
        return last
    nxt = _add_month(dt.date(anchor.year, anchor.month, 1))
    return dt.date(nxt.year, nxt.month, calendar.monthrange(nxt.year, nxt.month)[1])


def _week_of(d: dt.date, anchor: dt.date, weeks: int) -> int | None:
    """Week index (1..weeks) a date falls in, or None if before the anchor or
    past the horizon. Week 1 = anchor+1 .. anchor+7."""
    delta = (d - anchor).days
    if delta <= 0:
        return None
    week = math.ceil(delta / 7)
    return week if week <= weeks else None


def _project_cadence(
    last_date: dt.date, cadence: str, anchor: dt.date, weeks: int
) -> list[int]:
    """Week indices of occurrences stepping forward from last_date on the
    detected cadence, kept to the horizon."""
    out: list[int] = []
    horizon = anchor + dt.timedelta(days=weeks * 7)
    d = last_date
    guard = 0
    while guard < 1000:
        guard += 1
        d = _add_month(d) if cadence == "monthly" else d + dt.timedelta(
            days=_CADENCE_DAYS[cadence]
        )
        if d > horizon:
            break
        week = _week_of(d, anchor, weeks)
        if week:
            out.append(week)
    return out


# ── account / transaction helpers ───────────────────────────────────────────

def _account_categories(conn: sqlite3.Connection) -> dict[str, str]:
    """name (and leaf) lowercased -> category, for split classification."""
    mapping: dict[str, str] = {}
    for r in conn.execute(
        "SELECT qbo_name, full_path, category FROM accounts WHERE category IS NOT NULL"
    ):
        mapping.setdefault(r["qbo_name"].strip().lower(), r["category"])
        leaf = (r["full_path"] or r["qbo_name"]).split(":")[-1].strip().lower()
        mapping.setdefault(leaf, r["category"])
    return mapping


def _split_category(split: str | None, cat_map: dict[str, str]) -> str | None:
    if not split:
        return None
    return cat_map.get(split.strip().lower()) or cat_map.get(
        split.split(":")[-1].strip().lower()
    )


def _cash_negatives(conn: sqlite3.Connection):
    return conn.execute(
        f"""
        SELECT t.name, t.txn_type, t.split, t.description, t.txn_date,
               t.amount
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE a.category = 'CASH' AND {NOT_EXCLUDED} AND t.amount < 0
        """
    ).fetchall()


def _cash_anchor(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        f"""
        SELECT MAX(t.txn_date)
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE a.category = 'CASH' AND {NOT_EXCLUDED}
        """
    ).fetchone()
    return row[0]


# ── outflow group classification ────────────────────────────────────────────

@dataclass
class _Group:
    name: str
    txn_type: str
    items: list[tuple[str, float]]  # (date_iso, abs_amount)
    split_category: str | None
    family: str  # payroll | draw | tax | card | ap | recurring


def _classify_groups(rows, cat_map: dict[str, str]) -> list[_Group]:
    buckets: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"items": [], "splits": []}
    )
    for r in rows:
        if not r["name"]:
            continue
        key = (r["name"], r["txn_type"] or "")
        buckets[key]["items"].append((r["txn_date"], abs(r["amount"])))
        buckets[key]["splits"].append(_split_category(r["split"], cat_map))

    groups: list[_Group] = []
    for (name, ttype), data in buckets.items():
        cats = [c for c in data["splits"] if c]
        dominant = Counter(cats).most_common(1)[0][0] if cats else None
        text = name.lower()
        if dominant in {"OH-PAY", "DL"} or any(k in text for k in PAYROLL_KEYWORDS):
            family = "payroll"
        elif dominant == "EQ-DRAW" or any(k in text for k in DRAW_KEYWORDS):
            family = "draw"
        elif dominant in {"TAXL", "TAXE"} or any(
            k in text for k in PAYROLL_TAX_KEYWORDS
        ):
            family = "tax"
        elif dominant == "CC":
            family = "card"
        elif dominant == "AP":
            family = "ap"
        else:
            family = "recurring"
        groups.append(_Group(name, ttype, data["items"], dominant, family))
    return groups


def _cadence_series(
    items: list[tuple[str, float]], anchor: dt.date, weeks: int
) -> tuple[str | None, float, list[int]]:
    """Detect a single dominant recurring cluster in a group and project it.
    Returns (cadence, typical_amount, [week indices]). cadence None when no
    cluster qualifies."""
    for cluster in _amount_clusters(items):
        if len(cluster) < 3:
            continue
        cadence = _detect_cadence([d for d, _ in cluster])
        if cadence is None:
            continue
        typical = statistics.mean(a for _, a in cluster)
        last = max(dt.date.fromisoformat(d) for d, _ in cluster)
        return cadence, typical, _project_cadence(last, cadence, anchor, weeks)
    return None, 0.0, []


# ── inflow builders ─────────────────────────────────────────────────────────

def _collections_weekly(
    conn: sqlite3.Connection, anchor: dt.date, params: dict, weeks: int
) -> tuple[list[float], dict]:
    info = {"has_aging": False, "invoices": 0, "total": 0.0}
    snap = conn.execute(
        "SELECT id FROM ar_aging_snapshots ORDER BY as_of_date DESC, id DESC "
        "LIMIT 1"
    ).fetchone()
    if snap is None:
        return [0.0] * weeks, info
    rows = conn.execute(
        "SELECT customer, num, invoice_date, open_balance, bucket "
        "FROM ar_aging_rows WHERE snapshot_id = ?",
        (snap["id"],),
    ).fetchall()
    info["has_aging"] = True
    info["invoice_list"] = []

    matches, _, _ = match_pairings(
        conn, "invoice_payments", "customer", ("payment",)
    )
    per_party = {p: [lag for _, lag in lst] for p, lst in matches.items()}
    all_lags = [lag for lags in per_party.values() for lag in lags]
    median = _median(all_lags)
    own_lag = {p for p, lags in per_party.items() if len(lags) >= 3}
    effective = {
        p: (sum(lags) / len(lags)) if len(lags) >= 3 else median
        for p, lags in per_party.items()
    }
    shift_weeks = params["lag_shift_weeks"]

    base = [0.0] * weeks
    for r in rows:
        open_bal = r["open_balance"]
        if not open_bal or open_bal <= 0 or not r["invoice_date"]:
            continue
        factor, shift = _BUCKET_FACTOR.get(r["bucket"], (1.0, 0))
        if r["bucket"] == "61 - 90 days past due":
            factor = params["haircut_61_90"]
        elif r["bucket"] == "91 or more days past due":
            factor = params["haircut_91"]
        lag = effective.get(r["customer"])
        if lag is None:
            lag = median or 0
        amount = open_bal * factor
        expected = dt.date.fromisoformat(r["invoice_date"]) + dt.timedelta(
            days=round(lag) + shift
        )
        delta = (expected - anchor).days
        if delta > 0:
            week = math.ceil(delta / 7)
            span = [week] if week <= weeks else []
            timing = expected.isoformat()
        else:
            # Already overdue: spread recovery across the bucket's window
            # instead of dumping it all into week 1.
            lo, hi = _RECOVERY_WINDOW.get(r["bucket"], (1, 3))
            span = list(range(lo, min(hi, weeks) + 1))
            timing = "overdue — recovering"
        if not span:
            continue
        info["invoices"] += 1
        info["total"] += amount
        per = amount / len(span)
        for w in span:
            base[w - 1] += per
        # The week(s) shown to the user reflect the scenario's lag shift.
        shown = sorted({min(max(w + shift_weeks, 1), weeks) for w in span})
        info["invoice_list"].append({
            "customer": r["customer"],
            "invoice": r["num"],
            "open_balance": round(open_bal, 2),
            "bucket": r["bucket"],
            "days_to_pay": round(lag),
            "lag_basis": "this customer" if r["customer"] in own_lag
            else "company median",
            "timing": timing,
            "weeks": shown,
            "projected": round(amount, 2),
        })

    shifted = [0.0] * weeks
    for i in range(weeks):
        target = min(max(i + 1 + shift_weeks, 1), weeks)
        shifted[target - 1] += base[i]
    info["invoice_list"].sort(key=lambda x: x["projected"], reverse=True)
    info["total"] = round(info["total"], 2)
    return shifted, info


def _new_billings_weekly(
    conn: sqlite3.Connection, weeks: int
) -> tuple[list[float], str, str | None, list[dict]]:
    """Returns (weekly array, tier, note, payers). Recurring-revenue clients
    get a behavioral projection; everyone else falls to the manual schedule."""
    rows = conn.execute(
        f"""
        SELECT t.name, t.txn_date, t.amount
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE a.category IN ('AR', 'CASH') AND {NOT_EXCLUDED} AND t.amount > 0
        """
    ).fetchall()
    by_payer: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in rows:
        if r["name"]:
            by_payer[r["name"]].append((r["txn_date"], r["amount"]))

    monthly_total = 0.0
    payers: list[dict] = []
    for name, items in by_payer.items():
        for cluster in _amount_clusters(items):
            if len(cluster) >= 3 and _detect_cadence(
                [d for d, _ in cluster]
            ) == "monthly":
                typical = statistics.mean(a for _, a in cluster)
                monthly_total += typical
                payers.append({"payer": name, "monthly_amount": round(typical, 2),
                               "n_seen": len(cluster)})

    if monthly_total > 0:
        weekly = round(monthly_total / 4.33, 2)
        payers.sort(key=lambda p: p["monthly_amount"], reverse=True)
        return [weekly] * weeks, "behavioral", None, payers

    raw = _config(conn, "manual_billing_schedule")
    if raw:
        weekly = [0.0] * weeks
        for entry in json.loads(raw):
            week = int(entry["week"])
            if 1 <= week <= weeks:
                weekly[week - 1] += float(entry["amount"])
        return weekly, "manual", None, []
    return [0.0] * weeks, "manual", "no manual billing schedule configured", []


# ── outflow builders ────────────────────────────────────────────────────────

def _card_payments_weekly(
    conn: sqlite3.Connection,
    anchor: dt.date,
    beginnings: dict[int, float],
    weeks: int,
) -> tuple[list[float], list[dict]]:
    weekly = [0.0] * weeks
    cards: list[dict] = []
    names = {r["id"]: r["qbo_name"]
             for r in conn.execute("SELECT id, qbo_name FROM accounts")}
    per_account = balance_as_of(conn, ["CC"], _iso(anchor), beginnings)[1]
    for account_id, balance in per_account.items():
        if balance <= 0:
            continue
        history = conn.execute(
            "SELECT txn_date, amount FROM transactions "
            "WHERE account_id = ? AND amount < 0 AND txn_date <= ?",
            (account_id, _iso(anchor)),
        ).fetchall()
        amounts = [abs(r["amount"]) for r in history]
        days = [dt.date.fromisoformat(r["txn_date"]).day for r in history]
        anchor_day = (
            int(statistics.median(days)) if days else min(28, anchor.day)
        )
        pay_dates = _monthly_dates(anchor, weeks, anchor_day)
        if not pay_dates:
            continue
        amount = (
            statistics.median(amounts)
            if amounts
            else round(balance / len(pay_dates), 2)
        )
        for d in pay_dates:
            week = _week_of(d, anchor, weeks)
            if week:
                weekly[week - 1] += amount
        cards.append({
            "card": names.get(account_id, "Card"),
            "balance": round(balance, 2),
            "payment": round(amount, 2),
            "cycle_day": anchor_day,
            "basis": "median of past payments" if amounts
            else "balance spread over the window",
        })
    return weekly, cards


def _monthly_dates(anchor: dt.date, weeks: int, day: int) -> list[dt.date]:
    horizon = anchor + dt.timedelta(days=weeks * 7)
    out: list[dt.date] = []
    year, month = anchor.year, anchor.month
    while True:
        last = calendar.monthrange(year, month)[1]
        d = dt.date(year, month, min(day, last))
        if d > horizon:
            break
        if d > anchor:
            out.append(d)
        month += 1
        if month > 12:
            month = 1
            year += 1
    return out


def _ap_scheduled_weekly(
    conn: sqlite3.Connection, anchor: dt.date, weeks: int
) -> tuple[list[float], list[dict]]:
    weekly = [0.0] * weeks
    bills: list[dict] = []
    snap = conn.execute(
        "SELECT id FROM ap_aging_snapshots ORDER BY as_of_date DESC, id DESC "
        "LIMIT 1"
    ).fetchone()
    if snap is None:
        return weekly, bills
    rows = conn.execute(
        "SELECT vendor, due_date, open_balance FROM ap_aging_rows "
        "WHERE snapshot_id = ?",
        (snap["id"],),
    ).fetchall()
    if not rows:
        return weekly, bills

    matches, _, _ = match_pairings(
        conn, "bills_payments", "vendor", ("payment", "credit")
    )
    terms_raw = _config(conn, "ap_terms_days")
    terms = int(terms_raw) if terms_raw else DEFAULT_AP_TERMS_DAYS
    vendor_slippage: dict[str, int] = {}
    for vendor, lst in matches.items():
        if lst:
            avg_lag = sum(lag for _, lag in lst) / len(lst)
            vendor_slippage[vendor] = max(0, round(avg_lag) - terms)

    floor_date = anchor + dt.timedelta(days=1)
    for r in rows:
        open_bal = r["open_balance"]
        if not open_bal or open_bal <= 0:
            continue
        due = (
            dt.date.fromisoformat(r["due_date"]) if r["due_date"] else floor_date
        )
        slip = vendor_slippage.get(r["vendor"], 0)
        place = max(due, floor_date) + dt.timedelta(days=slip)
        week = _week_of(place, anchor, weeks)
        if week:
            weekly[week - 1] += open_bal
            bills.append({
                "vendor": r["vendor"], "amount": round(open_bal, 2),
                "due": r["due_date"], "week": week,
                "slippage_days": slip,
            })
    bills.sort(key=lambda b: b["week"])
    return weekly, bills


def _taxes_weekly(
    conn: sqlite3.Connection,
    anchor: dt.date,
    beginnings: dict[int, float],
    weeks: int,
    tax_groups: list[_Group],
) -> tuple[list[float], dict]:
    weekly = [0.0] * weeks
    info: dict = {"sales_tax": None, "payroll_tax": []}
    taxl_balance = balance_as_of(conn, ["TAXL"], _iso(anchor), beginnings)[0]
    if taxl_balance > 0:
        month_end = _next_month_end(anchor)
        week = _week_of(month_end, anchor, weeks)
        if week:
            weekly[week - 1] += taxl_balance
            info["sales_tax"] = {"amount": round(taxl_balance, 2),
                                 "due": _iso(month_end), "week": week}
    for group in tax_groups:
        cadence, typical, week_indices = _cadence_series(
            group.items, anchor, weeks
        )
        for week in week_indices:
            weekly[week - 1] += typical
        if cadence:
            info["payroll_tax"].append({"payee": group.name, "cadence": cadence,
                                        "typical_amount": round(typical, 2)})
    return weekly, info


# ── assembly ─────────────────────────────────────────────────────────────────

def compute_forecast(
    conn: sqlite3.Connection, scenario: str = "BASE", weeks: int = 13
) -> ForecastResult:
    params = _scenario_params(conn, scenario)
    anchor_iso = _cash_anchor(conn)
    if anchor_iso is None:
        raise ValueError("No cash-account transactions; nothing to forecast.")
    anchor = dt.date.fromisoformat(anchor_iso)
    beginnings = load_beginning_balances(conn)
    beginning_cash = balance_as_of(conn, ["CASH"], anchor_iso, beginnings)[0]
    cat_map = _account_categories(conn)
    notes: list[str] = []

    groups = _classify_groups(_cash_negatives(conn), cat_map)

    drivers: dict = {}

    # --- inflows -------------------------------------------------------------
    collections, collections_info = _collections_weekly(
        conn, anchor, params, weeks)
    drivers["collections"] = collections_info
    new_billings, billing_tier, billing_note, billing_payers = \
        _new_billings_weekly(conn, weeks)
    if billing_note:
        notes.append(billing_note)
    drivers["new_billings"] = {
        "basis": "recurring revenue" if billing_tier == "behavioral"
        else "manual schedule",
        "note": billing_note,
        "weekly_amount": round(new_billings[0], 2) if new_billings else 0.0,
        "payers": billing_payers,
    }

    # --- outflows ------------------------------------------------------------
    payroll = [0.0] * weeks
    payroll_items: list[dict] = []
    for group in (g for g in groups if g.family == "payroll"):
        cadence, typical, week_indices = _cadence_series(
            group.items, anchor, weeks)
        for week in week_indices:
            payroll[week - 1] += typical
        if cadence:
            payroll_items.append({
                "payee": group.name, "cadence": cadence,
                "typical_amount": round(typical, 2),
                "last_seen": max(d for d, _ in group.items)})
    drivers["payroll"] = payroll_items

    recurring = [0.0] * weeks
    recurring_items: list[dict] = []
    discretionary = params["discretionary"]
    for group in (g for g in groups if g.family == "recurring"):
        cadence, typical, week_indices = _cadence_series(
            group.items, anchor, weeks)
        protected = group.split_category in PROTECTED_CATEGORIES
        amount = typical if protected else typical * discretionary
        for week in week_indices:
            recurring[week - 1] += amount
        if cadence:
            recurring_items.append({
                "payee": group.name, "cadence": cadence,
                "typical_amount": round(typical, 2),
                "applied_amount": round(amount, 2),
                "protected": protected})
    drivers["recurring"] = {"items": recurring_items,
                            "discretionary": discretionary}

    owner_draws = [0.0] * weeks
    draw_items: list[dict] = []
    if params["draws"] == "paused":
        notes.append(f"owner draws paused under {scenario}")
        drivers["owner_draws"] = {"paused": True, "items": []}
    else:
        for group in (g for g in groups if g.family == "draw"):
            cadence, typical, week_indices = _cadence_series(
                group.items, anchor, weeks
            )
            for week in week_indices:
                owner_draws[week - 1] += typical
            if cadence:
                draw_items.append({
                    "payee": group.name, "cadence": cadence,
                    "typical_amount": round(typical, 2)})
        drivers["owner_draws"] = {"paused": False, "items": draw_items}

    ap_scheduled, ap_bills = _ap_scheduled_weekly(conn, anchor, weeks)
    drivers["ap_scheduled"] = {"bills": ap_bills}
    card_payments, card_items = _card_payments_weekly(
        conn, anchor, beginnings, weeks)
    drivers["card_payments"] = {"cards": card_items}
    taxes, tax_info = _taxes_weekly(
        conn, anchor, beginnings, weeks,
        [g for g in groups if g.family == "tax"],
    )
    drivers["taxes"] = tax_info

    tiers = {
        "collections": "behavioral",
        "new_billings": billing_tier,
        "other": "manual",
        "payroll": "scheduled",
        "recurring": "scheduled",
        "ap_scheduled": "scheduled",
        "card_payments": "behavioral",
        "taxes": "scheduled",
        "owner_draws": "behavioral",
    }

    week_rows: list[WeekRow] = []
    running = beginning_cash
    for i in range(weeks):
        inflows = {
            "collections": round(collections[i], 2),
            "new_billings": round(new_billings[i], 2),
            "other": 0.0,
        }
        outflows = {
            "payroll": round(payroll[i], 2),
            "recurring": round(recurring[i], 2),
            "ap_scheduled": round(ap_scheduled[i], 2),
            "card_payments": round(card_payments[i], 2),
            "taxes": round(taxes[i], 2),
            "owner_draws": round(owner_draws[i], 2),
        }
        net = round(sum(inflows.values()) - sum(outflows.values()), 2)
        running = round(running + net, 2)
        start = anchor + dt.timedelta(days=7 * i + 1)
        end = anchor + dt.timedelta(days=7 * (i + 1))
        week_rows.append(
            WeekRow(
                index=i + 1,
                start_date=_iso(start),
                end_date=_iso(end),
                inflows=inflows,
                outflows=outflows,
                net=net,
                ending_cash=running,
                confidence_tier=dict(tiers),
            )
        )

    floor_raw = _config(conn, "cash_floor")
    floor = float(floor_raw) if floor_raw else 0.0
    breach_weeks = [w.index for w in week_rows if w.ending_cash < floor]

    return ForecastResult(
        scenario=scenario,
        anchor_date=anchor_iso,
        beginning_cash=beginning_cash,
        weeks=week_rows,
        floor=floor,
        breach_weeks=breach_weeks,
        notes=notes,
        drivers=drivers,
    )


# ── persistence + variance ───────────────────────────────────────────────────

def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def save_forecast(conn: sqlite3.Connection, result: ForecastResult) -> int:
    """Persist a forecast: one forecast_runs row keyed to the anchor date,
    plus one forecast_rows row per (week, row family). Returns the run id.

    run_date stores the forecast anchor so variance can recover each run's
    week-1 window later.
    """
    try:
        cur = conn.execute(
            "INSERT INTO forecast_runs (run_date, scenario) VALUES (?, ?)",
            (result.anchor_date, result.scenario),
        )
        run_id = cur.lastrowid
        rows = []
        for week in result.weeks:
            for family, amount in {**week.inflows, **week.outflows}.items():
                rows.append(
                    (run_id, week.index, family, amount,
                     week.confidence_tier[family])
                )
        conn.executemany(
            "INSERT INTO forecast_rows "
            "(run_id, week, row_type, amount, confidence_tier) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return run_id


def compute_variance(conn: sqlite3.Connection) -> list[dict]:
    """Compare the most recent saved run whose week-1 window has fully elapsed
    (and isn't yet scored) against cash actuals over that window.

    Writes two variance_log rows — 'inflow_total' and 'outflow_total' —
    comparing the run's week-1 forecast totals to actual CASH-account inflow
    and outflow. Returns the rows written; returns [] when no run has an
    elapsed, unscored week 1.
    """
    anchor_iso = _cash_anchor(conn)
    if anchor_iso is None:
        return []
    latest_actual = dt.date.fromisoformat(anchor_iso)

    runs = conn.execute(
        "SELECT id, run_date FROM forecast_runs ORDER BY run_date DESC, id DESC"
    ).fetchall()
    target = None
    for run in runs:
        run_anchor = dt.date.fromisoformat(run["run_date"])
        week1_end = run_anchor + dt.timedelta(days=7)
        if week1_end > latest_actual:
            continue  # week 1 not yet fully covered by actuals
        scored = conn.execute(
            "SELECT 1 FROM variance_log WHERE run_id = ? LIMIT 1", (run["id"],)
        ).fetchone()
        if scored:
            continue
        target = (run["id"], run_anchor, week1_end)
        break
    if target is None:
        return []

    run_id, run_anchor, week1_end = target
    week1_start = run_anchor + dt.timedelta(days=1)

    in_marks = ",".join("?" * len(INFLOW_FAMILIES))
    out_marks = ",".join("?" * len(OUTFLOW_FAMILIES))
    inflow_forecast = conn.execute(
        f"SELECT COALESCE(SUM(amount), 0) FROM forecast_rows "
        f"WHERE run_id = ? AND week = 1 AND row_type IN ({in_marks})",
        (run_id, *INFLOW_FAMILIES),
    ).fetchone()[0]
    outflow_forecast = conn.execute(
        f"SELECT COALESCE(SUM(amount), 0) FROM forecast_rows "
        f"WHERE run_id = ? AND week = 1 AND row_type IN ({out_marks})",
        (run_id, *OUTFLOW_FAMILIES),
    ).fetchone()[0]

    actual = conn.execute(
        f"""
        SELECT COALESCE(SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END), 0)
                   AS inflow,
               COALESCE(SUM(CASE WHEN t.amount < 0 THEN t.amount ELSE 0 END), 0)
                   AS outflow
        FROM transactions t JOIN accounts a ON a.id = t.account_id
        WHERE a.category = 'CASH' AND {NOT_EXCLUDED}
          AND t.txn_date BETWEEN ? AND ?
        """,
        (_iso(week1_start), _iso(week1_end)),
    ).fetchone()

    written = [
        {
            "run_id": run_id,
            "week_ending": _iso(week1_end),
            "row_type": "inflow_total",
            "forecast": round(inflow_forecast, 2),
            "actual": round(actual["inflow"], 2),
        },
        {
            "run_id": run_id,
            "week_ending": _iso(week1_end),
            "row_type": "outflow_total",
            "forecast": round(outflow_forecast, 2),
            "actual": round(abs(actual["outflow"]), 2),
        },
    ]
    now = _now()
    try:
        for row in written:
            conn.execute(
                "INSERT INTO variance_log "
                "(run_id, week_ending, row_type, forecast, actual, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row["run_id"], row["week_ending"], row["row_type"],
                 row["forecast"], row["actual"], now),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return written
