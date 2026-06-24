"""Offline HTML dashboard generator.

generate_dashboard(conn, client_name, out_dir, scenario_default="BASE") -> Path
writes a single self-contained file: {Client}_Dashboard_{YYYY-MM}.html. Every
asset is inline — CSS in a <style> block, JS in a <script> block, and all data
embedded once as JSON in <script type="application/json" id="data">. The file
makes zero external requests: no CDN, no web fonts, no image or font URLs, and
the SVG charts are drawn by a few lines of vanilla JS (bars + line only).

Any URL-looking text in the embedded data is scrubbed before serialization so
the offline guarantee holds even when a transaction memo contains a link, and
the template itself contains no "http" substring (no xmlns on inline SVG, no
http-equiv meta).
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path

from core.kpi.base import NOT_EXCLUDED, load_beginning_balances, period_bounds
from core.kpi.disbursements import compute_disbursements
from core.kpi.forecast import ForecastResult, compute_forecast
from core.kpi.history import monthly_history
from core.kpi.liquidity import compute_liquidity
from core.kpi.receivables import compute_receivables
from core.kpi.revenue import compute_revenue

SCENARIO_KEYS = ("BASE", "STRETCH", "CRUNCH")
# percent-unit KPIs whose value is stored as a fraction, not a scaled percent.
_FRACTION_PERCENT = {"collection_effectiveness", "payroll_load", "gross_margin"}
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)

_INFLOW_ROWS = (
    ("collections", "Collections"),
    ("new_billings", "New billings"),
    ("other", "Other"),
)
_OUTFLOW_ROWS = (
    ("payroll", "Payroll"),
    ("recurring", "Recurring"),
    ("ap_scheduled", "A/P scheduled"),
    ("card_payments", "Card payments"),
    ("taxes", "Taxes"),
    ("owner_draws", "Owner draws"),
)


# ── data scrubbing ───────────────────────────────────────────────────────────

def _scrub(obj):
    """Recursively replace URL-ish text so nothing reaches the embedded JSON."""
    if isinstance(obj, str):
        return _URL_RE.sub("[link removed]", obj)
    if isinstance(obj, dict):
        return {
            (_scrub(k) if isinstance(k, str) else k): _scrub(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


# ── gather ───────────────────────────────────────────────────────────────────

def _global_db_path(conn: sqlite3.Connection) -> Path | None:
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main" and file:
            return Path(file).parent.parent / "aliases.db"
    return None


def _flag_patterns(conn: sqlite3.Connection) -> list[str]:
    path = _global_db_path(conn)
    if path is None or not path.exists():
        return []
    g = sqlite3.connect(path)
    try:
        return [
            (r[0] or "").strip().lower()
            for r in g.execute(
                "SELECT pattern FROM aliases WHERE flag_if_nonzero = 1"
            )
        ]
    finally:
        g.close()


def _account_balances(conn: sqlite3.Connection) -> dict[int, tuple[sqlite3.Row, float]]:
    beginnings = load_beginning_balances(conn)
    activity = {
        r["account_id"]: r["s"]
        for r in conn.execute(
            "SELECT account_id, SUM(amount) AS s FROM transactions "
            "GROUP BY account_id"
        )
    }
    out: dict[int, tuple[sqlite3.Row, float]] = {}
    for r in conn.execute(
        "SELECT id, qbo_name, full_path, category, dormant, coa_balance "
        "FROM accounts"
    ):
        if r["id"] in beginnings or r["id"] in activity:
            balance = beginnings.get(r["id"], 0.0) + activity.get(r["id"], 0.0)
        else:
            balance = r["coa_balance"] or 0.0
        out[r["id"]] = (r, round(balance, 2))
    return out


def _matches_flag(leaf: str, patterns: list[str]) -> bool:
    return any(
        leaf == p or leaf.startswith(p + " (") for p in patterns if p
    )


def _gather_flags(conn: sqlite3.Connection) -> dict:
    patterns = _flag_patterns(conn)
    flagged: list[dict] = []
    dormant_nonzero: list[dict] = []
    for _id, (r, balance) in _account_balances(conn).items():
        if abs(balance) <= 0.005:
            continue
        leaf = (r["full_path"] or r["qbo_name"]).split(":")[-1].strip().lower()
        if _matches_flag(leaf, patterns):
            flagged.append(
                {"name": r["qbo_name"], "balance": balance,
                 "reason": "clearing/suspense account should be zero"}
            )
        if r["dormant"]:
            dormant_nonzero.append({"name": r["qbo_name"], "balance": balance})
    unmapped = conn.execute(
        f"SELECT COUNT(*) FROM accounts a WHERE a.category IS NULL "
        f"AND {NOT_EXCLUDED}"
    ).fetchone()[0]
    queue = [
        r["qbo_name"]
        for r in conn.execute(
            f"SELECT qbo_name FROM accounts a WHERE status != 'confirmed' "
            f"AND (a.category IS NULL OR a.confidence < 70) AND {NOT_EXCLUDED}"
        )
    ]
    return {
        "flagged_accounts": flagged,
        "dormant_nonzero": dormant_nonzero,
        "unmapped_count": unmapped,
        "queue": queue,
    }


def _change_banner(conn: sqlite3.Connection) -> dict:
    last = conn.execute(
        "SELECT run_date FROM forecast_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    sql = "SELECT entity, entity_id, field, ts FROM audit_log WHERE source = 'user'"
    params: list = []
    if last:
        sql += " AND ts >= ?"
        params.append(last["run_date"])
    sql += " ORDER BY ts"
    items: list[str] = []
    for r in conn.execute(sql, params):
        name = None
        if r["entity"] == "accounts" and r["entity_id"]:
            row = conn.execute(
                "SELECT qbo_name FROM accounts WHERE id = ?", (r["entity_id"],)
            ).fetchone()
            name = row["qbo_name"] if row else None
        target = name or f"{r['entity']} #{r['entity_id']}"
        items.append(f"{target}: {r['field']} updated")
    text = (
        f"{len(items)} bookkeeping change(s) applied since the last forecast."
        if items
        else ""
    )
    return {"count": len(items), "items": items, "text": text}


def _forecast_to_dict(result: ForecastResult) -> dict:
    return {
        "scenario": result.scenario,
        "anchor_date": result.anchor_date,
        "beginning_cash": result.beginning_cash,
        "floor": result.floor,
        "breach_weeks": result.breach_weeks,
        "notes": result.notes,
        "drivers": result.drivers,
        "weeks": [asdict(w) for w in result.weeks],
    }


def _gather(conn: sqlite3.Connection, client_name: str, scenario_default: str) -> dict:
    start, end = period_bounds(conn)
    kpis = {
        "liquidity": [asdict(k) for k in compute_liquidity(conn)],
        "revenue": [asdict(k) for k in compute_revenue(conn)],
        "receivables": [asdict(k) for k in compute_receivables(conn)],
        "disbursements": [asdict(k) for k in compute_disbursements(conn)],
    }
    # Record this run's scalar values, then load the full trend. Balance-driven
    # KPIs are backfilled monthly straight from the GL, so their charts show a
    # real history immediately; everything else accumulates run over run.
    _snapshot_history(conn, kpis, end or dt.date.today().isoformat())
    history = {**_load_history(conn), **monthly_history(conn)}
    for section in kpis.values():
        for kpi in section:
            kpi["headline"] = _headline_for(kpi)
            kpi["chart"] = _kpi_chart(kpi, history)
    forecast = {
        scn: _forecast_to_dict(compute_forecast(conn, scn))
        for scn in SCENARIO_KEYS
    }
    data = {
        "client": client_name,
        "period": {"start": start, "end": end},
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "scenario_default": scenario_default,
        "kpis": kpis,
        "forecast": forecast,
        "flags": _gather_flags(conn),
        "change_banner": _change_banner(conn),
    }
    return _scrub(data)


# ── server-side formatting + rendering ───────────────────────────────────────

def _esc(text) -> str:
    return html.escape(str(text), quote=True)


def _fmt_value(kpi: dict) -> str:
    value = kpi["value"]
    if value is None:
        return "—"
    detail = kpi.get("detail") or {}
    if detail.get("unit_is") == "days":
        return f"{value:.1f} days"
    unit = kpi["unit"]
    if unit == "currency":
        return "$" + f"{round(value):,}"
    if unit == "percent":
        scaled = value * 100 if kpi["key"] in _FRACTION_PERCENT else value
        return f"{scaled:.1f}%"
    if unit == "ratio":
        return f"{value:.2f}"
    if unit == "weeks":
        return f"{value:.1f} wk"
    if unit == "months":
        return f"{value:.1f} mo"
    return _esc(value)


KPI_INFO = {
    "cash_on_hand": ("Money available across all bank accounts right now.",
        "Your starting point — the whole forecast flows from here."),
    "weeks_of_cash": ("How many weeks you could operate if no new money came in.",
        "Your runway. Under about four weeks is tight and worth acting on."),
    "net_working_capital": ("Short-term assets minus short-term obligations.",
        "Positive means current resources cover what's due soon."),
    "current_ratio": ("Current assets divided by current liabilities.",
        "Above 1.0 means you can cover near-term bills; around 2 is comfortable."),
    "card_debt_load": ("Credit-card balance expressed in months of revenue.",
        "A rising load means cards are funding operations — a warning sign."),
    "quick_burn_check": ("Cash plus receivables, minus payables, cards, and a "
        "month of spending.",
        "Negative means even collecting A/R won't cover near-term obligations."),
    "revenue_t12": ("Total revenue over the trailing twelve months.",
        "The top line — the scale of the business."),
    "revenue_trend_3mo": ("Average monthly revenue, last 3 months vs the prior 3.",
        "Shows whether the business is growing or slowing."),
    "revenue_mix": ("How revenue splits across income accounts.",
        "Heavy concentration in one stream is a risk if it dries up."),
    "gross_margin": ("Share of revenue left after direct job costs.",
        "What's available to cover overhead and profit."),
    "direct_labor_pct": ("Direct labor as a share of revenue.",
        "Labor efficiency on the billable work."),
    "overhead_burn": ("Average monthly overhead — rent, admin, insurance.",
        "Fixed cost you carry every month regardless of sales."),
    "breakeven_revenue": ("Monthly revenue needed to cover overhead at current "
        "margin.",
        "Sell below this in a month and you lose money that month."),
    "revenue_per_job": ("Revenue grouped by job or project.",
        "Which jobs are carrying the top line."),
    "total_ar": ("Money customers owe you on open invoices.",
        "Cash you've earned but haven't collected yet."),
    "dso_true": ("Average days customers take to pay, from real payment history.",
        "Lower means faster cash; a high number strains the forecast."),
    "customer_payment_lag": ("How long each customer typically takes to pay.",
        "Identifies the slow payers dragging on cash."),
    "aging_distribution": ("How open A/R splits across age buckets.",
        "Older buckets are less likely to be collected."),
    "ar_at_risk": ("Open invoices more than 90 days past due.",
        "The receivables most likely to never be collected."),
    "expected_collections_13wk": ("Projected collections over the next 13 weeks.",
        "Drives the inflow side of the cash forecast."),
    "collection_effectiveness": ("Share of collectible A/R you actually collected.",
        "How well the business turns invoices into cash."),
    "customer_concentration": ("Share of billings from your largest customers.",
        "High concentration is a risk if a top customer leaves."),
    "open_ap_due": ("Unpaid vendor bills and what's coming due.",
        "Near-term cash you owe."),
    "vendor_pay_lag": ("Average days you take to pay vendors.",
        "How far you're stretching payables."),
    "top5_vendor_concentration": ("Share of spend going to your top vendors.",
        "Where your outgoing cash is concentrated."),
    "recurring_outflow_base": ("Monthly total of recurring outflows.",
        "The baseline cash leaving every month — payroll, rent, subscriptions."),
    "payroll_load": ("Payroll as a share of total cash outflow.",
        "How much of your spend is people."),
    "card_cycle_exposure": ("Total credit-card balances and payment timing.",
        "Card obligations sitting inside the forecast window."),
    "job_cash_demand": ("Open payables grouped by job.",
        "Which jobs are tying up cash in unpaid bills."),
}

# Plain-English "how we got this number" for the click-in detail.
KPI_HOW = {
    "cash_on_hand": "We total the current balance of every bank and cash "
        "account — each account's opening balance plus every transaction "
        "posted to it through the end of the period.",
    "weeks_of_cash": "We divide your cash on hand by your average weekly cash "
        "outflow over the trailing 13 weeks. It answers: if money stopped "
        "coming in and you kept spending at the recent pace, how long would "
        "the cash last?",
    "net_working_capital": "We add up your short-term assets (cash, "
        "receivables, other current assets) and subtract your short-term "
        "obligations (payables, credit cards, taxes, other current "
        "liabilities).",
    "current_ratio": "We divide short-term assets by short-term obligations. "
        "A result of 2.0 means $2 of current assets for every $1 you owe in "
        "the near term.",
    "card_debt_load": "We take your total credit-card balance and divide it "
        "by average monthly revenue over the last three months — expressing "
        "card debt as 'months of revenue.'",
    "quick_burn_check": "We start with cash plus receivables, then subtract "
        "what you owe on payables and cards plus four weeks of typical "
        "outflow. It's a stress test of near-term obligations.",
    "revenue_t12": "We sum all income-account activity over the trailing "
        "twelve months, normalizing the sign so revenue reads as positive.",
    "revenue_trend_3mo": "We compare average monthly revenue over the last "
        "three full months against the three months before that, and show "
        "the percentage change.",
    "revenue_mix": "We total revenue by income account and show each as a "
        "share of the whole.",
    "gross_margin": "We subtract direct job costs (labor, subcontractors, "
        "materials) from revenue, then divide what's left by revenue.",
    "direct_labor_pct": "We total direct-labor and subcontractor costs and "
        "divide by revenue.",
    "overhead_burn": "We total your overhead accounts (rent, admin payroll, "
        "insurance, general overhead) and divide by the number of months. "
        "Depreciation and tax expense are excluded — they aren't cash "
        "overhead.",
    "breakeven_revenue": "We divide monthly overhead by your gross margin — "
        "the revenue you'd need each month just to cover fixed costs.",
    "revenue_per_job": "We group revenue by the job or project prefix on your "
        "transactions.",
    "total_ar": "We total the A/R balance as of period end — opening balance "
        "plus invoices billed minus payments received.",
    "dso_true": "We match real payments to the invoices they paid (by "
        "amount), measure how many days each took, and take a dollar-weighted "
        "average. This uses your actual payment history, not a textbook "
        "estimate.",
    "customer_payment_lag": "For each customer we match payments to invoices "
        "and average the days between. Customers with fewer than three "
        "matched payments fall back to your company-wide median.",
    "aging_distribution": "From your latest A/R aging report, we group every "
        "open invoice into age buckets and show how the open balance splits "
        "across them.",
    "ar_at_risk": "We sum the open balance of every invoice more than 90 days "
        "past due on your latest aging report — the receivables least likely "
        "to be collected.",
    "expected_collections_13wk": "For each open invoice on your latest aging "
        "report we estimate the pay date from how long that customer "
        "typically takes (or your median), discount it by how far past due it "
        "is (older = less likely to collect), and total what should land in "
        "each of the next 13 weeks.",
    "collection_effectiveness": "We divide cash collected during the period "
        "by everything that was collectible — opening A/R plus new billings. "
        "100% means you collected everything that came due.",
    "customer_concentration": "We total billings by customer over the period "
        "and show your top five as a share of the total.",
    "open_ap_due": "From your latest A/P aging report we total open vendor "
        "bills and flag how much is due within 14 and 30 days.",
    "vendor_pay_lag": "We match your bill payments to the bills they paid and "
        "average the days between — how long you typically take to pay.",
    "top5_vendor_concentration": "We total spend by vendor and show your top "
        "five as a share of the total.",
    "recurring_outflow_base": "We scan your cash outflows for repeating "
        "patterns (same payee, regular interval, similar amount) and total "
        "their monthly-equivalent value — the baseline that leaves every "
        "month.",
    "payroll_load": "We total payroll-related outflows (by name match or "
        "payroll-account links) and divide by your total cash outflow.",
    "card_cycle_exposure": "We total your credit-card balances and infer the "
        "typical payment day from your card history.",
    "job_cash_demand": "We group open payables by job to show which projects "
        "are tying up cash in unpaid bills.",
}

_CONF_LABEL = {"high": "Solid data", "medium": "Fair data",
               "low": "Limited data"}


def _money(v) -> str:
    if v is None:
        return "—"
    v = round(v)
    return ("-$" if v < 0 else "$") + f"{abs(v):,}"


def _humanize_key(key: str) -> str:
    text = str(key).replace("_", " ").replace("pct", "%").strip()
    return text[:1].upper() + text[1:] if text else text


def _fmt_detail_value(value) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if value is None:
        return "—"
    if isinstance(value, float):
        if value == int(value):
            return f"{int(value):,}"
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return _esc(value)


def _detail_table(obj) -> str:
    if isinstance(obj, dict):
        if not obj:
            return "<span class='muted'>—</span>"
        values = list(obj.values())
        # A dict whose values are all (non-empty) dicts is a table of records:
        # render one row per entry with the inner fields as columns.
        if values and all(isinstance(v, dict) and v for v in values):
            cols: list = []
            for v in values:
                for k in v:
                    if k not in cols:
                        cols.append(k)
            head = "<th>Item</th>" + "".join(
                f"<th>{_esc(_humanize_key(c))}</th>" for c in cols)
            body = ""
            for name, v in list(obj.items())[:120]:
                body += ("<tr><th>" + _esc(name) + "</th>" + "".join(
                    f"<td>{_fmt_detail_value(v.get(c))}</td>" for c in cols)
                    + "</tr>")
            return (f"<table class='detail records'><thead><tr>{head}</tr>"
                    f"</thead><tbody>{body}</tbody></table>")
        rows = ""
        for key, value in obj.items():
            label = _esc(_humanize_key(str(key)))
            cell = (_detail_table(value)
                    if isinstance(value, (dict, list)) and value
                    else _fmt_detail_value(value))
            rows += f"<tr><th>{label}</th><td>{cell}</td></tr>"
        return f"<table class='detail'>{rows}</table>"
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            keys = []
            for item in obj:
                for k in item:
                    if k not in keys:
                        keys.append(k)
            head = "".join(f"<th>{_esc(_humanize_key(k))}</th>" for k in keys)
            body = ""
            for item in obj[:50]:
                body += "<tr>" + "".join(
                    f"<td>{_fmt_detail_value(item.get(k))}</td>" for k in keys
                ) + "</tr>"
            return (f"<table class='detail'><thead><tr>{head}</tr></thead>"
                    f"<tbody>{body}</tbody></table>")
        return _esc(", ".join(_fmt_detail_value(x) for x in obj[:50]))
    return _fmt_detail_value(obj)


# Per-KPI benchmark goal lines, expressed in the metric's DISPLAYED units.
# better = the direction that is healthier (for shading good vs bad).
BENCHMARKS = {
    "weeks_of_cash": (8.0, "higher", "8-week cushion"),
    "current_ratio": (2.0, "higher", "2.0 healthy"),
    "card_debt_load": (1.0, "lower", "1-month guide"),
    "gross_margin": (0.40, "higher", "40% target"),
    "direct_labor_pct": (35.0, "lower", "35% guide"),
    "revenue_trend_3mo": (0.0, "higher", "flat (0%)"),
    "dso_true": (45.0, "lower", "45-day target"),
    "collection_effectiveness": (95.0, "higher", "95% target"),
    "customer_concentration": (30.0, "lower", "30% risk line"),
    "vendor_pay_lag": (30.0, "higher", "~30 days"),
    "payroll_load": (30.0, "lower", "30% guide"),
}
_AGE_ORDER = ["CURRENT", "1 - 30 days past due", "31 - 60 days past due",
              "61 - 90 days past due", "91 or more days past due"]
_AGE_SHORT = {"CURRENT": "Current", "1 - 30 days past due": "1-30d",
              "31 - 60 days past due": "31-60d",
              "61 - 90 days past due": "61-90d",
              "91 or more days past due": "91d+"}


def _display_value(kpi: dict, value):
    """Scale a raw KPI value into the units the tile shows, with a unit tag."""
    if value is None:
        return None, "number"
    detail = kpi.get("detail") or {}
    if detail.get("unit_is") == "days":
        return value, "days"
    unit = kpi["unit"]
    if unit == "percent" and kpi["key"] in _FRACTION_PERCENT:
        return value * 100, "percent"
    return value, unit


def _headline_for(kpi: dict) -> str | None:
    """A display string for tiles whose primary value is None (detail-only)."""
    if kpi["value"] is not None:
        return None
    d = kpi.get("detail") or {}
    key = kpi["key"]
    if key == "customer_payment_lag" and d.get("client_median_lag") is not None:
        return f"{round(d['client_median_lag'])} days (median)"
    if key == "aging_distribution":
        cur = (d.get("CURRENT") or {}).get("pct_of_open")
        if cur is not None:
            return f"{cur:.0f}% current"
    if key == "revenue_mix" and d:
        return f"Top stream {max(d.values()):.0f}%"
    if key == "revenue_per_job" and d:
        return f"{len(d)} job(s)"
    return None


def _kpi_chart(kpi: dict, history: dict) -> dict | None:
    """Build a small chart spec (rendered client-side) for a KPI's modal."""
    key = kpi["key"]
    d = kpi.get("detail") or {}

    if key == "revenue_t12" and d.get("monthly"):
        months = sorted(d["monthly"])
        return {"type": "line", "labels": [m[5:] for m in months],
                "values": [round(d["monthly"][m], 2) for m in months],
                "unit": "currency", "caption": "Monthly revenue, last 12 months"}
    if key == "expected_collections_13wk" and d.get("weekly"):
        w = d["weekly"]
        return {"type": "bar", "labels": [f"W{i+1}" for i in range(len(w))],
                "values": w, "unit": "currency",
                "caption": "Projected collections by week"}
    if key == "aging_distribution" and d:
        labels, values = [], []
        for b in _AGE_ORDER:
            if b in d:
                labels.append(_AGE_SHORT[b])
                values.append(round(d[b]["open_total"], 2))
        if values:
            return {"type": "bar", "labels": labels, "values": values,
                    "unit": "currency", "caption": "Open A/R by age"}
    if key == "revenue_mix" and d:
        items = sorted(d.items(), key=lambda kv: kv[1], reverse=True)[:8]
        return {"type": "bar", "labels": [n for n, _ in items],
                "values": [v for _, v in items], "unit": "percent",
                "caption": "Revenue by income stream"}
    if key in ("customer_concentration", "top5_vendor_concentration"):
        src = d.get("by_vendor", d)
        items = [(n, v) for n, v in src.items() if isinstance(v, (int, float))]
        items.sort(key=lambda kv: kv[1], reverse=True)
        if items:
            bm = BENCHMARKS.get(key)
            return {"type": "bar", "labels": [n for n, _ in items[:8]],
                    "values": [v for _, v in items[:8]], "unit": "percent",
                    "benchmark": bm[0] if bm else None,
                    "benchmark_label": bm[2] if bm else None,
                    "caption": "Share of the total"}
    if key == "customer_payment_lag" and d.get("by_customer"):
        rows = [(n, v.get("avg_lag_days"))
                for n, v in d["by_customer"].items()
                if v.get("avg_lag_days") is not None]
        rows.sort(key=lambda kv: kv[1], reverse=True)
        if rows:
            return {"type": "bar", "labels": [n for n, _ in rows[:10]],
                    "values": [v for _, v in rows[:10]], "unit": "days",
                    "benchmark": 45, "benchmark_label": "45-day target",
                    "caption": "Average days to pay, by customer"}

    # Fall back to a value-over-time trend with a benchmark goal line.
    series = history.get(key)
    if series:
        scaled = []
        for point in series:
            v, unit = _display_value(kpi, point["value"])
            scaled.append({"date": point["date"][5:], "value": v})
        bm = BENCHMARKS.get(key)
        return {"type": "line", "labels": [p["date"] for p in scaled],
                "values": [p["value"] for p in scaled],
                "unit": unit,
                "benchmark": bm[0] if bm else None,
                "benchmark_label": bm[2] if bm else None,
                "caption": "This metric over time"}
    return None


def _snapshot_history(conn, sections: dict, as_of: str) -> None:
    try:
        for kpis in sections.values():
            for k in kpis:
                if isinstance(k["value"], (int, float)) and \
                        not isinstance(k["value"], bool):
                    conn.execute(
                        "INSERT INTO kpi_history (as_of, key, value) "
                        "VALUES (?, ?, ?) ON CONFLICT(as_of, key) "
                        "DO UPDATE SET value = excluded.value",
                        (as_of, k["key"], k["value"]),
                    )
        conn.commit()
    except Exception:
        conn.rollback()


def _load_history(conn) -> dict:
    from collections import defaultdict as _dd
    hist = _dd(list)
    for r in conn.execute(
        "SELECT as_of, key, value FROM kpi_history ORDER BY as_of"
    ):
        hist[r["key"]].append({"date": r["as_of"], "value": r["value"]})
    return hist


def _render_kpi(kpi: dict) -> str:
    meaning, why = KPI_INFO.get(kpi["key"], ("", ""))
    how = KPI_HOW.get(kpi["key"], "")
    conf = kpi["confidence"]
    conf_pill = (f'<span class="conf conf-{_esc(conf)}">'
                 f'{_esc(_CONF_LABEL.get(conf, conf))}</span>')
    note = ""
    if kpi["notes"]:
        note = f'<div class="kpi-note">{_esc(kpi["notes"][0])}</div>'
    meaning_html = (f'<div class="kpi-meaning">{_esc(meaning)}</div>'
                    if meaning else "")
    more = ""
    if how:
        more += f'<h4>How this is calculated</h4><p>{_esc(how)}</p>'
    if why:
        more += f'<h4>Why it matters</h4><p>{_esc(why)}</p>'
    for n in kpi["notes"]:
        more += f'<p class="modal-note">{_esc(n)}</p>'
    if kpi["detail"]:
        more += '<h4>The supporting numbers</h4>' + _detail_table(kpi["detail"])
    # Tiles with no primary value (detail-only) get a derived headline so the
    # client always sees a number, not a blank.
    display = _fmt_value(kpi)
    if kpi["value"] is None:
        headline = kpi.get("headline")
        display = headline if headline else "—"
        value_cls = "kpi-value text" if headline else "kpi-value muted"
    else:
        value_cls = "kpi-value"
    has_chart = bool(kpi.get("chart"))
    cls = "kpi clickable" if (more or has_chart) else "kpi"
    extra = (f' tabindex="0" role="button" data-key="{_esc(kpi["key"])}"'
             f' data-title="{_esc(kpi["label"])}"'
             f' data-value="{_esc(display)}"') if (more or has_chart) else ""
    cue = ('<span class="kpi-cue">View details →</span>'
           if (more or has_chart) else "")
    more_block = f'<div class="kpi-more" hidden>{more}</div>' if more else ""
    return (
        f'<div class="{cls}"{extra}>'
        f'<div class="kpi-label">{_esc(kpi["label"])}</div>'
        f'<div class="{value_cls}">{_esc(display)}</div>'
        f'{meaning_html}{note}'
        f'<div class="kpi-foot">{conf_pill}{cue}</div>{more_block}</div>'
    )


def _render_section(title: str, section_id: str, kpis: list[dict],
                    intro: str = "") -> str:
    cards = "".join(_render_kpi(k) for k in kpis)
    intro_html = f'<p class="section-intro">{_esc(intro)}</p>' if intro else ""
    return (
        f'<section id="{section_id}"><div class="wrap">'
        f'<h2>{_esc(title)}</h2>{intro_html}'
        f'<div class="kpi-grid">{cards}</div></div></section>'
    )


def _render_flags(flags: dict) -> str:
    parts = [
        '<section id="flags"><div class="wrap">'
        '<h2>Bookkeeping Quality Checks</h2>',
        '<p class="section-intro">Issues worth cleaning up — they can distort '
        'the numbers above until resolved.</p>',
        f'<p class="summary">{flags["unmapped_count"]} account(s) still '
        f'uncategorized; {len(flags["queue"])} awaiting review.</p>',
    ]

    def table(title, rows, render_row):
        if not rows:
            return f'<p class="ok">{_esc(title)}: none — good.</p>'
        body = "".join(render_row(r) for r in rows)
        return (
            f"<h3>{_esc(title)}</h3><table class='flags-table'><tbody>{body}"
            "</tbody></table>"
        )

    parts.append(
        table(
            "Clearing / suspense accounts holding a balance",
            flags["flagged_accounts"],
            lambda r: f"<tr><td>{_esc(r['name'])}</td>"
            f"<td class='num'>{_money(r['balance'])}</td>"
            f"<td>{_esc(r['reason'])}</td></tr>",
        )
    )
    parts.append(
        table(
            "Dormant accounts still carrying a balance",
            flags["dormant_nonzero"],
            lambda r: f"<tr><td>{_esc(r['name'])}</td>"
            f"<td class='num'>{_money(r['balance'])}</td></tr>",
        )
    )
    if flags["queue"]:
        names = ", ".join(_esc(n) for n in flags["queue"])
        parts.append(f"<h3>Awaiting category review</h3><p>{names}</p>")
    parts.append("</div></section>")
    return "".join(parts)


def _render_appendix(data: dict) -> str:
    parts = ['<section id="appendix"><div class="wrap">'
             '<h2>Underlying Data</h2>'
             '<p class="section-intro">The raw figures behind every metric, '
             'for auditing or export.</p>']
    blocks = [
        ("Liquidity", data["kpis"]["liquidity"]),
        ("Revenue", data["kpis"]["revenue"]),
        ("Receivables", data["kpis"]["receivables"]),
        ("Payables & Disbursements", data["kpis"]["disbursements"]),
        ("Forecast", data["forecast"]),
        ("Bookkeeping checks", data["flags"]),
    ]
    for title, payload in blocks:
        body = _esc(json.dumps(payload, indent=2))
        parts.append(
            f'<details class="raw"><summary>{_esc(title)} data</summary>'
            f"<pre>{body}</pre></details>"
        )
    parts.append("</div></section>")
    return "".join(parts)


def _render_header(data: dict) -> str:
    period = data["period"]
    span = (
        f'{period["start"]} – {period["end"]}'
        if period["start"]
        else "no period"
    )
    banner = data["change_banner"]["text"]
    banner_inner = ""
    if banner:
        items = "".join(
            f"<li>{_esc(i)}</li>" for i in data["change_banner"]["items"]
        )
        banner_inner = (
            f'<strong>{_esc(banner)}</strong><ul>{items}</ul>'
        )
    return (
        '<header class="strip"><div class="wrap">'
        f'<h1>{_esc(data["client"])}</h1>'
        f'<div class="meta">Period {_esc(span)} &middot; generated '
        f'{_esc(data["generated_at"])}</div></div></header>'
        + (f'<div class="banner"><div class="wrap">{banner_inner}</div></div>'
           if banner else "")
    )


def _kpi_value(kpis, key):
    for k in kpis:
        if k["key"] == key:
            return k["value"]
    return None


def _executive_summary(data: dict) -> str:
    liq = data["kpis"]["liquidity"]
    cash = _kpi_value(liq, "cash_on_hand")
    weeks = _kpi_value(liq, "weeks_of_cash")
    forecast = data["forecast"].get("BASE", {})
    weeks_list = forecast.get("weeks", [])
    sentences = []
    if cash is not None:
        s = f"{data['client']} is holding {_money(cash)} in cash"
        if weeks is not None:
            s += f" — roughly {weeks:.1f} weeks of runway"
        sentences.append(s + ".")
    if weeks_list:
        end = weeks_list[-1]["ending_cash"]
        begin = forecast.get("beginning_cash", end)
        direction = "falls" if end < begin else "holds at" if end == begin \
            else "rises"
        s = f"Under the base plan, projected cash {direction} {_money(end)} " \
            "over the next 13 weeks"
        breach = forecast.get("breach_weeks") or []
        if breach:
            s += (f", dropping below the {_money(forecast.get('floor', 0))} "
                  f"floor in week {breach[0]}")
        sentences.append(s + ".")
        collections = sum(w["inflows"]["collections"] for w in weeks_list)
        if collections == 0:
            sentences.append(
                "No collections are projected yet because no A/R aging report "
                "has been uploaded — adding it will sharpen the incoming-cash "
                "side of the forecast."
            )
    return " ".join(sentences) or "Upload reports and update the dashboard " \
        "to see a summary."


def _render_summary_section(data: dict) -> str:
    return (
        '<section id="summary" class="exec"><div class="wrap">'
        '<h2>Executive summary</h2>'
        f'<p class="lead">{_esc(_executive_summary(data))}</p></div></section>'
    )


def _render_forecast_section() -> str:
    buttons = "".join(
        f'<button type="button" data-scn="{scn}">{scn.title()}</button>'
        for scn in SCENARIO_KEYS
    )
    return (
        '<section id="forecast"><div class="wrap">'
        '<h2>13-Week Cash Forecast</h2>'
        '<p class="section-intro">Projected bank balance week by week. The '
        'dashed line is the cash floor; red points are weeks that fall below '
        'it.</p>'
        f'<div class="scn-bar no-print"><span class="scn-lbl">Scenario:</span>'
        f'{buttons}<span id="scn-note" class="scn-note"></span></div>'
        '<p id="forecast-narrative" class="lead"></p>'
        '<div id="forecast-charts" class="chart-wrap"></div>'
        '<div id="forecast-table"></div>'
        '<div class="tier-legend"><h3>How each line is projected from your '
        'books</h3><ul>'
        '<li><span class="tier tier-scheduled">scheduled</span> A known, fixed '
        'obligation read straight from your data — payroll runs, recurring '
        'bills, scheduled vendor payments, and tax balances. Highest '
        'certainty.</li>'
        '<li><span class="tier tier-behavioral">behavioral</span> Estimated '
        'from your own history — how fast your customers actually pay, plus '
        'your recurring card-payment and owner-draw patterns. A solid '
        'estimate that moves with behavior.</li>'
        '<li><span class="tier tier-manual">manual</span> We could not infer '
        'this from your books, so it stays at zero until you enter it — for '
        'example, future billings when there is no recurring-revenue pattern '
        'to learn from.</li>'
        '</ul></div></div></section>'
    )


# ── full document ────────────────────────────────────────────────────────────

_STYLE = """
:root{--green:#1a7f37;--amber:#9a6700;--red:#cf222e;--blue:#0969da;
--ink:#1c2128;--muted:#636c76;--line:#e1e5ea;--bg:#f6f8fa;--card:#fff;}
*{box-sizing:border-box;}
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,
Arial,sans-serif;color:var(--ink);margin:0;background:#f0f2f5;
line-height:1.5;font-size:15px;}
header.strip{background:linear-gradient(135deg,#1c2128,#2d333b);color:#fff;
padding:22px 0;}
.wrap{max-width:1720px;margin:0 auto;padding:0 32px;}
header.strip h1{margin:0;font-size:24px;font-weight:650;letter-spacing:-.01em;}
.meta{font-size:13px;opacity:.75;margin-top:5px;}
.banner{background:#fff8c5;border-bottom:1px solid #e6c84b;}
.banner .wrap{padding-top:12px;padding-bottom:12px;font-size:13px;}
.banner strong{display:block;margin-bottom:4px;}
.banner ul{margin:0;padding-left:18px;color:var(--muted);}
section{background:transparent;}
section>.wrap{padding-top:26px;padding-bottom:26px;}
section+section>.wrap{border-top:1px solid #dfe3e8;}
h2{font-size:13px;margin:0 0 14px;text-transform:uppercase;letter-spacing:.06em;
color:var(--muted);font-weight:700;}
h3{font-size:14px;margin:18px 0 8px;}
.exec{background:#fff;}
.lead{font-size:17px;line-height:1.6;color:#24292f;margin:0;max-width:760px;}
.section-intro{font-size:13px;color:var(--muted);margin:-6px 0 14px;
max-width:720px;}
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,
1fr));gap:14px;align-items:start;}
.kpi{border:1px solid var(--line);border-radius:12px;padding:16px;
background:var(--card);box-shadow:0 1px 2px rgba(27,33,40,.04);
display:flex;flex-direction:column;transition:box-shadow .12s,border-color .12s;}
.kpi.clickable{cursor:pointer;}
.kpi.clickable:hover{border-color:var(--blue);
box-shadow:0 3px 12px rgba(9,105,218,.13);}
.kpi-cue{font-size:11px;color:var(--blue);font-weight:600;margin-left:auto;}
.kpi-more{display:none;}
.kpi-label{font-size:12px;color:var(--muted);font-weight:600;
text-transform:uppercase;letter-spacing:.03em;}
.kpi-value{font-size:28px;font-weight:680;margin:4px 0 6px;
letter-spacing:-.02em;font-variant-numeric:tabular-nums;}
.kpi-meaning{font-size:12.5px;color:#48515a;line-height:1.45;}
.kpi-note{font-size:11.5px;color:var(--amber);margin-top:6px;}
.kpi-foot{margin-top:auto;padding-top:10px;display:flex;align-items:center;
gap:8px;}
.conf{font-size:10.5px;font-weight:600;border-radius:999px;padding:2px 9px;
border:1px solid transparent;}
.conf-high{background:#e7f6ec;color:#1a7f37;}
.conf-medium{background:#fdf3e3;color:#9a6700;}
.conf-low{background:#eef1f4;color:#636c76;}
details.proof{margin-top:10px;border-top:1px solid var(--line);padding-top:8px;}
details.proof>summary{cursor:pointer;font-size:12px;color:var(--blue);
font-weight:600;list-style:none;}
details.proof>summary::-webkit-details-marker{display:none;}
details.proof>summary::before{content:"▸ ";}
details.proof[open]>summary::before{content:"▾ ";}
.proof-body{margin-top:8px;}
.why{font-size:12.5px;color:#48515a;margin:0 0 8px;}
.proof-label{font-size:11px;text-transform:uppercase;letter-spacing:.05em;
color:var(--muted);font-weight:700;margin-bottom:5px;}
table.detail{border-collapse:collapse;width:100%;font-size:12px;}
table.detail th,table.detail td{border:1px solid var(--line);padding:4px 8px;
text-align:left;vertical-align:top;}
table.detail th{background:var(--bg);color:#48515a;font-weight:600;
white-space:nowrap;}
.muted{color:var(--muted);}
.chart-wrap{background:#fff;border:1px solid var(--line);border-radius:12px;
padding:14px 10px 6px;margin:6px 0 18px;}
svg.chart{width:100%;height:auto;display:block;}
svg .axis{font-size:11px;fill:var(--muted);}
svg .axis2{font-size:9.5px;fill:#9aa4ae;}
.scn-bar{display:flex;align-items:center;gap:8px;margin:10px 0 6px;
flex-wrap:wrap;}
.scn-lbl{font-size:12px;color:var(--muted);font-weight:600;}
.scn-note{font-size:12px;color:var(--muted);}
.scn-bar button{border:1px solid var(--line);background:#fff;padding:6px 14px;
border-radius:999px;cursor:pointer;font-size:13px;font-weight:600;
color:#48515a;}
body[data-scenario="BASE"] button[data-scn="BASE"],
body[data-scenario="STRETCH"] button[data-scn="STRETCH"],
body[data-scenario="CRUNCH"] button[data-scn="CRUNCH"]{
background:var(--ink);color:#fff;border-color:var(--ink);}
table.fc{border-collapse:collapse;width:100%;font-size:12px;background:#fff;}
table.fc th,table.fc td{border:1px solid var(--line);padding:5px 8px;
white-space:nowrap;}
table.fc td.num{text-align:right;font-variant-numeric:tabular-nums;}
table.fc thead th{background:var(--bg);position:sticky;top:0;}
tr.fc-row{cursor:pointer;}
tr.fc-row:hover{background:#eef6ff;}
.fc-cue{font-size:9px;color:var(--blue);text-transform:uppercase;
font-weight:700;opacity:0;letter-spacing:.03em;}
tr.fc-row:hover .fc-cue{opacity:1;}
td.breach{background:#ffebe9;color:var(--red);font-weight:700;}
.row-head td{font-weight:700;background:#f0f2f5;}
.row-total td{font-weight:700;border-top:2px solid #c4ccd4;}
.tier{font-size:9px;text-transform:uppercase;font-weight:700;border-radius:
4px;padding:1px 5px;margin-left:6px;color:#fff;letter-spacing:.02em;}
.tier-scheduled{background:#57606a;}
.tier-behavioral{background:var(--amber);}
.tier-manual{background:#8250df;}
.flags-table{border-collapse:collapse;width:100%;font-size:13px;
background:#fff;}
.flags-table td{border:1px solid var(--line);padding:6px 9px;}
.flags-table td.num{text-align:right;font-variant-numeric:tabular-nums;}
.ok,.summary{font-size:13px;color:var(--muted);}
.tier-legend{margin-top:18px;background:#fff;border:1px solid var(--line);
border-radius:12px;padding:16px 18px;}
.tier-legend h3{margin:0 0 10px;font-size:13px;}
.tier-legend ul{list-style:none;margin:0;padding:0;}
.tier-legend li{font-size:13px;color:#48515a;margin-bottom:8px;line-height:1.5;}
.modal{position:fixed;inset:0;background:rgba(27,33,40,.5);display:flex;
align-items:center;justify-content:center;padding:24px;z-index:100;}
.modal[hidden]{display:none;}
.modal-card{background:#fff;border-radius:16px;max-width:680px;width:100%;
max-height:86vh;overflow:auto;padding:24px 28px 28px;position:relative;
box-shadow:0 20px 60px rgba(0,0,0,.35);}
.modal-close{position:absolute;top:12px;right:16px;border:none;background:none;
font-size:26px;line-height:1;cursor:pointer;color:var(--muted);}
.modal-head{border-bottom:1px solid var(--line);padding-bottom:14px;
margin-bottom:16px;}
.modal-head h3{margin:0;font-size:13px;text-transform:uppercase;
letter-spacing:.05em;color:var(--muted);}
.modal-value{font-size:32px;font-weight:680;margin-top:4px;
font-variant-numeric:tabular-nums;}
.modal-body h4{font-size:11px;text-transform:uppercase;letter-spacing:.05em;
color:var(--muted);font-weight:700;margin:18px 0 6px;}
.modal-body h4:first-child{margin-top:0;}
.modal-body p{margin:0 0 8px;font-size:14.5px;line-height:1.6;color:#24292f;}
.modal-body .modal-note{color:var(--amber);font-size:13px;}
.modal-body table.detail{margin-top:4px;}
.modal-chart{margin:0 0 14px;padding:12px 8px 6px;}
.chart-cap{font-size:12px;color:var(--muted);margin:0 0 6px;font-weight:600;}
table.detail.records tbody th{text-align:left;font-weight:600;background:#fff;
white-space:normal;max-width:260px;}
table.detail.records td{text-align:right;font-variant-numeric:tabular-nums;}
.kpi-value.muted{color:var(--muted);}
.kpi-value.text{font-size:20px;font-weight:650;}
.kpi-value{overflow-wrap:anywhere;}
details.raw{margin-top:8px;font-size:12px;}
details.raw pre{background:#fff;border:1px solid var(--line);border-radius:8px;
padding:10px;overflow:auto;max-height:280px;}
.print-header{display:none;}
@media print{
body{background:#fff;font-size:12px;}
.no-print,.scn-bar,.kpi-cue,.modal{display:none!important;}
details.proof,details.raw{display:none!important;}
.kpi{box-shadow:none;break-inside:avoid;}
section{page-break-before:always;}
.print-header{display:block;position:fixed;top:0;left:0;right:0;
font-size:10px;color:var(--muted);border-bottom:1px solid var(--line);
padding:3px 24px;background:#fff;}
}
"""

_SCRIPT = r"""
var DATA = JSON.parse(document.getElementById("data").textContent);
function money(v){return (v==null)?"—":((v<0?"-$":"$")+
  Math.abs(Math.round(v)).toLocaleString());}
function shortMoney(v){
  var a=Math.abs(v), sign=v<0?"-$":"$";
  if(a>=1e6) return sign+(a/1e6).toFixed(a>=1e7?0:1)+"M";
  if(a>=1e3) return sign+Math.round(a/1e3)+"k";
  return sign+Math.round(a);
}
var SCN_NOTE={
  BASE:"Most-likely assumptions.",
  STRETCH:"Optimistic: collections a week sooner, gentler discounts, draws on.",
  CRUNCH:"Conservative: collections two weeks later, harsh discounts, owner "+
    "draws paused, discretionary spend cut 25%."
};
function cashChart(f){
  var wk=f.weeks, n=wk.length;
  var W=1040,H=320,L=68,R=120,T=18,B=52;
  var vals=wk.map(function(w){return w.ending_cash;});
  var lo=Math.min(f.floor,f.beginning_cash,Math.min.apply(null,vals));
  var hi=Math.max(f.floor,f.beginning_cash,Math.max.apply(null,vals));
  var span=(hi-lo)||1; lo-=span*0.12; hi+=span*0.12;
  function X(i){return L+(W-L-R)*(i/(n-1));}
  function Y(v){return T+(H-T-B)*(1-(v-lo)/(hi-lo));}
  var s="";
  for(var g=0;g<=4;g++){
    var gv=lo+(hi-lo)*g/4, gy=Y(gv);
    s+='<line x1="'+L+'" y1="'+gy+'" x2="'+(W-R)+'" y2="'+gy+
       '" stroke="#eef1f4"/>';
    s+='<text x="'+(L-8)+'" y="'+(gy+4)+'" text-anchor="end" class="axis">'+
       shortMoney(gv)+'</text>';
  }
  // area under the line
  var area="M "+X(0)+" "+Y(vals[0]);
  for(var i=1;i<n;i++) area+=" L "+X(i)+" "+Y(vals[i]);
  area+=" L "+X(n-1)+" "+Y(lo)+" L "+X(0)+" "+Y(lo)+" Z";
  s+='<path d="'+area+'" fill="#0969da" opacity="0.06"/>';
  // floor line
  var fy=Y(f.floor);
  s+='<line x1="'+L+'" y1="'+fy+'" x2="'+(W-R)+'" y2="'+fy+
     '" stroke="#cf222e" stroke-width="1.5" stroke-dasharray="6 4"/>';
  s+='<text x="'+(W-R+8)+'" y="'+(fy+4)+'" class="axis" fill="#cf222e">Floor '+
     shortMoney(f.floor)+'</text>';
  // cash line
  var pts=[];
  for(i=0;i<n;i++) pts.push(X(i).toFixed(1)+","+Y(vals[i]).toFixed(1));
  s+='<polyline fill="none" stroke="#0969da" stroke-width="2.5" points="'+
     pts.join(" ")+'"/>';
  // points + x labels
  var breach={}; f.breach_weeks.forEach(function(b){breach[b]=1;});
  for(i=0;i<n;i++){
    var br=breach[wk[i].index];
    s+='<circle cx="'+X(i)+'" cy="'+Y(vals[i])+'" r="3.6" fill="'+
       (br?"#cf222e":"#0969da")+'"/>';
    if(i===0||i===n-1||i%2===1){
      s+='<text x="'+X(i)+'" y="'+(H-B+18)+'" text-anchor="middle" class="axis">W'+
         wk[i].index+'</text>';
      s+='<text x="'+X(i)+'" y="'+(H-B+31)+'" text-anchor="middle" class="axis2">'+
         wk[i].start_date.slice(5)+'</text>';
    }
  }
  // endpoint label
  s+='<text x="'+(X(n-1))+'" y="'+(Y(vals[n-1])-9)+'" text-anchor="end" '+
     'class="axis" fill="#0969da">'+shortMoney(vals[n-1])+'</text>';
  s+='<line x1="'+L+'" y1="'+(H-B)+'" x2="'+(W-R)+'" y2="'+(H-B)+
     '" stroke="#d0d7de"/>';
  return '<svg viewBox="0 0 '+W+' '+H+'" class="chart" role="img">'+
    '<title>Projected ending cash by week</title>'+s+'</svg>';
}
var INFLOWS=[["collections","Collections"],["new_billings","New billings"],
  ["other","Other income"]];
var OUTFLOWS=[["payroll","Payroll"],["recurring","Recurring bills"],
  ["ap_scheduled","Vendor bills (A/P)"],["card_payments","Card payments"],
  ["taxes","Taxes"],["owner_draws","Owner draws"]];
function tier(t){return '<span class="tier tier-'+t+'">'+t+'</span>';}
function fcTable(f){
  var wk=f.weeks, breach={};
  f.breach_weeks.forEach(function(i){breach[i]=1;});
  var head="<tr><th>Line item</th>";
  wk.forEach(function(w){head+="<th>W"+w.index+"<br><span class='axis2'>"+
    w.start_date.slice(5)+"</span></th>";});
  head+="</tr>";
  function group(label,rows,bucket){
    var out="<tr class='row-head'><td colspan='"+(wk.length+1)+"'>"+label+
      "</td></tr>";
    rows.forEach(function(r){
      var key=r[0],name=r[1];
      out+="<tr class='fc-row' data-fam='"+key+"' tabindex='0'><td>"+name+
        tier(wk[0].confidence_tier[key])+
        " <span class='fc-cue'>explain</span></td>";
      wk.forEach(function(w){out+="<td class='num'>"+money(w[bucket][key])+
        "</td>";});
      out+="</tr>";
    });
    return out;
  }
  var body=group("Cash in",INFLOWS,"inflows")+
    group("Cash out",OUTFLOWS,"outflows");
  body+="<tr class='row-total'><td>Net change</td>";
  wk.forEach(function(w){body+="<td class='num'>"+money(w.net)+"</td>";});
  body+="</tr><tr class='row-total'><td>Ending cash</td>";
  wk.forEach(function(w){
    body+="<td class='"+(breach[w.index]?"num breach":"num")+"'>"+
      money(w.ending_cash)+"</td>";
  });
  body+="</tr>";
  return "<div style='overflow:auto'><table class='fc'><thead>"+head+
    "</thead><tbody>"+body+"</tbody></table></div>";
}
function narrative(f){
  var wk=f.weeks; if(!wk.length) return "";
  var end=wk[wk.length-1].ending_cash, beg=f.beginning_cash;
  var inflow=0,outflow=0,byFam={};
  wk.forEach(function(w){
    for(var k in w.inflows) inflow+=w.inflows[k];
    for(var k2 in w.outflows){outflow+=w.outflows[k2];
      byFam[k2]=(byFam[k2]||0)+w.outflows[k2];}
  });
  var names={payroll:"payroll",recurring:"recurring bills",
    ap_scheduled:"vendor bills",card_payments:"card payments",taxes:"taxes",
    owner_draws:"owner draws"};
  var top=Object.keys(byFam).sort(function(a,b){return byFam[b]-byFam[a];})[0];
  var t="Cash "+(end<beg?"declines":"holds")+" from "+money(beg)+" to "+
    money(end)+" over 13 weeks. About "+money(inflow)+" comes in and "+
    money(outflow)+" goes out";
  if(top&&byFam[top]>0) t+="; the largest outflow is "+(names[top]||top)+" ("+
    money(byFam[top])+")";
  t+=".";
  if(f.breach_weeks.length) t+=" Cash falls below the "+money(f.floor)+
    " floor starting in week "+f.breach_weeks[0]+".";
  return t;
}
function renderForecast(scn){
  var f=DATA.forecast[scn];
  if(!f){return;}
  document.getElementById("forecast-charts").innerHTML=cashChart(f);
  document.getElementById("forecast-table").innerHTML=fcTable(f);
  document.getElementById("forecast-narrative").textContent=narrative(f);
  var note=document.getElementById("scn-note");
  if(note) note.textContent=SCN_NOTE[scn]||"";
  document.body.setAttribute("data-scenario",scn);
}
var FAM_LABEL={collections:"Collections",new_billings:"New billings",
  other:"Other income",payroll:"Payroll",recurring:"Recurring bills",
  ap_scheduled:"Vendor bills (A/P)",card_payments:"Card payments",
  taxes:"Taxes",owner_draws:"Owner draws"};
var FAM_BUCKET={collections:"inflows",new_billings:"inflows",other:"inflows"};
var FC_HOW={
  collections:"We take every open invoice on your latest A/R aging report and "+
    "schedule each one to the week we expect it to be paid, based on how fast "+
    "that customer has paid in the past. Invoices already overdue are spread "+
    "across a recovery window (recent ones sooner, stale ones later) and "+
    "discounted by how old they are.",
  new_billings:"Future invoicing you haven't booked yet. If we detect a "+
    "recurring-revenue pattern (3+ monthly deposits from the same payers) we "+
    "project it forward; otherwise this stays at zero until you enter a "+
    "billing schedule in Settings.",
  other:"A placeholder for miscellaneous inflows; not projected automatically.",
  payroll:"We found your payroll runs in the cash transactions and project "+
    "them forward on the same cadence (weekly, biweekly, or monthly) at your "+
    "typical amount.",
  recurring:"Bills that repeat — same payee, regular interval, similar amount "+
    "— projected forward on their cadence. Protected costs (rent, insurance, "+
    "loan payments) are kept in full; discretionary ones get trimmed under "+
    "the Crunch scenario.",
  ap_scheduled:"Your open vendor bills from the latest A/P aging report, each "+
    "placed on its due date — and pushed later if you've historically paid "+
    "that vendor past terms.",
  card_payments:"One payment per credit card per month, on the day of the "+
    "month you usually pay, sized to your typical past payment.",
  taxes:"Sales tax currently owed is scheduled at the next month-end. Any "+
    "detected payroll-tax payments continue on their own cadence.",
  owner_draws:"Your detected owner-draw pattern, continued forward. Paused "+
    "entirely under the Crunch scenario."
};
function jsTable(rows,cols){
  if(!rows||!rows.length) return "";
  var h="<tr>"+cols.map(function(c){return "<th>"+c[1]+"</th>";}).join("")+"</tr>";
  var b=rows.map(function(r){return "<tr>"+cols.map(function(c){
    var v=r[c[0]]; if(v==null) v="—";
    else if(c[2]==="money") v=money(v); else if(c[2]==="yn") v=v?"Yes":"No";
    return "<td>"+v+"</td>";}).join("")+"</tr>";}).join("");
  return "<table class='detail'><thead>"+h+"</thead><tbody>"+b+"</tbody></table>";
}
function driverHtml(fam,d,weekly){
  if(!d) return "";
  if(fam==="collections"){
    if(!d.has_aging) return "<p>No A/R aging report uploaded, so no "+
      "collections are projected. Upload it to populate this line.</p>";
    var lst=d.invoice_list||[];
    var hdr="<p>"+(d.invoices||0)+" open invoice(s) schedule out to "+
      money(d.total)+" over 13 weeks. Each invoice's expected timing uses "+
      "that customer's average days to pay (or the company median).</p>";
    return hdr+(lst.length?jsTable(lst.map(function(r){
      return {customer:r.customer,invoice:r.invoice||"—",
        open:r.open_balance,days:r.days_to_pay+"d ("+r.lag_basis+")",
        bucket:r.bucket,wk:"W"+r.weeks.join(", W"),proj:r.projected};}),
      [["customer","Customer"],["invoice","Invoice #"],["open","Open","money"],
       ["bucket","Age"],["days","Days to pay"],["wk","Lands"],
       ["proj","Projected","money"]]):"");
  }
  if(fam==="new_billings"){
    var p="<p>Basis: <b>"+(d.basis||"—")+"</b>. "+(d.note||"")+
      (d.weekly_amount?(" Projecting "+money(d.weekly_amount)+" per week."):"")+
      "</p>";
    return p+((d.payers&&d.payers.length)?("<p>Recurring revenue detected "+
      "from these payers:</p>"+jsTable(d.payers,[["payer","Payer"],
      ["monthly_amount","Monthly","money"],["n_seen","Months seen"]])):"");
  }
  if(fam==="payroll")
    return (d&&d.length)?jsTable(d,[["payee","Payee"],["cadence","Cadence"],
      ["typical_amount","Typical","money"],["last_seen","Last seen"]]):
      "<p>No recurring payroll pattern detected.</p>";
  if(fam==="recurring")
    return d.items&&d.items.length?("<p>Discretionary multiplier this scenario: "+
      d.discretionary+"×.</p>"+jsTable(d.items,[["payee","Payee"],
      ["cadence","Cadence"],["typical_amount","Typical","money"],
      ["applied_amount","Used here","money"],["protected","Protected","yn"]])):
      "<p>No recurring bills detected.</p>";
  if(fam==="ap_scheduled")
    return d.bills&&d.bills.length?jsTable(d.bills,[["vendor","Vendor"],
      ["due","Due"],["week","Week"],["amount","Amount","money"]]):
      "<p>No open vendor bills on the latest A/P aging report.</p>";
  if(fam==="card_payments")
    return d.cards&&d.cards.length?jsTable(d.cards,[["card","Card"],
      ["balance","Balance","money"],["payment","Monthly payment","money"],
      ["cycle_day","Pay day"],["basis","Basis"]]):
      "<p>No credit-card balances to pay down in the window.</p>";
  if(fam==="taxes"){
    var s=d.sales_tax?("<p>Sales tax owed "+money(d.sales_tax.amount)+
      " scheduled "+d.sales_tax.due+" (week "+d.sales_tax.week+").</p>"):
      "<p>No sales tax currently owed.</p>";
    return s+(d.payroll_tax&&d.payroll_tax.length?jsTable(d.payroll_tax,
      [["payee","Payee"],["cadence","Cadence"],
       ["typical_amount","Typical","money"]]):"");
  }
  if(fam==="owner_draws")
    return d.paused?"<p>Owner draws are paused under this scenario.</p>":
      (d.items&&d.items.length?jsTable(d.items,[["payee","Payee"],
      ["cadence","Cadence"],["typical_amount","Typical","money"]]):
      "<p>No owner-draw pattern detected.</p>");
  return "";
}
function openForecastModal(fam){
  var scn=document.body.getAttribute("data-scenario")||"BASE";
  var f=DATA.forecast[scn]; if(!f) return;
  var bucket=FAM_BUCKET[fam]||"outflows";
  var weekly=f.weeks.map(function(w){return w[bucket][fam];});
  var total=weekly.reduce(function(a,b){return a+b;},0);
  var tierName=f.weeks[0].confidence_tier[fam];
  var wkRows=f.weeks.map(function(w,i){return {w:"W"+w.index+" ("+
    w.start_date.slice(5)+")",a:weekly[i]};}).filter(function(r){return r.a;});
  var body='<p><span class="tier tier-'+tierName+'">'+tierName+
    '</span> &middot; '+SCN_NOTE[scn]+'</p>'+
    '<h4>How this line is projected</h4><p>'+FC_HOW[fam]+'</p>'+
    '<h4>Why these numbers, from your books</h4>'+
    driverHtml(fam,f.drivers&&f.drivers[fam],weekly)+
    '<h4>When it lands</h4>'+(wkRows.length?jsTable(wkRows,
      [["w","Week"],["a","Amount","money"]]):"<p>Nothing scheduled in the "+
      "next 13 weeks.</p>");
  document.getElementById("kpi-modal-title").textContent=FAM_LABEL[fam]+
    " — "+scn;
  document.getElementById("kpi-modal-value").textContent=money(total)+
    " over 13 weeks";
  document.getElementById("kpi-modal-body").innerHTML=body;
  modal.hidden=false;
}
document.getElementById("forecast-table").addEventListener("click",function(e){
  var row=e.target.closest?e.target.closest(".fc-row"):null;
  if(row) openForecastModal(row.getAttribute("data-fam"));
});
Array.prototype.forEach.call(
  document.querySelectorAll(".scn-bar button"),
  function(b){b.addEventListener("click",function(){
    renderForecast(b.getAttribute("data-scn"));});}
);
renderForecast(DATA.scenario_default||"BASE");
window.onbeforeprint=function(){renderForecast("BASE");};

// Flat lookup of every KPI by key, for the modal charts.
var KPIMAP={};
["liquidity","revenue","receivables","disbursements"].forEach(function(s){
  (DATA.kpis[s]||[]).forEach(function(k){KPIMAP[k.key]=k;});
});
function fmtU(v,unit){
  if(v==null) return "—";
  if(unit==="currency") return shortMoney(v);
  if(unit==="percent") return Math.round(v*10)/10+"%";
  if(unit==="days") return Math.round(v)+"d";
  if(unit==="ratio") return (Math.round(v*100)/100).toFixed(2);
  if(unit==="weeks") return (Math.round(v*10)/10)+"w";
  if(unit==="months") return (Math.round(v*10)/10)+"mo";
  return ""+Math.round(v*100)/100;
}
function kpiChart(spec){
  if(!spec||!spec.values||!spec.values.length) return "";
  var vals=spec.values, n=vals.length, bm=spec.benchmark;
  var W=640,H=230,L=58,R=18,T=14,B=46;
  var all=vals.slice(); if(bm!=null) all.push(bm); all.push(0);
  var lo=Math.min.apply(null,all), hi=Math.max.apply(null,all);
  if(hi===lo){hi+=1;lo-=1;} var pad=(hi-lo)*0.1; hi+=pad; lo-=pad;
  function Y(v){return T+(H-T-B)*(1-(v-lo)/(hi-lo));}
  var s="",g;
  for(g=0;g<=3;g++){var gv=lo+(hi-lo)*g/3,gy=Y(gv);
    s+='<line x1="'+L+'" y1="'+gy+'" x2="'+(W-R)+'" y2="'+gy+'" stroke="#eef1f4"/>';
    s+='<text x="'+(L-7)+'" y="'+(gy+4)+'" text-anchor="end" class="axis">'+
      fmtU(gv,spec.unit)+'</text>';}
  var iw=(W-L-R)/n;
  if(spec.type==="bar"){
    for(var i=0;i<n;i++){
      var x=L+i*iw+iw*0.15, bw=iw*0.7, y0=Y(Math.max(0,vals[i])), y1=Y(Math.min(0,vals[i]));
      s+='<rect x="'+x+'" y="'+y0+'" width="'+bw+'" height="'+Math.max(1,y1-y0)+
        '" rx="2" fill="#0969da"/>';
      s+='<text x="'+(x+bw/2)+'" y="'+(H-B+15)+'" text-anchor="middle" class="axis2">'+
        String(spec.labels[i]).slice(0,10)+'</text>';
    }
  } else {
    var pts=[],step=Math.max(1,Math.ceil(n/6));
    for(var j=0;j<n;j++){var px=L+iw*j+iw/2,py=Y(vals[j]);pts.push(px+","+py);
      s+='<circle cx="'+px+'" cy="'+py+'" r="3" fill="#0969da"/>';
      if(j===0||j===n-1||j%step===0)
        s+='<text x="'+px+'" y="'+(H-B+15)+'" text-anchor="middle" class="axis2">'+
          String(spec.labels[j]).slice(0,8)+'</text>';}
    s='<polyline fill="none" stroke="#0969da" stroke-width="2.5" points="'+
      pts.join(" ")+'"/>'+s;
  }
  if(bm!=null){var by=Y(bm);
    s+='<line x1="'+L+'" y1="'+by+'" x2="'+(W-R)+'" y2="'+by+
      '" stroke="#1a7f37" stroke-width="1.5" stroke-dasharray="6 4"/>';
    s+='<text x="'+(W-R)+'" y="'+(by-5)+'" text-anchor="end" class="axis" '+
      'fill="#1a7f37">'+(spec.benchmark_label||"target")+'</text>';}
  var cap=spec.caption?'<div class="chart-cap">'+spec.caption+
    (n<=1?' · trend builds as you update over time':'')+'</div>':'';
  return cap+'<div class="chart-wrap modal-chart"><svg viewBox="0 0 '+W+' '+H+
    '" class="chart">'+s+'</svg></div>';
}
// Click-in detail modal for the KPI tiles.
var modal=document.getElementById("kpi-modal");
function openModal(card){
  var key=card.getAttribute("data-key");
  var kpi=KPIMAP[key]||{};
  var more=card.querySelector(".kpi-more");
  document.getElementById("kpi-modal-title").textContent=
    card.getAttribute("data-title")||"";
  document.getElementById("kpi-modal-value").textContent=
    card.getAttribute("data-value")||"";
  document.getElementById("kpi-modal-body").innerHTML=
    kpiChart(kpi.chart)+(more?more.innerHTML:"");
  modal.hidden=false;
}
function closeModal(){modal.hidden=true;}
Array.prototype.forEach.call(
  document.querySelectorAll(".kpi.clickable"),
  function(c){
    c.addEventListener("click",function(){openModal(c);});
    c.addEventListener("keydown",function(e){
      if(e.key==="Enter"||e.key===" "){e.preventDefault();openModal(c);}
    });
  }
);
document.getElementById("kpi-modal-close").addEventListener("click",closeModal);
modal.addEventListener("click",function(e){if(e.target===modal){closeModal();}});
document.addEventListener("keydown",function(e){
  if(e.key==="Escape"){closeModal();}
});
"""


def _build_html(data: dict) -> str:
    payload = json.dumps(data, separators=(",", ":"))
    sections = [
        _render_header(data),
        '<div class="print-header">'
        f'{_esc(data["client"])} &middot; {_esc(data["generated_at"])}</div>',
        _render_summary_section(data),
        _render_section(
            "Cash & Liquidity", "liquidity", data["kpis"]["liquidity"],
            "How much cash you have and how long it lasts."),
        _render_forecast_section(),
        _render_section(
            "Receivables", "receivables", data["kpis"]["receivables"],
            "What customers owe and how quickly it turns into cash."),
        _render_section(
            "Payables & Disbursements", "disbursements",
            data["kpis"]["disbursements"],
            "What you owe and where the money goes out."),
        _render_section(
            "Revenue & Margin", "revenue", data["kpis"]["revenue"],
            "The top line, profitability, and what it takes to break even."),
        _render_flags(data["flags"]),
        _render_appendix(data),
    ]
    modal = (
        '<div id="kpi-modal" class="modal" hidden>'
        '<div class="modal-card" role="dialog" aria-modal="true">'
        '<button id="kpi-modal-close" class="modal-close" '
        'aria-label="Close">&times;</button>'
        '<div class="modal-head"><h3 id="kpi-modal-title"></h3>'
        '<div id="kpi-modal-value" class="modal-value"></div></div>'
        '<div id="kpi-modal-body" class="modal-body"></div></div></div>'
    )
    body = "".join(sections) + modal
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f'<title>{_esc(data["client"])} Dashboard</title>'
        f"<style>{_STYLE}</style></head><body data-scenario="
        f'"{_esc(data["scenario_default"])}">{body}'
        f'<script type="application/json" id="data">{payload}</script>'
        f"<script>{_SCRIPT}</script></body></html>"
    )


def _filename(client_name: str, end: str | None) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", client_name).strip("_") or "Client"
    stamp = (end or dt.date.today().isoformat())[:7]
    return f"{slug}_Dashboard_{stamp}.html"


def generate_dashboard(
    conn: sqlite3.Connection,
    client_name: str,
    out_dir: str | Path,
    scenario_default: str = "BASE",
) -> Path:
    """Build the offline dashboard and write it to out_dir; returns the path."""
    data = _gather(conn, client_name, scenario_default)
    html_text = _build_html(data)
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    target = out_path / _filename(client_name, data["period"]["end"])
    target.write_text(html_text, encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> None:
    """python -m dashboard.generate <slug> <out_dir> [--scenario X]"""
    import argparse

    from core import db

    parser = argparse.ArgumentParser(description="Generate a client dashboard.")
    parser.add_argument("client_slug")
    parser.add_argument("out_dir")
    parser.add_argument("--scenario", default="BASE", choices=SCENARIO_KEYS)
    parser.add_argument(
        "--name", default=None, help="display name (defaults to the slug)"
    )
    args = parser.parse_args(argv)

    conn = db.get_client_db(args.client_slug)
    try:
        path = generate_dashboard(
            conn, args.name or args.client_slug, args.out_dir, args.scenario
        )
    finally:
        conn.close()
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
