"""Audit the unmodified v1.1 oracle, including previously unscored requirements."""

import sqlite3
from pathlib import Path

import pytest

from jev_browser.saas_benchmark import grade_result
from jev_browser.saas_verifier import verifier_source

UPSTREAM = Path(__file__).parent / "fixtures/saas_bench/business_031_v11_verify.py"


@pytest.fixture
def oracle(monkeypatch):
    for site in ("HRMS", "BIGCAPITAL", "TWENTY"):
        for suffix in ("PORT", "CONTAINER", "DB_CONTAINER"):
            monkeypatch.setenv(f"{site}_{suffix}", "test")
    original, source, patch = verifier_source(
        {"task_id": "business_031", "verify_py_path": str(UPSTREAM)})
    assert original == source == UPSTREAM.read_text() and patch is None
    namespace = {"__name__": "test_oracle"}
    exec(compile(source, "verifier.v11.py", "exec"), namespace)
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE CONTACTS (
            ID INTEGER, COMPANY_NAME TEXT, FIRST_NAME TEXT, LAST_NAME TEXT,
            DISPLAY_NAME TEXT, EMAIL TEXT, CONTACT_SERVICE TEXT
        );
        INSERT INTO CONTACTS VALUES
            (1, 'Ananya Reddy - Ex Employee', 'Ananya', 'Reddy',
             'Ananya Reddy', 'ananya.reddy@gmail.com', 'vendor');
        CREATE TABLE MANUAL_JOURNALS (
            ID INTEGER, DATE TEXT, DESCRIPTION TEXT, PUBLISHED_AT TEXT
        );
        INSERT INTO MANUAL_JOURNALS VALUES
            (1, '2026-06-30', 'Final settlement — Ananya Reddy — 2026-06-30', '2026-06-30');
        CREATE TABLE ACCOUNTS (ID INTEGER, NAME TEXT);
        INSERT INTO ACCOUNTS VALUES
            (1, 'Rent'), (2, 'Advertising Expense'), (3, 'Opening Balance Liabilities'),
            (4, 'Sales of Product Income');
        CREATE TABLE MANUAL_JOURNALS_ENTRIES (
            MANUAL_JOURNAL_ID INTEGER, ACCOUNT_ID INTEGER, CREDIT REAL, DEBIT REAL
        );
        INSERT INTO MANUAL_JOURNALS_ENTRIES VALUES
            (1, 1, 0, 57950), (1, 2, 0, 29000), (1, 3, 86950, 0);
        CREATE TABLE ACCOUNTS_TRANSACTIONS (
            ACCOUNT_ID INTEGER, REFERENCE_TYPE TEXT, REFERENCE_ID INTEGER, DEBIT REAL, DATE TEXT
        );
        INSERT INTO ACCOUNTS_TRANSACTIONS VALUES (1, 'Journal', 1, 57950, '2026-06-30');
        CREATE TABLE BILLS_PAYMENTS (
            VENDOR_ID INTEGER, AMOUNT REAL, PAYMENT_DATE TEXT, PAYMENT_ACCOUNT_ID INTEGER,
            REFERENCE TEXT, STATEMENT TEXT
        );
        INSERT INTO BILLS_PAYMENTS VALUES
            (1, 86950, '2026-07-05', 4, 'Final settlement — Ananya Reddy — 2026-06-30', '');
    """)

    def sql(query):
        rows = db.execute(query).fetchall()
        return "\n".join("\t".join("NULL" if v is None else str(v) for v in row) for row in rows)

    namespace["bigcapital_sql"] = sql
    try:
        yield namespace, db
    finally:
        db.close()


def checks(oracle):
    namespace, _ = oracle
    vendor_id = namespace["check_5_bigcapital_vendor"]()
    namespace["check_6_journal_entry"]()
    namespace["check_7_payment_made"](vendor_id)
    return namespace["_checks"]


def test_complete_financial_state_passes_unmodified_v11(oracle):
    assert [(w, p) for _, w, p, _ in checks(oracle)] == [(1, True), (3, True), (2, True)]


@pytest.mark.parametrize("mutation,failed", [
    ("UPDATE CONTACTS SET COMPANY_NAME = 'wrong'", {"5", "7"}),
    ("UPDATE CONTACTS SET DISPLAY_NAME = 'wrong'", {"5", "7"}),
    ("UPDATE CONTACTS SET CONTACT_SERVICE = 'customer'", {"5", "7"}),
    ("UPDATE MANUAL_JOURNALS SET PUBLISHED_AT = NULL", {"6"}),
    ("DELETE FROM ACCOUNTS_TRANSACTIONS", {"6"}),
    ("UPDATE ACCOUNTS_TRANSACTIONS SET REFERENCE_ID = 2", {"6"}),
    ("INSERT INTO MANUAL_JOURNALS_ENTRIES VALUES (1, 1, 0, 1)", {"6"}),
    ("UPDATE BILLS_PAYMENTS SET VENDOR_ID = 2", {"7"}),
    ("UPDATE BILLS_PAYMENTS SET REFERENCE = 'wrong'", {"7"}),
    ("UPDATE BILLS_PAYMENTS SET PAYMENT_DATE = '2026-07-06'", {"7"}),
])
def test_incomplete_or_wrongly_linked_state_cannot_earn_credit(oracle, mutation, failed):
    oracle[1].execute(mutation)
    result = checks(oracle)
    assert {label.split(".")[0] for label, _, passed, _ in result if not passed} == failed
    assert all("exception:" not in detail for _, _, _, detail in result)


def test_runtime_schema_error_is_not_a_valid_agent_failure(oracle):
    oracle[1].execute("DROP TABLE ACCOUNTS_TRANSACTIONS")
    result = checks(oracle)
    grade = grade_result({
        "status": "FAIL", "returncode": 1, "total": 6,
        "checks": [{"label": label, "weight": w, "passed": p, "detail": detail}
                   for label, w, p, detail in result],
    })
    assert grade["data_valid"] is False and grade["strict_success"] is None


@pytest.mark.parametrize("extra", [
    {"has_errors": True},
    {"checks": [{"status": "ERROR", "weight": 1, "passed": False, "detail": "DB unavailable"}]},
])
def test_explicit_v11_error_flags_are_ungraded(extra):
    grade = grade_result({
        "status": "FAIL", "returncode": 1, "total": 1,
        "checks": [{"status": "FAIL", "weight": 1, "passed": False}], **extra,
    })
    assert grade["data_valid"] is False and grade["strict_success"] is None
