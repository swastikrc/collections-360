"""Phase 0: package skeleton, settings and the deny-list."""
import importlib
import os

import pytest

from c360.layer2 import config, deny_list

BLOCKED = [
    # protected attributes (exact names from golden.PROTECTED_COLUMNS)
    "salutation", "gender_code", "marital_status", "citizenship_status", "id_document_type",
    "household_size", "dependants_count", "newcomer_program_flag", "newcomer_arrival_date",
    "accessibility_needs_flag", "accessibility_type", "vulnerability_flag", "vulnerability_type",
    "vulnerability_source", "vulnerability_recorded_date", "vulnerability_review_date",
    # personal data
    "first_name", "middle_name", "last_name", "preferred_name", "full_name", "customer_name",
    "employer_name", "cardholder_name", "agent_name", "subject_name_reported", "date_of_birth",
    "dob_raw", "dob", "age", "age_band", "primary_phone_raw", "primary_phone_std", "primary_phone_type",
    "cardholder_phone_raw", "phone", "email", "email_std", "email_raw", "address_line1", "address_line2",
    "postal_code", "postal_code_raw", "postal_code_std", "national_id_hash", "national_id",
    "FIRST_NAME", "Email", "Date_Of_Birth",
    # proxies from the data contract
    "city", "fsa", "geo_lat", "geo_lon",
]

ALLOWED = [
    "email_template_id", "address_mismatch_flag", "model_name", "table_name", "column_name",
    "consent_email", "program_name", "metric_name", "province_code", "c360_id", "case_id",
    "national_dncl_telemarketing_only", "outcome_code", "segment", "hardship_reason", "agent_id",
    "letter_template_id", "sms_template_id", "tenure_months", "housing_status", "language",
    "preferred_language", "preferred_channel", "average_dpd", "note_text", "transcript_text",
    "dq_bucket_mismatch", "dpd_bucket_recomputed", "is_duplicate", "page_count", "stage",
]


@pytest.mark.parametrize("name", BLOCKED)
def test_deny_list_blocks(name):
    assert deny_list.is_denied(name), name
    assert deny_list.reason(name)


@pytest.mark.parametrize("name", ALLOWED)
def test_deny_list_allows(name):
    assert not deny_list.is_denied(name), f"{name}: {deny_list.reason(name)}"


def test_deny_list_covers_every_protected_column():
    from c360.layer1.golden import PROTECTED_COLUMNS
    assert deny_list.find_denied(PROTECTED_COLUMNS) == PROTECTED_COLUMNS


def test_layer1_still_imports():
    assert "publish_l2" in importlib.import_module("run_layer1").STEPS


def test_settings_defaults_and_overrides(monkeypatch, tmp_path):
    s = config.settings()
    assert s.max_tool_calls == 5 and s.max_rows == 50 and s.sql_timeout == 30 and s.temperature == 0
    assert s.snapshot_date == "2026-09-28"
    assert s.l2_dir == s.warehouse / "l2"
    assert s.source_db == s.warehouse / "c360.duckdb"
    monkeypatch.setenv("C360_WAREHOUSE", str(tmp_path))
    monkeypatch.setenv("C360_L2_MAX_TOOL_CALLS", "3")
    monkeypatch.setenv("C360_L2_MODEL", "some-model")
    s = config.settings()
    assert s.warehouse == tmp_path and s.l2_dir == tmp_path / "l2" and s.max_tool_calls == 3
    assert s.model == "some-model"
    monkeypatch.setenv("C360_L2_DIR", str(tmp_path / "elsewhere"))
    assert config.settings().l2_dir == tmp_path / "elsewhere"


def test_model_has_no_default(monkeypatch):
    monkeypatch.delenv("C360_L2_MODEL", raising=False)
    assert config.settings().model is None


def test_mongo_mode_validation(monkeypatch):
    with pytest.raises(ValueError):
        config.mongo_db("bogus")
    with pytest.raises(FileNotFoundError):
        config.mongo_db("mock:/nonexistent/customers.jsonl")


def test_warehouse_env_override_reaches_layer1_config():
    from c360 import config as base
    assert str(base.WAREHOUSE) == os.environ["C360_WAREHOUSE"]


# DB machine: real columns found by the doctor that must be blocked, and harmless real columns that must not be.
@pytest.mark.parametrize("name", [
    "agent_display_name", "name_suffix", "counterparty_name_clean", "vulnerable_customer_flag",
    "vulnerability_disclosed_flag", "customer_accent_region", "agent_accent_region", "sms_text", "sms_reply_text",
    "email_subject", "fsa_income_index", "phone_number_hash", "phone_type_dialled",
])
def test_real_personal_columns_are_denied(name):
    assert deny_list.is_denied(name), name


@pytest.mark.parametrize("name", [
    "vulnerable_customer_certified", "certified_for_vulnerable_flag", "agent_id", "agent_site", "program_name",
    "metric_name", "sms_template_id", "sms_delivered_flag", "email_opened_flag", "description_en", "province_code",
    "language_of_service", "team_id", "test_cell",
])
def test_real_harmless_columns_are_allowed(name):
    assert not deny_list.is_denied(name), name
