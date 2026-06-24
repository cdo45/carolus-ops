"""Tests for the import pipeline (core/importer.py)."""

import csv
import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from core import db
from core.importer import dormancy_pass, import_coa, import_gl
from core.parsers.coa import parse_coa
from core.parsers.gl import parse_gl

# ── numbered-account helpers ────────────────────────────────────────────────

FIXTURES = Path(__file__).parent / "fixtures"
COA_FULL = FIXTURES / "coa_full.csv"


def fmt(value: float) -> str:
    return f"{value:,.2f}"


def write_gl(path, sections, period="June, 2025-May, 2026", company="Test Co"):
    """Write a synthetic GL file with consistent running balances.

    sections: list of dicts with keys name, txns (list of (date, type, num,
    who, desc, split, amount)), optional beginning (adds a labeled Beginning
    Balance row), optional with_total (default True; False makes a parent-
    header-style section).
    """
    rows = [
        [company],
        ["General Ledger"],
        [period],
        [],
        ["", "Transaction date", "Transaction type", "Num", "Name",
         "Description", "Split", "Amount", "Balance"],
    ]
    for section in sections:
        rows.append([section["name"]])
        beginning = section.get("beginning")
        balance = beginning or 0.0
        if beginning is not None:
            rows.append(["", "Beginning Balance", "", "", "", "", "", "",
                         fmt(beginning)])
        for date, ttype, num, who, desc, split, amount in section["txns"]:
            balance = round(balance + amount, 2)
            rows.append(["", date, ttype, num, who, desc, split,
                         fmt(amount), fmt(balance)])
        if section.get("with_total", True):
            net = round(sum(t[6] for t in section["txns"]), 2)
            rows.append([f"Total for {section['name']}", "", "", "", "", "",
                         "", f"${fmt(net)}", ""])
    rows.append([])
    rows.append(["Accrual Basis  Friday, June 12, 2026 03:30 AM GMTZ"])
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)
    return path


def write_coa(path, accounts, company="Test Co"):
    """accounts: list of (number, full_path, qbo_type, detail_type, balance)."""
    rows = [
        [company],
        ["Chart of Accounts"],
        ["As of May 31, 2026"],
        [],
        ["Account #", "Full name", "Type", "Detail type", "Balance"],
    ]
    for number, full_path, qbo_type, detail, balance in accounts:
        rows.append([number or "", full_path, qbo_type, detail,
                     fmt(balance) if balance is not None else ""])
    rows.append([])
    rows.append(["Accrual Basis  Friday, June 12, 2026 03:31 AM GMTZ"])
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)
    return path


BASE_SECTIONS = [
    {
        "name": "Checking",
        "beginning": 5000.00,
        "txns": [
            ("06/15/2025", "Deposit", "", "Customer A", "Payment", "AR", 1200.00),
            ("07/02/2025", "Expense", "", "Office Depot", "Supplies", "Office", -350.25),
            ("12/10/2025", "Deposit", "", "Customer B", "Payment", "AR", 900.00),
        ],
    },
    {
        "name": "Design Income",
        "txns": [
            ("06/18/2025", "Invoice", "1001", "Customer A", "Phase 1", "AR", -3000.00),
            ("12/15/2025", "Invoice", "1042", "Customer C", "Retainer", "AR", -2750.00),
        ],
    },
]


@pytest.fixture
def conn(tmp_path):
    connection = db.get_client_db("testco", base_dir=tmp_path / "appdata")
    yield connection
    connection.close()


def txn_count(conn):
    return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


def upload_count(conn):
    return conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]


def test_identical_double_import(conn, tmp_path):
    gl_path = write_gl(tmp_path / "gl.csv", BASE_SECTIONS)
    first = import_gl(conn, parse_gl(gl_path), "gl.csv")
    assert first.inserted_count == 5
    second = import_gl(conn, parse_gl(gl_path), "gl.csv")
    assert txn_count(conn) == 5
    assert (second.diff.changed_count, second.diff.added_count,
            second.diff.removed_count) == (0, 0, 0)
    assert upload_count(conn) == 2


def test_edited_copy_diff_and_supersede(conn, tmp_path):
    import_gl(conn, parse_gl(write_gl(tmp_path / "gl1.csv", BASE_SECTIONS)),
              "gl1.csv")

    edited = [dict(s, txns=list(s["txns"])) for s in BASE_SECTIONS]
    date, ttype, num, who, desc, split, _ = edited[0]["txns"][1]
    edited[0]["txns"][1] = (date, ttype, num, who, desc, split, -400.00)
    report = import_gl(conn, parse_gl(write_gl(tmp_path / "gl2.csv", edited)),
                       "gl2.csv")

    assert report.diff.changed_count == 1
    assert report.diff.added_count == 0
    assert report.diff.removed_count == 0
    sample = report.diff.changed_samples[0]
    assert sample["account"] == "Checking"
    assert sample["old_amount"] == pytest.approx(-350.25)
    assert sample["new_amount"] == pytest.approx(-400.00)

    blob = conn.execute(
        "SELECT superseded_data FROM uploads WHERE id = 1"
    ).fetchone()[0]
    old_rows = json.loads(gzip.decompress(blob))
    assert len(old_rows) == 5
    assert any(r["amount"] == pytest.approx(-350.25) for r in old_rows)


def test_partial_range_reimport(conn, tmp_path):
    import_gl(conn, parse_gl(write_gl(tmp_path / "gl_full.csv", BASE_SECTIONS)),
              "gl_full.csv")
    assert txn_count(conn) == 5  # 3 in Jun-Jul, 2 in December

    partial_sections = [
        {
            "name": "Checking",
            "beginning": 5000.00,
            "txns": [
                ("06/15/2025", "Deposit", "", "Customer A", "Payment", "AR", 1200.00),
                ("07/02/2025", "Expense", "", "Office Depot", "Supplies", "Office", -355.00),
            ],
        },
        {
            "name": "Design Income",
            "txns": [
                ("06/18/2025", "Invoice", "1001", "Customer A", "Phase 1", "AR", -3000.00),
            ],
        },
    ]
    report = import_gl(
        conn,
        parse_gl(write_gl(tmp_path / "gl_partial.csv", partial_sections,
                          period="June, 2025-July, 2025")),
        "gl_partial.csv",
    )

    # June-July replaced (3 old → 3 new), December untouched.
    assert report.deleted_count == 3
    assert report.inserted_count == 3
    assert txn_count(conn) == 5
    december = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE txn_date > '2025-07-31'"
    ).fetchone()[0]
    assert december == 2
    assert any(
        "Previous data through 2025-12-15 retained" in n
        and "2025-06-01-2025-07-31" in n
        for n in report.notes
    )
    assert report.diff.changed_count == 1  # the -350.25 → -355.00 edit


def test_coa_reimport_preserves_user_decisions(conn):
    coa = parse_coa(COA_FULL)
    import_coa(conn, coa)
    conn.execute(
        "UPDATE accounts SET category = 'CASH', status = 'confirmed', "
        "confidence = 100, proposed_number = '10000' WHERE qbo_name = 'Checking'"
    )
    conn.commit()

    coa2 = parse_coa(COA_FULL)
    checking = next(a for a in coa2.accounts if a.qbo_name == "Checking")
    checking.balance = 9999.00
    report = import_coa(conn, coa2)
    assert report.updated_count == 8
    assert not report.new_accounts

    row = conn.execute(
        "SELECT category, status, confidence, proposed_number, coa_balance "
        "FROM accounts WHERE qbo_name = 'Checking'"
    ).fetchone()
    assert row["category"] == "CASH"
    assert row["status"] == "confirmed"
    assert row["confidence"] == 100
    assert row["proposed_number"] == "10000"
    assert row["coa_balance"] == pytest.approx(9999.00)  # facts still update


def test_deleted_marker_sets_inactive_candidate(conn):
    import_coa(conn, parse_coa(COA_FULL))
    row = conn.execute(
        "SELECT inactive_candidate FROM accounts WHERE qbo_name = 'Old Loan (deleted)'"
    ).fetchone()
    assert row["inactive_candidate"] == 1


def test_leaf_name_matching(conn, tmp_path):
    import_coa(conn, parse_coa(COA_FULL))
    gl_path = write_gl(tmp_path / "gl.csv", [
        {
            "name": "2019 Range Rover",
            "txns": [
                ("08/01/2025", "Bill", "", "Land Rover Dealer", "Purchase",
                 "Loan", 85000.00),
            ],
        },
    ])
    report = import_gl(conn, parse_gl(gl_path), "gl.csv")
    assert report.matched_count == 1
    assert not report.stubbed_accounts
    account_id = conn.execute(
        "SELECT id FROM accounts WHERE qbo_name = 'FIXED ASSETS:Vehicles:2019 Range Rover'"
    ).fetchone()["id"]
    attached = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE account_id = ?", (account_id,)
    ).fetchone()[0]
    assert attached == 1


def test_ambiguous_leaf_stubs_with_warning(conn, tmp_path):
    write_coa(tmp_path / "coa.csv", [
        ("", "OVERHEAD:Insurance", "Expenses", "Insurance", 0.00),
        ("", "JOB COSTS:Insurance", "Cost of Goods Sold", "Insurance", 0.00),
        ("", "OVERHEAD", "Expenses", "Other", 0.00),
        ("", "JOB COSTS", "Cost of Goods Sold", "Other", 0.00),
    ])
    import_coa(conn, parse_coa(tmp_path / "coa.csv"))
    before = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]

    gl_path = write_gl(tmp_path / "gl.csv", [
        {
            "name": "Insurance",
            "txns": [
                ("07/01/2025", "Bill", "", "Premier Insurance", "Premium",
                 "AP", 1199.75),
            ],
        },
    ])
    report = import_gl(conn, parse_gl(gl_path), "gl.csv")
    assert report.stubbed_accounts == ["Insurance"]
    assert any(
        "matches multiple accounts" in w
        and "JOB COSTS:Insurance" in w
        and "OVERHEAD:Insurance" in w
        for w in report.warnings
    )
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == before + 1


def test_parent_header_skipped_not_stubbed(conn, tmp_path):
    import_coa(conn, parse_coa(COA_FULL))
    before = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    gl_path = write_gl(tmp_path / "gl.csv", [
        {"name": "FIXED ASSETS", "txns": [], "with_total": False},
        {"name": "Vehicles", "txns": [], "with_total": False},
        {
            "name": "2019 Range Rover",
            "txns": [
                ("08/01/2025", "Bill", "", "Dealer", "Purchase", "Loan", 85000.00),
            ],
        },
    ])
    report = import_gl(conn, parse_gl(gl_path), "gl.csv")
    assert report.skipped_parent_headers == ["FIXED ASSETS", "Vehicles"]
    assert not report.stubbed_accounts
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == before
    assert any("parent hierarchy header" in n for n in report.notes)


def test_dormancy_flag_wake_and_parent_exemption(conn, tmp_path):
    import_coa(conn, parse_coa(COA_FULL))
    import_gl(conn, parse_gl(write_gl(tmp_path / "gl1.csv", BASE_SECTIONS)),
              "gl1.csv")
    toggled = dormancy_pass(conn)
    assert toggled
    dormant = {
        r["qbo_name"]
        for r in conn.execute("SELECT qbo_name FROM accounts WHERE dormant = 1")
    }
    assert "Money Market (8556)" in dormant
    # Parents are structure, never flagged.
    assert "FIXED ASSETS" not in dormant
    assert "FIXED ASSETS:Vehicles" not in dormant
    # GL stubs (no qbo_type) aren't COA-sourced, so they're not flagged.
    assert "Checking" not in dormant

    # The account wakes up when transactions arrive.
    wake_sections = BASE_SECTIONS + [
        {
            "name": "Money Market (8556)",
            "beginning": 24797.35,
            "txns": [
                ("09/15/2025", "Transfer", "", "", "To operating", "Checking",
                 -5000.00),
            ],
        },
    ]
    import_gl(conn, parse_gl(write_gl(tmp_path / "gl2.csv", wake_sections)),
              "gl2.csv")
    toggled2 = dormancy_pass(conn)
    mm = conn.execute(
        "SELECT id, dormant FROM accounts WHERE qbo_name = 'Money Market (8556)'"
    ).fetchone()
    assert mm["dormant"] == 0
    assert mm["id"] in toggled2


def test_atomicity_rollback_on_midimport_failure(conn, tmp_path):
    gl_path = write_gl(tmp_path / "gl.csv", BASE_SECTIONS)
    import_gl(conn, parse_gl(gl_path), "gl.csv")
    before_txns = txn_count(conn)
    before_uploads = upload_count(conn)
    before_audit = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

    poisoned = parse_gl(gl_path)
    # NOT NULL violation at insert time — after the range DELETE has run,
    # so rollback must restore the deleted rows too.
    poisoned.accounts[1].transactions[0].txn_date = None
    with pytest.raises(sqlite3.IntegrityError):
        import_gl(conn, poisoned, "gl_poisoned.csv")

    assert txn_count(conn) == before_txns
    assert upload_count(conn) == before_uploads
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before_audit
    # Prior data intact, including the original amounts.
    amounts = [
        r["amount"]
        for r in conn.execute("SELECT amount FROM transactions").fetchall()
    ]
    assert any(a == pytest.approx(-350.25) for a in amounts)


def test_gl_balances_audited(conn, tmp_path):
    import_gl(conn, parse_gl(write_gl(tmp_path / "gl.csv", BASE_SECTIONS)),
              "gl.csv")
    rows = conn.execute(
        "SELECT new_value FROM audit_log WHERE field = 'gl_balances' "
        "AND source = 'import'"
    ).fetchall()
    assert len(rows) == 2
    payloads = [json.loads(r["new_value"]) for r in rows]
    by_source = {p["beginning_balance_source"]: p for p in payloads}
    assert by_source["labeled"]["beginning_balance"] == pytest.approx(5000.00)
    assert by_source["derived"]["beginning_balance"] == pytest.approx(0.0)
    assert by_source["derived"]["declared_net_activity"] == pytest.approx(-5750.00)


# ── numbered-account tests ───────────────────────────────────────────────────

def test_composite_match_numbered_account(tmp_path):
    """GL section '1000 Checking' matches via composite (account_number + qbo_name)."""
    conn = db.get_client_db("num_comp", base_dir=tmp_path / "appdata")
    write_coa(tmp_path / "coa.csv", [
        ("1000", "Checking", "Bank", "Checking", 5000.00),
    ])
    import_coa(conn, parse_coa(tmp_path / "coa.csv"))
    assert conn.execute(
        "SELECT account_number FROM accounts WHERE qbo_name = 'Checking'"
    ).fetchone()["account_number"] == "1000"

    gl_path = write_gl(tmp_path / "gl.csv", [
        {"name": "1000 Checking", "txns": [
            ("06/15/2025", "Deposit", "", "Customer A", "Payment", "AR", 1200.00),
        ]},
    ])
    report = import_gl(conn, parse_gl(gl_path), "gl.csv")
    assert report.matched_count == 1
    assert not report.stubbed_accounts
    account_id = conn.execute(
        "SELECT id FROM accounts WHERE qbo_name = 'Checking'"
    ).fetchone()["id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE account_id = ?", (account_id,)
    ).fetchone()[0] == 1
    conn.close()


def test_number_stripped_match_backfills_account_number(tmp_path):
    """GL '1000 Checking' strips to 'Checking', matches leaf, backfills number."""
    conn = db.get_client_db("num_strip", base_dir=tmp_path / "appdata")
    write_coa(tmp_path / "coa.csv", [
        ("", "Checking", "Bank", "Checking", 5000.00),
    ])
    import_coa(conn, parse_coa(tmp_path / "coa.csv"))
    assert conn.execute(
        "SELECT account_number FROM accounts WHERE qbo_name = 'Checking'"
    ).fetchone()["account_number"] is None

    gl_path = write_gl(tmp_path / "gl.csv", [
        {"name": "1000 Checking", "txns": [
            ("06/15/2025", "Deposit", "", "Customer A", "Payment", "AR", 1200.00),
        ]},
    ])
    report = import_gl(conn, parse_gl(gl_path), "gl.csv")
    assert report.matched_count == 1
    assert not report.stubbed_accounts
    row = conn.execute(
        "SELECT account_number FROM accounts WHERE qbo_name = 'Checking'"
    ).fetchone()
    assert row["account_number"] == "1000"
    conn.close()


def test_unnumbered_leaf_match_unchanged(tmp_path):
    """Non-numbered GL section names still resolve via leaf match."""
    conn = db.get_client_db("num_plain", base_dir=tmp_path / "appdata")
    write_coa(tmp_path / "coa.csv", [
        ("", "Checking", "Bank", "Checking", 5000.00),
    ])
    import_coa(conn, parse_coa(tmp_path / "coa.csv"))

    gl_path = write_gl(tmp_path / "gl.csv", [
        {"name": "Checking", "txns": [
            ("06/15/2025", "Deposit", "", "Customer A", "Payment", "AR", 1200.00),
        ]},
    ])
    report = import_gl(conn, parse_gl(gl_path), "gl.csv")
    assert report.matched_count == 1
    assert not report.stubbed_accounts
    conn.close()


def test_numbered_ghost_account_stubs(tmp_path):
    """Number-prefixed section that matches no account is still stubbed."""
    conn = db.get_client_db("num_ghost", base_dir=tmp_path / "appdata")
    write_coa(tmp_path / "coa.csv", [
        ("1000", "Checking", "Bank", "Checking", 5000.00),
    ])
    import_coa(conn, parse_coa(tmp_path / "coa.csv"))
    before = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]

    gl_path = write_gl(tmp_path / "gl.csv", [
        {"name": "9999 Ghost Account", "txns": [
            ("06/15/2025", "Deposit", "", "Someone", "Whatever", "AR", 100.00),
        ]},
    ])
    report = import_gl(conn, parse_gl(gl_path), "gl.csv")
    assert report.stubbed_accounts == ["9999 Ghost Account"]
    assert conn.execute(
        "SELECT COUNT(*) FROM accounts"
    ).fetchone()[0] == before + 1
    conn.close()


def test_account_number_migration(tmp_path):
    """An old-schema DB without account_number gains the column on reopen."""
    db_path = tmp_path / "clients" / "migrate_test.db"
    db_path.parent.mkdir(parents=True)
    raw = sqlite3.connect(db_path)
    raw.execute(
        "CREATE TABLE accounts (id INTEGER PRIMARY KEY, qbo_name TEXT NOT NULL, "
        "UNIQUE(qbo_name))"
    )
    raw.commit()
    raw.close()

    conn = db.get_client_db("migrate_test", base_dir=tmp_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    assert "account_number" in cols
    conn.close()
