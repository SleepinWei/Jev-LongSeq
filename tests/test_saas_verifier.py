"""Exercise the actual pinned verifier against the image's BigCapital schema."""
import sqlite3
from pathlib import Path

import pytest

from jev_browser.saas_benchmark import grade_result
from jev_browser.saas_verifier import BUSINESS_031_PATCH, verifier_source

UPSTREAM = Path(__file__).parent / "fixtures/saas_bench/business_031_verify.py"


@pytest.fixture
def oracle(monkeypatch):
    for site in ("HRMS", "BIGCAPITAL", "TWENTY"):
        for suffix in ("PORT", "CONTAINER", "DB_CONTAINER"):
            monkeypatch.setenv(f"{site}_{suffix}", "test")
    task = {"task_id": "business_031", "verify_py_path": str(UPSTREAM)}
    _, source, patch = verifier_source(task)
    assert patch == BUSINESS_031_PATCH
    namespace = {"__name__": "test_oracle"}
    exec(compile(source, "verifier.compat.py", "exec"), namespace)
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE CONTACTS (
            ID INTEGER, DISPLAY_NAME TEXT, EMAIL TEXT, CONTACT_SERVICE TEXT, FIRST_NAME TEXT
        );
        INSERT INTO CONTACTS VALUES
            (1, 'Ananya Reddy', 'ananya.reddy@gmail.com', 'vendor', 'Ananya');
        CREATE TABLE MANUAL_JOURNALS (
            ID INTEGER, DATE TEXT, DESCRIPTION TEXT, PUBLISHED_AT TEXT
        );
        INSERT INTO MANUAL_JOURNALS VALUES
            (1, '2026-06-30', 'Final settlement — Ananya Reddy — 2026-06-30', '2026-06-30');
        CREATE TABLE ACCOUNTS (ID INTEGER, NAME TEXT);
        INSERT INTO ACCOUNTS VALUES
            (1, 'Rent'), (2, 'Advertising Expense'), (3, 'Opening Balance Liabilities');
        CREATE TABLE MANUAL_JOURNALS_ENTRIES (
            MANUAL_JOURNAL_ID INTEGER, ACCOUNT_ID INTEGER, CREDIT REAL, DEBIT REAL
        );
        INSERT INTO MANUAL_JOURNALS_ENTRIES VALUES
            (1, 1, 0, 57950), (1, 2, 0, 29000), (1, 3, 86950, 0);
    """)

    def sql(query):
        rows = db.execute(query).fetchall()
        return "\n".join("\t".join(str(value) for value in row) for row in rows)

    namespace["bigcapital_sql"] = sql
    try:
        yield namespace, db
    finally:
        db.close()


def checks(oracle):
    namespace, _ = oracle
    namespace["check_5_bigcapital_vendor"]()
    namespace["check_6_journal_entry"]()
    return namespace["_checks"]


def test_requested_display_name_and_published_journal_pass(oracle):
    result = checks(oracle)
    assert [(weight, passed) for _, weight, passed, _ in result] == [(1, True), (3, True)]


@pytest.mark.parametrize("mutation,failed_label", [
    ("DELETE FROM CONTACTS", "5."),
    ("UPDATE CONTACTS SET EMAIL = 'wrong@example.test'", "5."),
    ("DELETE FROM MANUAL_JOURNALS", "6."),
    ("UPDATE MANUAL_JOURNALS SET PUBLISHED_AT = NULL", "6."),
    ("UPDATE MANUAL_JOURNALS_ENTRIES SET DEBIT = 57949 WHERE ACCOUNT_ID = 1", "6."),
])
def test_missing_records_drafts_wrong_email_and_wrong_amount_fail(oracle, mutation, failed_label):
    oracle[1].execute(mutation)
    result = checks(oracle)
    failed = [label for label, _, passed, _ in result if not passed]
    assert len(failed) == 1 and failed[0].startswith(failed_label)
    assert all("exception:" not in detail for _, _, _, detail in result)


def test_unexpected_schema_error_remains_ungraded(oracle):
    oracle[1].execute("DROP TABLE MANUAL_JOURNALS")
    result = checks(oracle)
    verification = {"status": "FAIL", "returncode": 1, "total": 4,
                    "checks": [{"label": label, "weight": weight, "passed": passed,
                                "detail": detail} for label, weight, passed, detail in result]}
    grade = grade_result(verification)
    assert grade["data_valid"] is False and grade["strict_success"] is None


def test_changed_upstream_is_not_silently_patched(tmp_path):
    source = tmp_path / "verify.py"
    source.write_text(UPSTREAM.read_text() + "\n# changed oracle\n")
    with pytest.raises(ValueError, match="version changed"):
        verifier_source({"task_id": "business_031", "verify_py_path": str(source)})


def test_other_tasks_use_the_unmodified_official_source(tmp_path):
    source = tmp_path / "verify.py"
    source.write_text("# other task\n")
    original, effective, patch = verifier_source(
        {"task_id": "business_023", "verify_py_path": str(source)})
    assert original == effective == source.read_text() and patch is None
