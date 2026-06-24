"""Tests for the classification engine (core/classify.py)."""

import json
from pathlib import Path

import pytest

from core import db
from core.classify import classify_account, classify_client, tier_for

DATA = Path(__file__).parent.parent / "data"


@pytest.fixture(scope="module")
def aliases():
    with open(DATA / "factory_aliases.json", encoding="utf-8") as f:
        return [
            (a["pattern"].lower(), a["category"], a["confidence"], a["source"])
            for a in json.load(f)
        ]


def test_factory_alias_hit_and_prefix_rule(aliases):
    c = classify_account("Retained Earnings", "Equity", None, aliases)
    assert (c.category, c.confidence, c.source) == ("EQ", 100, "alias-factory")
    assert c.tier == "auto"
    # Parenthetical suffix matches via the "sales" alias.
    c = classify_account("Sales (deleted)", "Income", None, aliases)
    assert (c.category, c.source) == ("REV", "alias-factory")
    # "Sales of Product Income" matches its own factory alias, not "sales".
    c = classify_account("Sales of Product Income", "Income", None, aliases)
    assert (c.category, c.confidence, c.source) == ("REV", 100, "alias-factory")
    # No alias match → falls all the way to the expense default.
    c = classify_account("Salesperson Expense", "Expenses", None, aliases)
    assert (c.category, c.confidence, c.source) == ("OH", 75, "default")


def test_keyword_beats_detail_type(aliases):
    c = classify_account(
        "Equipment Depreciation - Field", "Expenses", "auto", aliases
    )
    assert (c.category, c.confidence, c.source) == ("DEP", 95, "keyword")


def test_detail_type_stage_beats_cogs_default(aliases):
    c = classify_account(
        "Tile Labor", "Cost of Goods Sold", "Cost of labor - COS", aliases
    )
    assert (c.category, c.confidence, c.source) == ("DL", 85, "detail-type")
    assert c.tier == "confirm"


def test_qbo_type_stage(aliases):
    c = classify_account("Random Bucket", "Bank", None, aliases)
    assert (c.category, c.confidence, c.source) == ("CASH", 95, "qbo-type")
    assert c.tier == "auto"


def test_expense_default(aliases):
    c = classify_account(
        "Weird Expense Nobody Understands", "Expenses", "Something Unknown",
        aliases,
    )
    assert (c.category, c.confidence, c.source) == ("OH", 75, "default")
    assert c.tier == "confirm"


def test_unmapped_goes_to_queue(aliases):
    for qbo_type in (None, "", "Mystery Type"):
        c = classify_account("Inexplicable", qbo_type, None, aliases)
        assert c.category is None
        assert c.confidence == 0
        assert (c.source, c.tier) == ("unmapped", "queue")


def test_loan_rule_not_bank_condition(aliases):
    # Bank type: the loan keyword must NOT fire; the type stage wins.
    c = classify_account("Loan to Shareholder", "Bank", None, aliases)
    assert (c.category, c.confidence, c.source) == ("CASH", 95, "qbo-type")
    # Long Term Liabilities: the keyword fires.
    c = classify_account(
        "Loan to Shareholder", "Long Term Liabilities", None, aliases
    )
    assert (c.category, c.confidence, c.source) == ("LTD", 75, "keyword")


def test_taxl_taxe_split_by_type_class(aliases):
    c = classify_account(
        "Sales Tax Payable", "Other Current Liabilities", None, aliases
    )
    assert (c.category, c.confidence, c.source) == ("TAXL", 90, "keyword")
    c = classify_account("Payroll Tax Expense", "Expenses", None, aliases)
    assert (c.category, c.confidence, c.source) == ("TAXE", 90, "keyword")


def test_eq_draw_keyword_beats_equity_type(aliases):
    c = classify_account(
        "Owner Distributions - Pedro", "Equity", None, aliases
    )
    assert (c.category, c.confidence, c.source) == ("EQ-DRAW", 90, "keyword")


def test_tier_routing():
    assert tier_for("CASH", 95) == "auto"
    assert tier_for("CASH", 90) == "auto"
    assert tier_for("OH", 89) == "confirm"
    assert tier_for("OH", 70) == "confirm"
    assert tier_for("DMAT", 60) == "queue"
    assert tier_for(None, 0) == "queue"
    assert tier_for(None, None) == "queue"


def test_classify_client_end_to_end(tmp_path):
    conn = db.get_client_db("classify_test", base_dir=tmp_path)
    global_conn = db.get_global_db(base_dir=tmp_path)
    accounts = [
        # (qbo_name, qbo_type, detail_type, category, status)
        ("Retained Earnings", "Equity", None, None, "proposed"),
        ("Checking", "Bank", "Checking", None, "proposed"),
        ("Tile Labor", "Cost of Goods Sold", "Cost of labor - COS", None,
         "proposed"),
        ("Weird Expense Nobody Understands", "Expenses", "Something Unknown",
         None, "proposed"),
        ("Inexplicable", None, None, None, "proposed"),
        # Confirmed, with a deliberately wrong category that must survive.
        ("Design Income", "Income", "Service/Fee Income", "CC", "confirmed"),
    ]
    for name, qbo_type, detail, category, status in accounts:
        conn.execute(
            "INSERT INTO accounts (qbo_name, qbo_type, detail_type, category, "
            "status) VALUES (?, ?, ?, ?, ?)",
            (name, qbo_type, detail, category, status),
        )
    conn.commit()

    report = classify_client(conn, global_conn)
    assert report.total == 6
    assert report.skipped_confirmed == 1
    assert report.by_tier == {"auto": 2, "confirm": 2, "queue": 1}
    assert report.by_source == {
        "alias-factory": 1, "qbo-type": 1, "detail-type": 1, "default": 1,
        "unmapped": 1,
    }
    assert report.queue == [("Inexplicable", None, None)]

    # Results written back.
    rows = {
        r["qbo_name"]: r
        for r in conn.execute(
            "SELECT qbo_name, category, confidence, status FROM accounts"
        )
    }
    assert rows["Retained Earnings"]["category"] == "EQ"
    assert rows["Checking"]["category"] == "CASH"
    assert rows["Tile Labor"]["category"] == "DL"
    assert rows["Weird Expense Nobody Understands"]["category"] == "OH"
    assert rows["Inexplicable"]["category"] is None
    # Status never changed by the classifier.
    assert all(
        r["status"] == ("confirmed" if name == "Design Income" else "proposed")
        for name, r in rows.items()
    )
    # The confirmed account's wrong category survived, untouched and unlogged.
    assert rows["Design Income"]["category"] == "CC"
    confirmed_id = conn.execute(
        "SELECT id FROM accounts WHERE qbo_name = 'Design Income'"
    ).fetchone()["id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE entity_id = ?", (confirmed_id,)
    ).fetchone()[0] == 0

    # One audit row per changed account (all 5 non-confirmed changed:
    # 4 gained categories, the unmapped one went NULL-confidence → 0).
    audit_count = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE source = 'classifier'"
    ).fetchone()[0]
    assert audit_count == 5

    # Re-run: nothing changes → ZERO new audit rows.
    report2 = classify_client(conn, global_conn)
    assert report2.by_tier == report.by_tier
    audit_count2 = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE source = 'classifier'"
    ).fetchone()[0]
    assert audit_count2 == audit_count

    conn.close()
    global_conn.close()


def test_audit_rows_carry_old_and_new_values(tmp_path):
    conn = db.get_client_db("audit_test", base_dir=tmp_path)
    global_conn = db.get_global_db(base_dir=tmp_path)
    conn.execute(
        "INSERT INTO accounts (qbo_name, qbo_type, status) "
        "VALUES ('Checking', 'Bank', 'proposed')"
    )
    conn.commit()
    classify_client(conn, global_conn)
    row = conn.execute(
        "SELECT old_value, new_value FROM audit_log WHERE source = 'classifier'"
    ).fetchone()
    assert json.loads(row["old_value"]) == {"category": None, "confidence": None}
    assert json.loads(row["new_value"]) == {"category": "CASH", "confidence": 95}
    conn.close()
    global_conn.close()
