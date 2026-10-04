"""Build the Layer 3 local store (`warehouse_l3/l3.duckdb`) from the Maple release.

    python -m c360.layer3.data.build_local            # ~5-10 min on a laptop, once

Only what Layer 3 needs, only for customers who have a collections case. Every CSV is read as
text and each column is cast with TRY_CAST, so a dirty value becomes NULL instead of failing the
load. Protected attributes (gender, marital status, citizenship, household, newcomer,
accessibility, vulnerability type, age, date of birth) and contact details are never copied. Of the
address, only the city, province and postal area (FSA, the first 3 characters of the postal code) are kept.
Model scores flagged `uses_protected_attributes` are dropped.

This is a stand-in for the resolved Collections 360 record: it links records inside the
collections domain by CRM id and case account ids, without Layer 1's identity resolution.
Set C360_L3_SOURCE=api to read the real C360 documents from the Layer 2 API instead.
"""
import sys
import time

import duckdb

from c360.layer3 import config

# column specs: v = text, b = boolean, d = date, t = timestamp, i = integer, n = double
CUSTOMERS = {
    "v": "crm_customer_id first_name last_name preferred_name city province_code time_zone preferred_language "
         "correspondence_language preferred_channel preferred_contact_window_start preferred_contact_window_end "
         "permitted_call_start_local permitted_call_end_local segment employment_status pay_frequency income_source "
         "housing_status internal_risk_grade customer_status duplicate_of_crm_id "
         # work and area, for the Groups view and "similar customers" (postal AREA only: the first 3 characters)
         "fsa occupation_category industry_naics2 employer_name",
    "b": "consent_call consent_autodialer consent_sms consent_email voicemail_permitted contact_at_work_permitted "
         "sunday_contact_permitted national_dncl_flag internal_do_not_call_flag cease_communication_flag "
         "third_party_authorised_flag credit_counselling_agency_flag power_of_attorney_flag legal_representative_flag "
         "deceased_flag bankruptcy_flag consumer_proposal_flag fraud_victim_flag military_or_reserve_flag "
         "digital_banking_enrolled mobile_app_user_flag income_verified_flag",
    "d": "customer_since_date insolvency_filed_date",
    "i": "tenure_months",
    "n": "declared_annual_income monthly_housing_cost",
}

CASES = {
    "v": "case_id coll_customer_ref case_open_reason case_status primary_account_id primary_product account_ids "
         "current_bucket prior_bucket queue assigned_team assigned_agent_id language_of_service strategy_code test_cell "
         "assignment_method current_treatment next_scheduled_action last_contact_channel current_ptp_id hardship_source "
         "hardship_reason hardship_plan_type dispute_type dispute_status complaint_stage legal_status province_code "
         "escalation_reason nba_recommendation_last outcome",
    "d": "case_open_date case_status_date bucket_entry_date current_ptp_due_date cure_date hardship_plan_start "
         "hardship_plan_end dispute_date complaint_date chargeoff_date outcome_date",
    "t": "next_action_ts last_contact_ts last_rpc_ts",
    "i": "entry_dpd current_dpd max_dpd_episode accounts_in_case_count contact_attempts_total rpc_count promises_total "
         "promises_kept promises_broken promises_open payments_since_open_count days_to_cure prior_cases_12m "
         "plan_instalments_total plan_instalments_paid contact_cap_7d contacts_last_7d contact_cap_breach_count "
         "out_of_hours_contact_count days_in_collections notes_count qa_flag_count",
    "n": "total_overdue total_exposure entry_overdue current_ptp_amount payments_since_open_amount "
         "plan_instalment_amount settlement_amount settlement_pct chargeoff_amount cost_to_collect_est expected_loss_est",
    "b": "cure_flag recidivism_flag hardship_flag hardship_program_offered hardship_program_accepted payment_plan_flag "
         "settlement_offered_flag dispute_flag complaint_flag cease_contact_flag third_party_rep_flag "
         "insolvency_hold_flag deceased_hold_flag vulnerable_customer_flag legal_referral_flag chargeoff_flag "
         "escalation_flag supervisor_review_flag nba_accepted_flag",
}

PROMISES = {
    "v": "ptp_id case_id crm_customer_id account_id product contact_id agent_id ptp_channel ptp_type "
         "payment_method_promised ptp_status broken_reason_agent_coded",
    "t": "ptp_created_ts status_ts",
    "d": "ptp_due_date paid_date customer_stated_income_date",
    "n": "ptp_amount overdue_at_creation ptp_to_overdue_ratio amount_paid_against",
    "i": "days_to_due_at_creation ptp_sequence_in_case prior_broken_ptp_count dpd_at_creation grace_days",
    "b": "reminder_scheduled_flag",
}

OFFERS = {
    "v": "offer_id case_id account_id contact_id agent_id program_id offered_by eligibility_result customer_response "
         "decline_reason plan_status",
    "t": "offer_ts response_ts",
    "d": "plan_start_date",
    "n": "proposed_payment_amount",
    "i": "duration_months plan_payments_due plan_payments_made",
    "b": "nba_recommended_flag",
}

CONTACTS = {
    "v": "contact_id case_id crm_customer_id account_id agent_id direction sub_channel day_of_week outcome_code "
         "outcome_desc transcript_id note_id system_event_type agent_site test_cell inbound_reason dial_result",
    "t": "contact_ts_local",
    "i": "contact_hour_local talk_duration_sec contacts_prior_7d",
    "b": "within_permitted_hours_flag stat_holiday_flag rpc_flag ptp_made_flag payment_taken_flag dispute_raised_flag "
         "complaint_raised_flag hardship_mentioned_flag vulnerability_disclosed_flag cease_request_flag "
         "callback_requested_flag",
    "n": "payment_amount contact_cost_cad overdue_at_contact",
}

NOTES = {
    "v": "note_id contact_id case_id crm_customer_id agent_id note_type note_source note_text note_language",
    "t": "note_ts",
}

TRANSCRIPTS = {
    "v": "transcript_id contact_id call_recording_id voice_sample_id case_id crm_customer_id agent_id direction "
         "language file_path",
    "t": "call_start_ts call_end_ts",
    "i": "duration_sec",
    "n": "asr_confidence_avg customer_talk_ratio agent_talk_ratio silence_ratio",
}

AGENTS = {
    "v": "agent_id site team_id team_lead_id role employment_type languages shift_pattern status",
    "i": "tenure_months skill_early_stage skill_mid_stage skill_late_stage skill_hardship skill_disputes "
         "max_concurrent_cases current_case_load complaints_90d",
    "b": "vulnerable_customer_certified coaching_flag",
    "n": "rpc_rate_90d ptp_rate_90d ptp_kept_rate_90d cure_rate_90d qa_score_avg_90d",
}

CARDS = {
    "v": "card_account_id src_customer_ref product_code card_tier account_status dpd_bucket hardship_program_type "
         "payment_history_24m",
    "d": "snapshot_date open_date payment_due_date last_payment_date chargeoff_date",
    "i": "months_on_book dpd worst_dpd_12m times_30dpd_12m autopay_failed_count_90d cash_advance_count_90d",
    "n": "credit_limit current_balance statement_balance utilisation minimum_payment_due past_due_amount "
         "last_payment_amount chargeoff_amount purchase_apr",
    "b": "autopay_enrolled hardship_program_flag dispute_open_flag chargeoff_flag overlimit_flag",
}

LOANS = {
    "v": "loan_id src_customer_ref product_type product_code payment_frequency dpd_bucket hardship_program_type "
         "collateral_type rate_type",
    "d": "snapshot_date origination_date maturity_date next_due_date last_payment_date chargeoff_date",
    "i": "original_term_months remaining_term_months missed_payments_count dpd worst_dpd_12m deferral_count_life",
    "n": "original_amount interest_rate scheduled_payment_amount next_due_amount principal_balance total_outstanding "
         "past_due_amount last_payment_amount chargeoff_amount",
    "b": "secured_flag revolving_flag pad_enrolled hardship_program_flag chargeoff_flag restructure_flag",
}

DEPOSITS = {
    "v": "deposit_account_id src_customer_ref product_type product_code account_status overdraft_dpd_bucket",
    "d": "snapshot_date last_payroll_date expected_next_payroll_date",
    "n": "current_balance overdraft_limit overdraft_used",
    "i": "nsf_count_mtd payroll_delay_days overdraft_dpd",
    "b": "payroll_deposit_flag ei_deposit_flag garnishment_flag",
}

SCORES = {
    "v": "score_id model_name model_version entity_type entity_id crm_customer_id case_id score_band slot_label "
         "top_reason_1_text top_reason_2_text top_reason_3_text recommended_action",
    "d": "score_date",
    "n": "score predicted_value answer_rate_estimate",
    "i": "risk_grade slot_rank",
    "b": "uses_protected_attributes",
}

SQL_TYPE = {"v": None, "b": "BOOLEAN", "d": "DATE", "t": "TIMESTAMP", "i": "BIGINT", "n": "DOUBLE"}
SMALL_TABLES = ["hardship_programs", "qa_checklist", "ops_events", "reference_documents", "code_lookups",
                "metric_definitions"]


def _csv(name):
    return str(config.maple_data() / "data" / f"{name}.csv")


def _read(name):
    return f"read_csv('{_csv(name)}', header = true, all_varchar = true, quote = '\"', escape = '\"')"


def _select(con, name, spec, rename=None, prefix=""):
    """SELECT list casting each wanted column; columns missing from the file are skipped."""
    have = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {_read(name)}").fetchall()}
    rename = rename or {}
    parts, missing = [], []
    for kind, names in spec.items():
        for col in names.split():
            if col not in have:
                missing.append(col)
                continue
            expr = f"{prefix}{col}" if SQL_TYPE[kind] is None else f"TRY_CAST({prefix}{col} AS {SQL_TYPE[kind]})"
            parts.append(f"{expr} AS {rename.get(col, col)}")
    if missing:
        print(f"    note: {name} has no column(s) {', '.join(missing)}; skipped")
    return ", ".join(parts)


def _step(label, con, sql):
    t = time.time()
    con.execute(sql)
    print(f"  {label:<28} ({time.time() - t:5.1f}s)")


def build(path=None):
    src = config.maple_data()
    if not (src / "data" / "collections_cases.csv").exists():
        sys.exit(f"Maple release not found at {src}. Set MAPLE_DATA to the folder that holds data/ and files/.")
    path = path or config.l3_db()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.duckdb")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    con.execute(f"SET temp_directory = '{path.parent / 'tmp'}'")
    con.execute("SET preserve_insertion_order = false")
    con.execute("SET memory_limit = '3GB'")
    t0 = time.time()
    print(f"[layer3] building {path} from {src}")

    _step("cases", con, f"CREATE TABLE cases AS SELECT {_select(con, 'collections_cases', CASES)} "
                        f"FROM {_read('collections_cases')} ORDER BY case_id")
    _step("case_accounts", con, """
        CREATE TABLE case_accounts AS
        SELECT DISTINCT case_id, coll_customer_ref, unnest(regexp_extract_all(coalesce(account_ids, ''), '[A-Z]{1,3}-[0-9]+')) AS account_id
        FROM cases   -- account_ids is a JSON array in most rows and a bare id in some
        UNION SELECT case_id, coll_customer_ref, primary_account_id FROM cases WHERE primary_account_id IS NOT NULL""")
    _step("customers", con, f"""
        CREATE TABLE customers AS SELECT {_select(con, 'customers', CUSTOMERS)} FROM {_read('customers')}
        WHERE crm_customer_id IN (SELECT DISTINCT coll_customer_ref FROM cases) ORDER BY crm_customer_id""")
    _step("promises", con, f"CREATE TABLE promises AS SELECT {_select(con, 'promises_to_pay', PROMISES)} "
                           f"FROM {_read('promises_to_pay')} ORDER BY case_id")
    _step("offers", con, f"CREATE TABLE offers AS SELECT {_select(con, 'offers', OFFERS)} "
                         f"FROM {_read('offers')} ORDER BY case_id")
    ch = _select(con, "contact_history", CONTACTS)
    _step("contacts (2.7M rows)", con, f"""
        CREATE TABLE contacts AS SELECT {ch}, coalesce(channel_v2, channel) AS channel
        FROM {_read('contact_history')} ORDER BY case_id""")
    _step("notes", con, f"CREATE TABLE notes AS SELECT {_select(con, 'agent_notes', NOTES)} "
                        f"FROM {_read('agent_notes')} WHERE case_id IS NOT NULL ORDER BY case_id")
    _step("transcripts", con, f"CREATE TABLE transcripts AS SELECT {_select(con, 'call_transcripts', TRANSCRIPTS)} "
                              f"FROM {_read('call_transcripts')} ORDER BY case_id")
    _step("agents", con, f"CREATE TABLE agents AS SELECT {_select(con, 'agents', AGENTS)} FROM {_read('agents')}")
    for name, spec, key, table in (("card_accounts", CARDS, "card_account_id", "cards"),
                                   ("loan_accounts", LOANS, "loan_id", "loans"),
                                   ("deposit_accounts", DEPOSITS, "deposit_account_id", "deposits")):
        cols = _select(con, name, spec, rename={key: "account_id"}, prefix="r.")
        _step(table, con, f"""
            CREATE TABLE {table} AS SELECT {cols} FROM {_read(name)} r
            WHERE r.{key} IN (SELECT account_id FROM case_accounts)
            QUALIFY row_number() OVER (PARTITION BY r.{key} ORDER BY TRY_CAST(r.snapshot_date AS DATE) DESC NULLS LAST) = 1
            ORDER BY account_id""")
    _step("model_scores", con, f"""
        CREATE TABLE model_scores AS SELECT * FROM (
          SELECT {_select(con, 'model_scores', SCORES)} FROM {_read('model_scores')}
          WHERE crm_customer_id IN (SELECT crm_customer_id FROM customers))
        WHERE NOT coalesce(uses_protected_attributes, false)
        QUALIFY row_number() OVER (PARTITION BY crm_customer_id, model_name ORDER BY score_date DESC NULLS LAST) = 1
        ORDER BY crm_customer_id""")
    for t in SMALL_TABLES:
        _step(t, con, f"CREATE TABLE {t} AS SELECT * FROM read_csv('{_csv(t)}', header = true, sample_size = -1)")
    for t, col in (("cases", "case_id"), ("cases", "coll_customer_ref"), ("customers", "crm_customer_id"),
                   ("promises", "case_id"), ("offers", "case_id"), ("contacts", "case_id"), ("notes", "case_id"),
                   ("transcripts", "case_id"), ("transcripts", "transcript_id"), ("case_accounts", "account_id"),
                   ("case_accounts", "case_id"), ("cards", "account_id"), ("loans", "account_id"),
                   ("deposits", "account_id"), ("model_scores", "crm_customer_id")):
        con.execute(f"CREATE INDEX idx_{t}_{col} ON {t}({col})")
    con.execute("CREATE TABLE meta AS SELECT now() AS built_ts, ? AS source_path, ? AS snapshot_date",
                [str(src), config.SNAPSHOT_DATE])
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("customers", "cases", "promises", "offers", "contacts", "notes", "transcripts", "cards",
                        "loans", "deposits", "model_scores")}
    con.close()
    tmp.replace(path)
    print(f"[layer3] done in {time.time() - t0:.0f}s: " + ", ".join(f"{k} {v:,}" for k, v in counts.items()))
    return path


if __name__ == "__main__":
    build()
