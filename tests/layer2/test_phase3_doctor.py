"""Phase 3: the doctor says exactly what differs between a c360.duckdb and what Layer 2 expects."""
import copy
import json
import shutil

import duckdb
import pytest

from c360.layer2 import config, doctor, publish


@pytest.fixture()
def db_copy(world, tmp_path):
    path = tmp_path / "c360.duckdb"
    shutil.copy(world["db"], path)
    return path


def alter(path, *statements):
    con = duckdb.connect(str(path))
    for sql in statements:
        con.execute(sql)
    con.close()


def find(items, subject_part, message_part=""):
    return [i for i in items if subject_part in i.subject and message_part in i.message]


def test_synthetic_database_is_clean(world, mock_db):
    report = doctor.check()
    assert report.errors == [], [(i.subject, i.message) for i in report.errors]
    assert report.warnings == [], [(i.subject, i.message) for i in report.warnings]
    checks = {i.check for i in report.items}
    assert {"source", "schema", "views", "reference", "transcripts", "mongo", "text_search"} <= checks
    assert set(report.reference) == set(doctor.REFERENCE_TABLES)
    assert len(report.reference["hardship_programs"]["rows"]) == 3
    assert find(report.infos, "l2.call_transcripts", "top-level JSON keys of the first readable file: language, transcript_id, turns")
    assert find(report.infos, "customers", "reachable, 2,000 documents")


def test_main_exits_zero_and_writes_both_reports(world, mock_db, capsys):
    with pytest.raises(SystemExit) as e:
        doctor.main()
    assert e.value.code == 0
    out_dir = config.settings().out_dir
    md = (out_dir / "doctor_report.md").read_text()
    assert "## Errors (must fix)" in md and "## Warnings (explain or fix)" in md and "## Notes" in md
    data = json.loads((out_dir / "doctor_report.json").read_text())
    assert data["errors"] == 0 and data["items"]
    assert "0 error(s)" in capsys.readouterr().out


def test_dropping_a_required_column_is_an_error_naming_table_and_column(db_copy, mock_db):
    alter(db_copy, "ALTER TABLE curated.promises_to_pay DROP COLUMN ptp_status")
    report = doctor.check(source_db=db_copy)
    assert find(report.errors, "curated.promises_to_pay.ptp_status", "confirmed column missing")
    assert find(report.errors, "l2.promises_to_pay.ptp_status", "does not bind")
    with pytest.raises(SystemExit) as e:
        report2 = doctor.check(source_db=db_copy)
        doctor.write_report(report2)
        raise SystemExit(1 if report2.errors else 0)
    assert e.value.code == 1


def test_publish_refuses_when_the_doctor_finds_an_error(db_copy, tmp_path):
    alter(db_copy, "ALTER TABLE curated.collections_cases DROP COLUMN queue")
    with pytest.raises(publish.PublishError, match="doctor found"):
        publish.publish_l2(quiet=True, l2_dir=tmp_path / "l2", source_db=db_copy)
    assert not list((tmp_path / "l2").glob("l2_*"))


def test_dropping_a_required_table_is_an_error(db_copy, mock_db):
    alter(db_copy, "DROP TABLE curated.offers")
    report = doctor.check(source_db=db_copy)
    assert find(report.errors, "curated.offers", "table missing")
    assert find(report.errors, "l2.offers", "source does not bind")


def test_missing_join_key_is_an_error(db_copy, mock_db):
    alter(db_copy, "ALTER TABLE golden.case_xref RENAME COLUMN c360_id TO person_id")
    report = doctor.check(source_db=db_copy)
    assert find(report.errors, "golden.case_xref.c360_id", "column missing")
    assert find(report.errors, "l2.collections_cases.c360_id")


def test_deny_listed_column_in_the_allow_list_is_an_error(world, mock_db):
    views = copy.deepcopy(publish.load_views())
    views["customers"]["columns"]["date_of_birth"] = "any_value(c.date_of_birth)"
    report = doctor.check(views=views)
    assert find(report.errors, "l2.customers.date_of_birth", "allow-listed column is deny-listed")


def test_deny_listed_source_column_under_another_name_is_an_error(world, mock_db):
    views = copy.deepcopy(publish.load_views())
    views["customers"]["columns"]["contact_number"] = "arg_max(c.primary_phone_std, c.last_profile_update_ts)"
    views["card_accounts"]["columns"]["holder"] = {"expr": "a.cardholder_phone_raw", "optional": True}
    views["customer_summary"]["columns"]["mail"] = {"path": "contact.email", "type": "VARCHAR"}
    report = doctor.check(views=views)
    assert find(report.errors, "l2.customers.contact_number", "reads deny-listed column curated.customers.primary_phone_std")
    assert find(report.errors, "l2.card_accounts.holder", "cardholder_phone_raw")
    assert find(report.errors, "l2.customer_summary.mail", "deny-listed field `contact.email`")


def test_renaming_an_optional_column_is_a_warning(db_copy, mock_db):
    alter(db_copy, "ALTER TABLE curated.hardship_programs RENAME COLUMN program_name TO name",
          "ALTER TABLE curated.account_monthly_snapshot RENAME COLUMN product_type TO product")
    report = doctor.check(source_db=db_copy)
    assert report.errors == [], [(i.subject, i.message) for i in report.errors]
    assert find(report.warnings, "l2.hardship_programs.program_name", "optional column skipped")
    assert find(report.warnings, "l2.account_monthly_snapshot.product_type", "optional column skipped")
    assert find(report.warnings, "curated.hardship_programs.program_name", "confirmed column missing")
    # the renamed columns are listed for a human to decide on, never auto-added
    assert find(report.infos, "curated.hardship_programs", "columns not mentioned anywhere")
    assert find(report.infos, "curated.account_monthly_snapshot", "product")


def test_missing_optional_table_is_a_warning(db_copy, mock_db):
    alter(db_copy, "DROP TABLE curated.case_assignment_history")
    report = doctor.check(source_db=db_copy)
    assert report.errors == []
    assert find(report.warnings, "l2.case_assignment_history", "it will be skipped")


def test_unexpected_columns_are_listed_and_personal_ones_flagged(db_copy, mock_db):
    alter(db_copy, "ALTER TABLE curated.agents ADD COLUMN agent_name VARCHAR", "ALTER TABLE curated.agents ADD COLUMN zz_unlisted_attr VARCHAR",
          "CREATE TABLE curated.surprise AS SELECT 1 AS a")
    report = doctor.check(source_db=db_copy)
    assert report.errors == []
    hit = find(report.infos, "curated.agents", "columns not mentioned anywhere")
    assert hit and "agent_name (deny-listed)" in hit[0].message and "zz_unlisted_attr" in hit[0].message
    assert find(report.infos, "curated.surprise", "table not in expected_schema.yaml")


def test_type_mismatch_is_a_warning(db_copy, mock_db):
    alter(db_copy, "ALTER TABLE curated.promises_to_pay ALTER ptp_amount TYPE VARCHAR")
    report = doctor.check(source_db=db_copy)
    assert find(report.warnings, "curated.promises_to_pay.ptp_amount", "type is VARCHAR, expected float")


def test_missing_source_database(tmp_path):
    report = doctor.check(source_db=tmp_path / "nope.duckdb")
    assert len(report.errors) == 1 and "not found" in report.errors[0].message


def test_unreachable_mongo_is_an_error(world):
    class Broken:
        @property
        def customers(self):
            raise ConnectionError("connection refused")
    report = doctor.check(mongo=Broken())
    assert find(report.errors, "customers", "MongoDB not reachable")


def test_missing_transcript_files_are_reported(world, mock_db, tmp_path):
    report = doctor.check(data_root=tmp_path)
    assert find(report.warnings, "l2.call_transcripts", "0 of 20 sampled files exist")
