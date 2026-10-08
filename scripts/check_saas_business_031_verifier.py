"""Validate an audited official/legacy oracle on disposable DB fixtures; never an agent score.

Run on macmini with the SaaS Docker context. This intentionally inserts test
records directly into a separate slot, then removes all owned containers.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import runpy
import uuid
from pathlib import Path

from jev_browser.observability import write_json
from jev_browser.protocol import digest
from jev_browser.saas_benchmark import configuration, grade_result, upstream
from jev_browser.saas_verifier import verifier_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--saas-root", type=Path, default=Path.home() / "SaaS-Bench-v1.1")
    parser.add_argument("--saas-slot", type=int, default=98)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.saas_slot <= 99:
        parser.error("slot must be between 0 and 99")
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error("output must be empty")
    output.mkdir(parents=True, exist_ok=True)
    root = args.saas_root.resolve()
    loader, slots, verifier = upstream(root)
    task = next(t for t in loader.load_tasks(str(root / "tasks")) if t["task_id"] == "business_031")
    original, effective, patch = verifier_source(task)
    v11 = patch is None
    source_path = output / ("verifier.official.py" if v11 else "verifier.compat.py")
    source_path.write_text(effective)
    task = {**task, "verify_py_path": str(source_path)}
    sites = task["meta"]["meta_data"]["sites"]
    slot = slots.SlotManager(configuration(root), args.saas_slot)
    ports = slot.get_port_map(sites)
    results = {"scope": "Disposable database fixtures; no agent or model performance",
               "verifier_patch": patch, "upstream_verifier_hash": digest(original),
               "verifier_hash": digest(effective), "cases": []}

    def check_case(name, earned):
        result = verifier.run_verify(task, args.saas_slot, ports, "localhost", str(output), f"_{name}")
        grade = grade_result(result)
        results["cases"].append({"name": name, "expected_earned": earned, "grade": grade})
        write_json(output / "validation.json", results)
        assert grade["data_valid"] is True, grade
        assert result["total"] == 15 and len(result["checks"]) == 8, result
        assert result["earned"] == earned, result
        assert grade["strict_success"] is (earned == 15), grade
        print(f"{name}: valid, {earned}/15", flush=True)

    lock = (root / f".jev-{slots._SLOT_PREFIX}-{args.saas_slot}.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        lock.close()
        raise
    try:
        slot.start_apps(sites, hostname="localhost")
        check_case("initial", 0)
        os.environ.update(verifier.build_verify_env(task, args.saas_slot, ports, "localhost"))
        oracle = runpy.run_path(str(source_path))
        hrms, bc, twenty = (oracle[f"{name}_sql"] for name in ("hrms", "bigcapital", "twenty"))
        hrms("INSERT INTO `tabEmployee Separation` "
             "(name, employee, employee_name, boarding_begins_on, docstatus) VALUES "
             "('JEV-VERIFY-031', 'HR-EMP-00007', 'Ananya Reddy', '2026-06-30', 1);")
        for index, (activity, user) in enumerate([
            ("Return company laptop", "rajesh.kumar@example.test"),
            ("Revoke system access", "rajesh.kumar@example.test"),
            ("Conduct exit interview", "pooja.malhotra@example.test"),
        ]):
            hrms("INSERT INTO `tabEmployee Boarding Activity` "
                 "(name, parent, parenttype, parentfield, activity_name, user) VALUES "
                 f"('JEV-VERIFY-031-{index}', 'JEV-VERIFY-031', 'Employee Separation', "
                 f"'activities', '{activity}', '{user}');")
        vendor_id = int(bc("SELECT COALESCE(MAX(ID), 0) + 1 FROM CONTACTS;"))
        bc("INSERT INTO CONTACTS "
           "(ID, CONTACT_SERVICE, DISPLAY_NAME, COMPANY_NAME, FIRST_NAME, LAST_NAME, EMAIL) "
           f"VALUES ({vendor_id}, 'vendor', 'Ananya Reddy', 'Ananya Reddy - Ex Employee', "
           "'Ananya', 'Reddy', 'ananya.reddy@gmail.com');")
        journal_id = int(bc("SELECT COALESCE(MAX(ID), 0) + 1 FROM MANUAL_JOURNALS;"))
        memo = "Final settlement — Ananya Reddy — 2026-06-30"
        bc("INSERT INTO MANUAL_JOURNALS (ID, DATE, DESCRIPTION, PUBLISHED_AT) "
           f"VALUES ({journal_id}, '2026-06-30', '{memo}', '2026-06-30');")
        rent_id = None
        for name, debit, credit in [
            ("Rent", 57950, 0), ("Advertising Expense", 29000, 0),
            ("Opening Balance Liabilities", 0, 86950),
        ]:
            account = int(bc(f"SELECT ID FROM ACCOUNTS WHERE NAME = '{name}' LIMIT 1;"))
            if name == "Rent":
                rent_id = account
            bc("INSERT INTO MANUAL_JOURNALS_ENTRIES (MANUAL_JOURNAL_ID, ACCOUNT_ID, DEBIT, CREDIT) "
               f"VALUES ({journal_id}, {account}, {debit}, {credit});")
            if v11:
                bc("INSERT INTO ACCOUNTS_TRANSACTIONS "
                   "(REFERENCE_TYPE, REFERENCE_ID, ACCOUNT_ID, DEBIT, CREDIT, DATE) "
                   f"VALUES ('Journal', {journal_id}, {account}, {debit}, {credit}, '2026-06-30');")
        payment_account = int(bc("SELECT ID FROM ACCOUNTS WHERE NAME = 'Sales of Product Income' LIMIT 1;"))
        bc("INSERT INTO BILLS_PAYMENTS (VENDOR_ID, AMOUNT, PAYMENT_DATE, PAYMENT_ACCOUNT_ID, REFERENCE) "
           f"VALUES ({vendor_id}, 86950, '2026-07-05', {payment_account}, '{memo}');")
        ws = oracle["get_twenty_workspace_schema"]()
        company = twenty(f'SELECT id FROM "{ws}".company WHERE name = \'MetricStream\' LIMIT 1;')
        if v11 and not company:
            raise RuntimeError("v1.1 fixture is missing MetricStream")
        body = ("Reassigned from Ananya Reddy (separated 2026-06-30). "
                "Original responsibility transferred — review and update client contacts.")
        for title in ["Schedule MetricStream compliance review meeting",
                      "Update MetricStream primary contact details",
                      "Follow up on MetricStream contract renewal"]:
            task_id = str(uuid.uuid4())
            twenty(f'INSERT INTO "{ws}".task (id, title, "dueAt", "bodyV2Markdown") '
                   f"VALUES ('{task_id}', '{title}', '2026-07-20', '{body}');")
            if v11:
                twenty(f'INSERT INTO "{ws}"."taskTarget" ("taskId", "targetCompanyId") '
                       f"VALUES ('{task_id}', '{company}');")
        note = ("Separation date: 2026-06-30. Final settlement: 86,950.00 "
                "(salary: 57,950.00, leave encashment: 29,000.00). "
                "Payment processed 2026-07-05 from Sales of Product Income. "
                "3 client tasks reassigned to company MetricStream.")
        twenty(f'INSERT INTO "{ws}".note (id, title, "bodyV2Markdown") '
               f"VALUES ('{uuid.uuid4()}', 'Employee Separation Complete — Ananya Reddy', '{note}');")
        check_case("complete", 15)
        bc(f"UPDATE MANUAL_JOURNALS SET PUBLISHED_AT = NULL WHERE ID = {journal_id};")
        check_case("draft", 12)
        bc(f"UPDATE MANUAL_JOURNALS SET PUBLISHED_AT = '2026-06-30' WHERE ID = {journal_id};")
        bc(f"UPDATE MANUAL_JOURNALS_ENTRIES SET DEBIT = 57949 "
           f"WHERE MANUAL_JOURNAL_ID = {journal_id} AND ACCOUNT_ID = {rent_id};")
        check_case("wrong_amount", 12)
        bc(f"UPDATE MANUAL_JOURNALS_ENTRIES SET DEBIT = 57950 "
           f"WHERE MANUAL_JOURNAL_ID = {journal_id} AND ACCOUNT_ID = {rent_id};")
        bc(f"UPDATE CONTACTS SET EMAIL = 'wrong@example.test' WHERE ID = {vendor_id};")
        check_case("wrong_email", 12 if v11 else 14)
        if v11:
            bc(f"UPDATE CONTACTS SET EMAIL = 'ananya.reddy@gmail.com' WHERE ID = {vendor_id};")
            bc(f"UPDATE BILLS_PAYMENTS SET REFERENCE = 'wrong' WHERE VENDOR_ID = {vendor_id};")
            check_case("wrong_reference", 13)
            bc(f"UPDATE BILLS_PAYMENTS SET REFERENCE = '{memo}' WHERE VENDOR_ID = {vendor_id};")
            bc(f"DELETE FROM ACCOUNTS_TRANSACTIONS WHERE REFERENCE_TYPE = 'Journal' "
               f"AND REFERENCE_ID = {journal_id};")
            check_case("missing_gl_posting", 12)
            # Restore all rows before the next independent mutation.
            bc("INSERT INTO ACCOUNTS_TRANSACTIONS "
               "(REFERENCE_TYPE, REFERENCE_ID, ACCOUNT_ID, DEBIT, CREDIT, DATE) "
               f"SELECT 'Journal', {journal_id}, ACCOUNT_ID, DEBIT, CREDIT, '2026-06-30' "
               f"FROM MANUAL_JOURNALS_ENTRIES WHERE MANUAL_JOURNAL_ID = {journal_id};")
            twenty(f'UPDATE "{ws}"."taskTarget" SET "targetCompanyId" = NULL '
                   f"WHERE \"taskId\" = '{task_id}';")
            check_case("unlinked_task", 13)
            twenty(f'UPDATE "{ws}"."taskTarget" SET "targetCompanyId" = \'{company}\' '
                   f"WHERE \"taskId\" = '{task_id}';")
            hrms("INSERT INTO `tabEmployee Boarding Activity` "
                 "(name, parent, parenttype, parentfield, activity_name, user) VALUES "
                 "('JEV-VERIFY-031-extra', 'JEV-VERIFY-031', 'Employee Separation', "
                 "'activities', 'Extra activity', 'pooja.malhotra@example.test');")
            check_case("extra_exit_activity", 13)
    finally:
        try:
            slot.stop_apps(sites)
        finally:
            lock.close()


if __name__ == "__main__":
    main()
