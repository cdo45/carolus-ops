"""Chart-of-Accounts standardization proposal.

generate_proposal(conn) -> ProposalResult inspects the client's accounts and
proposes a numbered, standardized chart following a fixed range map. It never
silently renumbers a 'confirmed' account, suggests merges only for near-
duplicate active accounts in the same category, and flags dormant/inactive
accounts for deactivation.

write_proposal_files(conn, out_dir) emits two artifacts:
  * <Client>_COA_Proposal_<YYYY-MM>.html — an offline human-review document
    (same offline rules as the dashboard: everything inline, zero external
    requests);
  * <Client>_QBO_Import.csv — exactly four columns (Account Number, Account
    Name, Type, Detail Type) holding only the rows the proposal numbers.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from core.kpi.base import period_bounds

# category -> (low, high) inclusive. Ordered by number for the chart preview.
RANGES: dict[str, tuple[int, int]] = {
    "CASH": (1000, 1099),
    "AR": (1200, 1279),
    "OCA": (1320, 1499),
    "FA": (1500, 1789),
    "AD": (1790, 1799),
    "AP": (2000, 2049),
    "CC": (2100, 2199),
    "TAXL": (2200, 2299),
    "OCL": (2320, 2399),
    "LTD": (2500, 2899),
    "EQ": (3000, 3199),
    "EQ-DRAW": (3200, 3299),
    "REV": (4000, 4199),
    "SUB": (5200, 5299),
    "DMAT": (5300, 5399),
    "DL": (5400, 5499),
    "OH-PAY": (7000, 7099),
    "OH-INS": (7100, 7199),
    "OH-OCC": (7200, 7299),
    "OH": (7300, 7899),
    "TAXE": (7900, 7949),
    "INT": (7950, 7979),
    "DEP": (7980, 7999),
    "OI": (8000, 8099),
}

# Dormant Tier-2 categories: reserved address space, no accounts created.
RESERVED_SLOTS = [
    ("RET-AR", "Retention receivable", "1280-1299"),
    ("UB", "Underbillings", "1300-1319"),
    ("OB", "Overbillings", "2300-2319"),
]

MERGE_THRESHOLD = 0.8
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


@dataclass
class ProposalRow:
    id: int
    qbo_name: str
    full_path: str | None
    current_number: str | None
    qbo_type: str | None
    detail_type: str | None
    category: str | None
    confidence: int | None
    status: str
    dormant: bool
    inactive: bool
    proposed_number: int | None
    proposed_name: str
    action: str  # keep | renumber | rename | merge | deactivate
    merge_into: str | None = None
    flagged: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class ProposalResult:
    client_name: str
    period_month: str
    rows: list[ProposalRow]
    reserved: list[dict]
    counts: dict[str, int]
    unmapped: list[str]


# ── helpers ──────────────────────────────────────────────────────────────────

def _scrub(text: str) -> str:
    return _URL_RE.sub("[link removed]", text)


def _esc(value) -> str:
    return html.escape(_scrub(str(value)), quote=True)


def _client_display(conn: sqlite3.Connection) -> str:
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main" and file:
            stem = Path(file).stem
            return re.sub(r"[-_]+", " ", stem).strip().title() or "Client"
    return "Client"


def _proposed_name(qbo_name: str, full_path: str | None) -> str:
    leaf = (full_path or qbo_name).split(":")[-1].strip()
    leaf = re.sub(r"\s*\(deleted\)\s*$", "", leaf, flags=re.IGNORECASE)
    return leaf.title() if leaf else qbo_name


def _parse_number(raw) -> int | None:
    if raw is None:
        return None
    m = re.match(r"\s*(\d{3,6})", str(raw))
    return int(m.group(1)) if m else None


def _tokens(name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", name.lower()))


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _related(a, b) -> bool:
    """True when one account is an ancestor of the other (so a high token
    overlap is hierarchy, not duplication)."""
    pa = a["full_path"] or a["qbo_name"]
    pb = b["full_path"] or b["qbo_name"]
    return pa.startswith(pb + ":") or pb.startswith(pa + ":")


def _is_deactivate(row) -> bool:
    if row["inactive_candidate"]:
        return True
    balance = row["coa_balance"]
    zero = balance is None or abs(balance) < 0.005
    return bool(row["dormant"]) and zero


def _next_free(used: set[int], low: int, high: int,
               prefer_base: int | None) -> int | None:
    if prefer_base is not None:
        for n in range(prefer_base + 1, prefer_base + 10):
            if low <= n <= high and n not in used:
                return n
    for step in (10, 5, 1):
        start = low if low % step == 0 else low + (step - low % step)
        for n in range(start, high + 1, step):
            if n not in used:
                return n
    return None


# ── core ─────────────────────────────────────────────────────────────────────

def generate_proposal(conn: sqlite3.Connection) -> ProposalResult:
    rows = conn.execute(
        """
        SELECT id, qbo_name, full_path, account_number, qbo_type, detail_type,
               category, confidence, status, dormant, inactive_candidate,
               coa_balance
        FROM accounts
        ORDER BY COALESCE(full_path, qbo_name)
        """
    ).fetchall()

    by_path = {(r["full_path"] or r["qbo_name"]): r for r in rows}

    deactivated: set[int] = {r["id"] for r in rows if _is_deactivate(r)}

    by_category: dict[str, list] = {}
    for r in rows:
        if r["category"]:
            by_category.setdefault(r["category"], []).append(r)

    # Merge suggestions: near-duplicate active accounts in one category.
    merged_into: dict[int, str] = {}
    for accounts in by_category.values():
        active = [a for a in accounts if a["id"] not in deactivated]
        kept: list = []
        for a in sorted(active, key=lambda x: x["qbo_name"]):
            match = next(
                (k for k in kept
                 if not _related(a, k)
                 and _overlap(a["qbo_name"], k["qbo_name"]) >= MERGE_THRESHOLD),
                None,
            )
            if match is not None:
                merged_into[a["id"]] = match["qbo_name"]
            else:
                kept.append(a)

    # Number the survivors, per category, in range order.
    numbers: dict[int, int] = {}
    flagged_ids: set[int] = set()
    notes_by_id: dict[int, list[str]] = {}

    for category in RANGES:
        accounts = [
            a for a in by_category.get(category, [])
            if a["id"] not in deactivated and a["id"] not in merged_into
        ]
        if not accounts:
            continue
        low, high = RANGES[category]
        used: set[int] = set()
        ordered = sorted(accounts, key=lambda a: (a["full_path"] or a["qbo_name"]))

        for a in ordered:  # pass A: keep numbers that already fit
            num = _parse_number(a["account_number"])
            if num is None or num in used:
                continue
            if low <= num <= high:
                numbers[a["id"]] = num
                used.add(num)
            elif a["status"] == "confirmed":
                numbers[a["id"]] = num
                used.add(num)
                flagged_ids.add(a["id"])
                notes_by_id.setdefault(a["id"], []).append(
                    "Confirmed account keeps its number though it sits outside "
                    f"the standard {category} range."
                )

        for a in ordered:  # pass B: assign the rest, children near parent
            if a["id"] in numbers:
                continue
            prefer = None
            path = a["full_path"] or a["qbo_name"]
            if ":" in path:
                parent = by_path.get(path.rsplit(":", 1)[0])
                if parent is not None and parent["id"] in numbers:
                    prefer = numbers[parent["id"]]
            n = _next_free(used, low, high, prefer)
            if n is None:
                notes_by_id.setdefault(a["id"], []).append(
                    f"No free number left in the {category} range."
                )
                continue
            numbers[a["id"]] = n
            used.add(n)
            if a["status"] == "confirmed" and _parse_number(
                a["account_number"]
            ) not in (None, n):
                flagged_ids.add(a["id"])
                notes_by_id.setdefault(a["id"], []).append(
                    "Confirmed account would be renumbered — review first."
                )

    # Build rows.
    result_rows: list[ProposalRow] = []
    unmapped: list[str] = []
    counts = {k: 0 for k in ("keep", "renumber", "rename", "merge", "deactivate")}
    for r in rows:
        if r["category"] is None and r["id"] not in deactivated:
            unmapped.append(r["qbo_name"])
        proposed_name = _proposed_name(r["qbo_name"], r["full_path"])
        proposed_number = numbers.get(r["id"])
        current_number = r["account_number"]

        if r["id"] in deactivated:
            action = "deactivate"
        elif r["id"] in merged_into:
            action = "merge"
        elif proposed_number is not None and _parse_number(current_number) != \
                proposed_number:
            action = "renumber"
        elif proposed_name != r["qbo_name"]:
            action = "rename"
        else:
            action = "keep"
        counts[action] += 1

        result_rows.append(ProposalRow(
            id=r["id"], qbo_name=r["qbo_name"], full_path=r["full_path"],
            current_number=current_number, qbo_type=r["qbo_type"],
            detail_type=r["detail_type"], category=r["category"],
            confidence=r["confidence"], status=r["status"],
            dormant=bool(r["dormant"]), inactive=bool(r["inactive_candidate"]),
            proposed_number=proposed_number, proposed_name=proposed_name,
            action=action, merge_into=merged_into.get(r["id"]),
            flagged=r["id"] in flagged_ids,
            notes=notes_by_id.get(r["id"], []),
        ))

    _, end = period_bounds(conn)
    month = (end or dt.date.today().isoformat())[:7]
    reserved = [
        {"category": code, "label": label, "range": rng}
        for code, label, rng in RESERVED_SLOTS
    ]
    return ProposalResult(
        client_name=_client_display(conn),
        period_month=month,
        rows=result_rows,
        reserved=reserved,
        counts=counts,
        unmapped=unmapped,
    )


# ── file writers ─────────────────────────────────────────────────────────────

_ACTION_LABELS = {
    "keep": "Keep as-is",
    "renumber": "Renumber",
    "rename": "Rename",
    "merge": "Merge (duplicate)",
    "deactivate": "Deactivate",
}


def _proposal_html(result: ProposalResult) -> str:
    def fmt_num(n):
        return str(n) if n is not None else "—"

    summary = "".join(
        f'<div class="card"><div class="n">{result.counts[a]}</div>'
        f'<div class="l">{_esc(_ACTION_LABELS[a])}</div></div>'
        for a in ("keep", "renumber", "rename", "merge", "deactivate")
    )

    def action_table(action):
        rows = [r for r in result.rows if r.action == action]
        if not rows:
            return ""
        body = ""
        for r in rows:
            flag = " &#9873;" if r.flagged else ""
            extra = (f" → merge into {_esc(r.merge_into)}"
                     if r.action == "merge" and r.merge_into else "")
            note = (f'<div class="note">{_esc("; ".join(r.notes))}</div>'
                    if r.notes else "")
            body += (
                "<tr><td>" + _esc(r.qbo_name) + flag + "</td>"
                "<td>" + _esc(r.current_number or "—") + "</td>"
                "<td>" + _esc(fmt_num(r.proposed_number)) + "</td>"
                "<td>" + _esc(r.proposed_name) + extra + note + "</td>"
                "<td>" + _esc(r.category or "—") + "</td></tr>"
            )
        return (
            f"<h3>{_esc(_ACTION_LABELS[action])} "
            f'<span class="count">({len(rows)})</span></h3>'
            "<table><thead><tr><th>Current name</th><th>Current #</th>"
            "<th>Proposed #</th><th>Proposed name</th><th>Category</th>"
            f"</tr></thead><tbody>{body}</tbody></table>"
        )

    numbered = sorted(
        (r for r in result.rows if r.proposed_number is not None),
        key=lambda r: r.proposed_number,
    )
    preview = "".join(
        "<tr><td class='num'>" + str(r.proposed_number) + "</td><td>" +
        _esc(r.proposed_name) + "</td><td>" + _esc(r.qbo_type or "") +
        "</td><td>" + _esc(r.detail_type or "") + "</td></tr>"
        for r in numbered
    )
    reserved = "".join(
        f"<li>{_esc(s['range'])} — {_esc(s['label'])} "
        f"({_esc(s['category'])}): reserved, not created</li>"
        for s in result.reserved
    )
    unmapped = (
        "<p class='note'>Unmapped accounts (no category yet): "
        + _esc(", ".join(result.unmapped)) + "</p>"
        if result.unmapped else ""
    )

    style = (
        "body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;"
        "color:#1c2128;margin:0;padding:0 24px 40px;}h1{font-size:20px;}"
        "h3{font-size:14px;margin-top:22px;}.cards{display:flex;gap:12px;"
        "flex-wrap:wrap;margin:16px 0;}.card{border:1px solid #d0d7de;"
        "border-radius:8px;padding:12px 18px;text-align:center;}"
        ".card .n{font-size:24px;font-weight:600;}.card .l{font-size:11px;"
        "color:#57606a;text-transform:uppercase;}table{border-collapse:"
        "collapse;width:100%;font-size:12px;margin-top:6px;}th,td{border:"
        "1px solid #d0d7de;padding:4px 8px;text-align:left;}td.num{"
        "text-align:right;font-variant-numeric:tabular-nums;}.count{color:"
        "#57606a;font-weight:400;}.note{color:#9a6700;font-size:11px;}"
        "ul{font-size:12px;color:#57606a;}"
    )
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>{_esc(result.client_name)} COA Proposal</title>"
        f"<style>{style}</style></head><body>"
        f"<h1>{_esc(result.client_name)} — Chart of Accounts Proposal</h1>"
        f"<p>Standardization preview for {_esc(result.period_month)}.</p>"
        f'<div class="cards">{summary}</div>'
        + action_table("merge") + action_table("renumber")
        + action_table("rename") + action_table("deactivate")
        + action_table("keep")
        + "<h3>Reserved address space</h3><ul>" + reserved + "</ul>"
        + unmapped
        + "<h3>Numbered chart preview</h3>"
        "<table><thead><tr><th>#</th><th>Account name</th><th>Type</th>"
        f"<th>Detail Type</th></tr></thead><tbody>{preview}</tbody></table>"
        "</body></html>"
    )


def _import_csv(result: ProposalResult) -> str:
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Account Number", "Account Name", "Type", "Detail Type"])
    numbered = sorted(
        (r for r in result.rows if r.proposed_number is not None),
        key=lambda r: r.proposed_number,
    )
    for r in numbered:
        writer.writerow([
            r.proposed_number, r.proposed_name, r.qbo_type or "",
            r.detail_type or "",
        ])
    return buffer.getvalue()


def write_proposal_files(conn: sqlite3.Connection, out_dir: str | Path) -> dict:
    result = generate_proposal(conn)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", result.client_name).strip("_") or "Client"
    html_path = out / f"{slug}_COA_Proposal_{result.period_month}.html"
    csv_path = out / f"{slug}_QBO_Import.csv"
    html_path.write_text(_proposal_html(result), encoding="utf-8")
    csv_path.write_text(_import_csv(result), encoding="utf-8")
    return {"html": html_path, "csv": csv_path, "result": result}
