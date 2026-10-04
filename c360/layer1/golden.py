"""Golden zone: assemble the Collections 360 documents (one per person) for MongoDB.

DuckDB is only the assembly engine here. This step builds staging tables:
  golden.c360_customer    one row per person; nested STRUCT/LIST columns = the JSON document
  golden.c360_protected   protected attributes, kept apart for fairness testing only
load_mongo.py then publishes them to MongoDB (collections360.customers and
collections360.restricted_protected_attributes) and drops the staging copies.

Protected attributes (gender, marital status, citizenship, household, newcomer, accessibility,
vulnerability) never enter the customer document, so nothing that reads it can use them.
"""
import time

from c360 import config

PIPELINE_VERSION = "layer1-1.0.0"

PROTECTED_COLUMNS = [
    "salutation", "gender_code", "marital_status", "citizenship_status", "id_document_type",
    "household_size", "dependants_count", "newcomer_program_flag", "newcomer_arrival_date",
    "accessibility_needs_flag", "accessibility_type", "vulnerability_flag", "vulnerability_type",
    "vulnerability_source", "vulnerability_recorded_date", "vulnerability_review_date",
]

STAGES = {}

# ---- per-person CRM profile with survivorship ---------------------------------------------
STAGES["_g_profile"] = """
SELECT pm.c360_id,
  list(pm.crm_customer_id ORDER BY pm.is_master DESC, pm.crm_customer_id) AS crm_ids,
  -- identity and demographics come from the master record
  any_value(c.first_name) FILTER (WHERE pm.is_master) AS first_name,
  any_value(c.middle_name) FILTER (WHERE pm.is_master) AS middle_name,
  any_value(c.last_name) FILTER (WHERE pm.is_master) AS last_name,
  any_value(c.preferred_name) FILTER (WHERE pm.is_master) AS preferred_name,
  any_value(c.date_of_birth) FILTER (WHERE pm.is_master) AS date_of_birth,
  any_value(c.age) FILTER (WHERE pm.is_master) AS age,
  any_value(c.age_band) FILTER (WHERE pm.is_master) AS age_band,
  min(c.customer_since_date) AS customer_since_date,
  max(c.tenure_months) AS tenure_months,
  any_value(c.segment) FILTER (WHERE pm.is_master) AS segment,
  -- contact details: most recently updated record wins
  arg_max(c.primary_phone_std, c.last_profile_update_ts) AS primary_phone,
  arg_max(c.primary_phone_type, c.last_profile_update_ts) AS primary_phone_type,
  arg_max(c.email_std, c.last_profile_update_ts) AS email,
  arg_max(c.address_line1, c.last_profile_update_ts) AS address_line1,
  arg_max(c.city, c.last_profile_update_ts) AS city,
  arg_max(c.province_code_std, c.last_profile_update_ts) AS province_code,
  arg_max(c.postal_code_std, c.last_profile_update_ts) AS postal_code,
  arg_max(c.time_zone, c.last_profile_update_ts) AS time_zone,
  bool_or(c.returned_mail_flag) AS returned_mail_flag,
  arg_max(c.preferred_language, c.last_profile_update_ts) AS preferred_language,
  arg_max(c.preferred_channel, c.last_profile_update_ts) AS preferred_channel,
  arg_max(c.preferred_contact_window_start, c.last_profile_update_ts) AS preferred_contact_window_start,
  arg_max(c.preferred_contact_window_end, c.last_profile_update_ts) AS preferred_contact_window_end,
  arg_max(c.permitted_call_start_local, c.last_profile_update_ts) AS permitted_call_start_local,
  arg_max(c.permitted_call_end_local, c.last_profile_update_ts) AS permitted_call_end_local,
  -- employment and affordability inputs: most recent
  arg_max(c.employment_status, c.last_profile_update_ts) AS employment_status,
  arg_max(c.occupation_category, c.last_profile_update_ts) AS occupation_category,
  arg_max(c.employer_name, c.last_profile_update_ts) AS employer_name,
  arg_max(c.pay_frequency, c.last_profile_update_ts) AS pay_frequency,
  arg_max(c.declared_annual_income, c.last_profile_update_ts) AS declared_annual_income,
  bool_or(c.income_verified_flag) AS income_verified_flag,
  arg_max(c.income_source, c.last_profile_update_ts) AS income_source,
  arg_max(c.housing_status, c.last_profile_update_ts) AS housing_status,
  arg_max(c.monthly_housing_cost, c.last_profile_update_ts) AS monthly_housing_cost,
  arg_max(c.internal_risk_grade, c.last_profile_update_ts) AS internal_risk_grade,
  arg_max(c.customer_status, c.last_profile_update_ts) AS crm_status,
  -- servicing constraints: the strictest value across all records of the person
  bool_and(coalesce(c.consent_call, false)) AS consent_call,
  bool_and(coalesce(c.consent_autodialer, false)) AS consent_autodialer,
  bool_and(coalesce(c.consent_sms, false)) AS consent_sms,
  bool_and(coalesce(c.consent_email, false)) AS consent_email,
  bool_and(coalesce(c.voicemail_permitted, false)) AS voicemail_permitted,
  bool_and(coalesce(c.contact_at_work_permitted, false)) AS contact_at_work_permitted,
  bool_or(c.internal_do_not_call_flag) AS internal_do_not_call,
  bool_or(c.cease_communication_flag) AS cease_communication,
  bool_or(c.third_party_authorised_flag) AS third_party_authorised,
  bool_or(c.credit_counselling_agency_flag) AS credit_counselling_agency,
  bool_or(c.power_of_attorney_flag) AS power_of_attorney,
  bool_or(c.legal_representative_flag) AS legal_representative,
  bool_or(c.deceased_flag) AS deceased,
  bool_or(c.bankruptcy_flag) AS bankruptcy,
  bool_or(c.consumer_proposal_flag) AS consumer_proposal,
  bool_or(c.fraud_victim_flag) AS fraud_victim,
  bool_or(c.military_or_reserve_flag) AS military_or_reserve,
  bool_or(c.national_dncl_flag) AS national_dncl_telemarketing_only,
  bool_and(coalesce(c.is_deleted, false)) AS all_crm_records_deleted,
  count(*) AS crm_record_count
FROM golden.crm_person_map pm JOIN curated.customers c ON c.crm_customer_id = pm.crm_customer_id
GROUP BY pm.c360_id
"""

# ---- identity: every key in every system, with confidence ----------------------------------
STAGES["_g_identity"] = """
SELECT c360_id,
  list({'src_system': src_system, 'src_ref': src_ref, 'link_status': link_status,
        'match_confidence': round(match_confidence, 4), 'evidence': evidence} ORDER BY src_system, src_ref) AS source_keys,
  list(src_ref) FILTER (WHERE src_system = 'cards') AS card_customer_refs,
  list(src_ref) FILTER (WHERE src_system = 'loans') AS borrower_refs,
  list(src_ref) FILTER (WHERE src_system = 'core') AS cifs,
  min(match_confidence) AS min_link_confidence,
  count(*) FILTER (WHERE link_status NOT IN ('auto_link', 'human_confirmed')) AS links_pending_review
FROM golden.source_customer_xref WHERE c360_id IS NOT NULL GROUP BY 1
"""

# ---- accounts --------------------------------------------------------------------------------
STAGES["_g_cards"] = """
SELECT x.c360_id, list({
    'account_id': a.card_account_id, 'role': x.role, 'product_code': a.product_code, 'card_tier': a.card_tier,
    'account_status': a.account_status, 'open_date': a.open_date, 'snapshot_date': a.snapshot_date,
    'credit_limit': a.credit_limit, 'current_balance': a.current_balance, 'statement_balance': a.statement_balance,
    'utilisation': a.utilisation, 'minimum_payment_due': a.minimum_payment_due, 'payment_due_date': a.payment_due_date,
    'past_due_amount': a.past_due_amount, 'dpd': a.dpd, 'dpd_bucket': a.dpd_bucket_recomputed,
    'dpd_bucket_as_stored': a.dpd_bucket, 'worst_dpd_12m': a.worst_dpd_12m, 'times_30dpd_12m': a.times_30dpd_12m,
    'last_payment_date': a.last_payment_date, 'last_payment_amount': a.last_payment_amount,
    'autopay_enrolled': a.autopay_enrolled, 'autopay_failed_count_90d': a.autopay_failed_count_90d,
    'hardship_program_flag': a.hardship_program_flag, 'hardship_program_type': a.hardship_program_type,
    'dispute_open_flag': a.dispute_open_flag, 'chargeoff_flag': a.chargeoff_flag, 'chargeoff_date': a.chargeoff_date,
    'chargeoff_amount': a.chargeoff_amount, 'cash_advance_count_90d': a.cash_advance_count_90d,
    'link_status': x.link_status, 'link_confidence': round(x.match_confidence, 4),
    'dq_flags': list_filter([CASE WHEN a.dq_dpd_missing THEN 'dpd_missing' END,
                             CASE WHEN a.dq_bucket_mismatch THEN 'stored_bucket_mismatch' END,
                             CASE WHEN a.dq_stale_snapshot THEN 'stale_snapshot' END], lambda f: f IS NOT NULL)
  } ORDER BY a.open_date) AS cards
FROM golden.account_xref x JOIN curated.card_accounts a ON a.card_account_id = x.account_id
WHERE x.product_family = 'card' AND x.c360_id IS NOT NULL GROUP BY 1
"""

STAGES["_g_loans"] = """
SELECT x.c360_id, list({
    'account_id': a.loan_id, 'role': x.role, 'product_type': a.product_type, 'product_code': a.product_code,
    'secured_flag': a.secured_flag, 'origination_date': a.origination_date, 'maturity_date': a.maturity_date,
    'original_amount': a.original_amount, 'interest_rate': a.interest_rate, 'payment_frequency': a.payment_frequency,
    'scheduled_payment_amount': a.scheduled_payment_amount, 'next_due_date': a.next_due_date,
    'next_due_amount': a.next_due_amount, 'principal_balance': a.principal_balance,
    'total_outstanding': a.total_outstanding, 'past_due_amount': a.past_due_amount,
    'missed_payments_count': a.missed_payments_count, 'dpd': a.dpd, 'dpd_bucket': a.dpd_bucket_recomputed,
    'dpd_bucket_as_stored': a.dpd_bucket, 'worst_dpd_12m': a.worst_dpd_12m,
    'last_payment_date': a.last_payment_date, 'last_payment_amount': a.last_payment_amount,
    'pad_enrolled': a.pad_enrolled, 'pad_returned_count_90d': a.pad_returned_count_90d,
    'deferral_count_life': a.deferral_count_life, 'restructure_flag': a.restructure_flag,
    'hardship_program_flag': a.hardship_program_flag, 'hardship_program_type': a.hardship_program_type,
    'chargeoff_flag': a.chargeoff_flag, 'chargeoff_date': a.chargeoff_date, 'chargeoff_amount': a.chargeoff_amount,
    'repossession_flag': a.repossession_flag, 'snapshot_date': a.snapshot_date,
    'link_status': x.link_status, 'link_confidence': round(x.match_confidence, 4),
    'dq_flags': list_filter([CASE WHEN a.dq_dpd_missing THEN 'dpd_missing' END,
                             CASE WHEN a.dq_bucket_mismatch THEN 'stored_bucket_mismatch' END,
                             CASE WHEN a.dq_stale_snapshot THEN 'stale_snapshot' END], lambda f: f IS NOT NULL)
  } ORDER BY a.origination_date) AS loans
FROM golden.account_xref x JOIN curated.loan_accounts a ON a.loan_id = x.account_id
WHERE x.product_family = 'loan' AND x.c360_id IS NOT NULL GROUP BY 1
"""

STAGES["_g_deposits"] = """
SELECT x.c360_id, list({
    'account_id': a.deposit_account_id, 'role': x.role, 'product_type': a.product_type, 'currency': a.currency,
    'account_status': a.account_status, 'open_date': a.open_date, 'current_balance': a.current_balance,
    'available_balance': a.available_balance, 'overdraft_limit': a.overdraft_limit, 'overdraft_used': a.overdraft_used,
    'overdraft_dpd': a.overdraft_dpd, 'overdraft_dpd_bucket': a.overdraft_dpd_bucket_recomputed,
    'days_negative_mtd': a.days_negative_mtd, 'nsf_count_mtd': a.nsf_count_mtd,
    'payroll_deposit_flag': a.payroll_deposit_flag, 'payroll_frequency_detected': a.payroll_frequency_detected,
    'last_payroll_date': a.last_payroll_date, 'last_payroll_amount': a.last_payroll_amount,
    'expected_next_payroll_date': a.expected_next_payroll_date, 'payroll_delay_days': a.payroll_delay_days,
    'payroll_avg_amount_3m': a.payroll_avg_amount_3m, 'payroll_amount_change_pct': a.payroll_amount_change_pct,
    'ei_deposit_flag': a.ei_deposit_flag, 'cpp_oas_deposit_flag': a.cpp_oas_deposit_flag,
    'other_lender_payment_amount_mtd': a.other_lender_payment_amount_mtd,
    'high_cost_lender_txn_flag': a.high_cost_lender_txn_flag,
    'essential_spend_amount_mtd': a.essential_spend_amount_mtd, 'net_cash_flow_mtd': a.net_cash_flow_mtd,
    'cash_flow_volatility_6m': a.cash_flow_volatility_6m,
    'income_to_debt_service_ratio_est': a.income_to_debt_service_ratio_est,
    'garnishment_flag': a.garnishment_flag, 'snapshot_date': a.snapshot_date,
    'link_status': x.link_status, 'link_confidence': round(x.match_confidence, 4),
    'dq_flags': list_filter([CASE WHEN a.dq_cif_leading_zeros_lost THEN 'cif_leading_zeros_restored' END,
                             CASE WHEN a.dq_stale_snapshot THEN 'stale_snapshot' END], lambda f: f IS NOT NULL)
  } ORDER BY a.open_date) AS deposits
FROM golden.account_xref x JOIN curated.deposit_accounts a ON a.deposit_account_id = x.account_id
WHERE x.product_family = 'deposit' AND x.c360_id IS NOT NULL GROUP BY 1
"""

STAGES["_g_salary"] = """
SELECT ax.c360_id,
  count(DISTINCT s.credit_month) AS salary_months_observed,
  avg(s.credit_amount_total) FILTER (WHERE s.credit_month >= '2026-04') AS salary_avg_6m,
  avg(s.credit_amount_total) FILTER (WHERE s.credit_month >= '2025-10' AND s.credit_month < '2026-04') AS salary_avg_prior_6m,
  count(*) FILTER (WHERE s.gap_flag AND s.credit_month >= '2026-04') AS salary_gap_months_6m,
  max(s.last_credit_date) AS salary_last_credit_date
FROM curated.salary_credit_history s
JOIN golden.account_xref ax ON ax.account_id = s.deposit_account_id AND ax.role = 'primary' AND ax.c360_id IS NOT NULL
GROUP BY 1
"""

# ---- bureau ------------------------------------------------------------------------------------
STAGES["_g_bureau"] = """
WITH e AS (
    SELECT x.c360_id, e.*
    FROM curated.external e JOIN golden.source_customer_xref x ON x.src_system = 'core' AND x.src_ref = e.cif
    WHERE x.c360_id IS NOT NULL
    QUALIFY row_number() OVER (PARTITION BY x.c360_id ORDER BY e.pull_date DESC) = 1
), h AS (
    SELECT x.c360_id, list({'pull_date': b.pull_date, 'risk_score': b.risk_score} ORDER BY b.pull_date DESC)[1:6] AS score_history
    FROM curated.bureau_history b JOIN golden.source_customer_xref x ON x.src_system = 'core' AND x.src_ref = b.cif
    WHERE x.c360_id IS NOT NULL GROUP BY 1
)
SELECT e.c360_id, {
    'pull_date': e.pull_date, 'hit_status': e.hit_status, 'risk_score': e.risk_score,
    'risk_score_prev_3m': e.risk_score_prev_3m, 'risk_score_delta_90d': e.risk_score_delta_90d,
    'bankruptcy_score': e.bankruptcy_score, 'inquiries_hard_6m': e.inquiries_hard_6m,
    'inquiries_hard_3m': e.inquiries_hard_3m_std, 'revolving_utilisation': e.revolving_utilisation,
    'total_monthly_obligations': e.total_monthly_obligations, 'total_balance_all_trades': e.total_balance_all_trades,
    'off_us_revolving_balance': e.off_us_revolving_balance, 'off_us_delinquent_trades': e.off_us_delinquent_trades,
    'collection_items_count': e.collection_items_count, 'high_cost_lender_trades_count': e.high_cost_lender_trades_count,
    'bnpl_trades_count': e.bnpl_trades_count, 'new_trades_6m': e.new_trades_6m,
    'bankruptcy_on_file_flag': e.bankruptcy_on_file_flag, 'consumer_proposal_on_file_flag': e.consumer_proposal_on_file_flag,
    'deceased_indicator': e.deceased_indicator, 'fraud_alert_flag': e.fraud_alert_flag,
    'address_mismatch_flag': e.address_mismatch_flag,
    'dq_shifted_row': e.dq_shifted_row, 'score_history': h.score_history
  } AS bureau
FROM e LEFT JOIN h USING (c360_id)
"""

# ---- collections -------------------------------------------------------------------------------
STAGES["_g_case_person"] = """
SELECT case_id, c360_id, link_method, match_confidence FROM golden.case_xref WHERE c360_id IS NOT NULL
"""

STAGES["_g_ptp_case"] = """
SELECT case_id,
  count(*) AS promises_total,
  count(*) FILTER (WHERE ptp_status = 'kept') AS promises_kept,
  count(*) FILTER (WHERE ptp_status = 'partially_kept') AS promises_partially_kept,
  count(*) FILTER (WHERE ptp_status = 'broken') AS promises_broken,
  count(*) FILTER (WHERE ptp_status = 'open') AS promises_open,
  sum(amount_paid_against) AS amount_paid_against_promises,
  arg_max({'ptp_id': ptp_id, 'amount': ptp_amount, 'due_date': ptp_due_date, 'status': ptp_status,
           'created_ts': ptp_created_ts, 'channel': ptp_channel}, ptp_created_ts) AS latest_promise
FROM curated.promises_to_pay GROUP BY 1
"""

STAGES["_g_offers_case"] = """
SELECT case_id, list({'offer_id': offer_id, 'program_id': program_id, 'offer_ts': offer_ts, 'offered_by': offered_by,
    'proposed_payment_amount': proposed_payment_amount, 'duration_months': duration_months,
    'customer_response': customer_response, 'plan_status': plan_status} ORDER BY offer_ts DESC) AS offers
FROM curated.offers GROUP BY 1
"""

STAGES["_g_cases"] = """
SELECT cp.c360_id, list({
    'case_id': c.case_id, 'case_open_date': c.case_open_date, 'case_open_reason': c.case_open_reason,
    'case_status': c.case_status, 'is_open': c.is_open, 'primary_account_id': c.primary_account_id,
    'primary_product': c.primary_product, 'account_ids': c.account_ids, 'current_dpd': c.current_dpd,
    'current_bucket': c.current_bucket_recomputed, 'current_bucket_as_stored': c.current_bucket,
    'total_overdue': c.total_overdue, 'total_exposure': c.total_exposure, 'queue': c.queue,
    'assigned_agent_id': c.assigned_agent_id, 'current_treatment': c.current_treatment,
    'next_scheduled_action': c.next_scheduled_action, 'contact_attempts_total': c.contact_attempts_total,
    'rpc_count': c.rpc_count, 'last_contact_ts': c.last_contact_ts, 'last_rpc_ts': c.last_rpc_ts,
    'hardship_flag': c.hardship_flag, 'hardship_reason': c.hardship_reason,
    'hardship_program_offered': c.hardship_program_offered, 'hardship_program_accepted': c.hardship_program_accepted,
    'payment_plan_flag': c.payment_plan_flag, 'plan_instalment_amount': c.plan_instalment_amount,
    'settlement_offered_flag': c.settlement_offered_flag, 'dispute_flag': c.dispute_flag,
    'complaint_flag': c.complaint_flag, 'cease_contact_flag': c.cease_contact_flag,
    'third_party_rep_flag': c.third_party_rep_flag, 'insolvency_hold_flag': c.insolvency_hold_flag,
    'deceased_hold_flag': c.deceased_hold_flag, 'legal_referral_flag': c.legal_referral_flag,
    'chargeoff_flag': c.chargeoff_flag, 'nba_recommendation_last': c.nba_recommendation_last,
    'outcome': c.outcome, 'outcome_date': c.outcome_date, 'cure_flag': c.cure_flag,
    'promises': {'total': p.promises_total, 'kept': p.promises_kept, 'partially_kept': p.promises_partially_kept,
                 'broken': p.promises_broken, 'open': p.promises_open, 'amount_paid': p.amount_paid_against_promises,
                 'latest': p.latest_promise},
    'offers': o.offers,
    'link_method': cp.link_method, 'link_confidence': round(cp.match_confidence, 4),
    'dq_flags': list_filter([CASE WHEN c.dq_bucket_mismatch THEN 'stored_bucket_mismatch' END,
                             CASE WHEN cp.link_method <> 'crm_id_exact' THEN 'customer_id_repaired' END], lambda f: f IS NOT NULL)
  } ORDER BY c.case_open_date DESC) AS cases
FROM curated.collections_cases c
JOIN golden._g_case_person cp ON cp.case_id = c.case_id
LEFT JOIN golden._g_ptp_case p ON p.case_id = c.case_id
LEFT JOIN golden._g_offers_case o ON o.case_id = c.case_id
GROUP BY 1
"""

STAGES["_g_contact_person"] = """
SELECT h.contact_id, coalesce(cp.c360_id, ox.c360_id) AS c360_id
FROM curated.contact_history h
LEFT JOIN golden._g_case_person cp ON cp.case_id = h.case_id
LEFT JOIN golden.contact_orphan_xref ox ON ox.contact_id = h.contact_id
"""

STAGES["_g_contacts"] = """
SELECT p.c360_id,
  count(*) FILTER (WHERE h.direction = 'outbound' AND h.channel_std <> 'system') AS outbound_attempts_total,
  count(*) FILTER (WHERE h.direction = 'outbound' AND h.channel_std <> 'system' AND h.contact_ts_local >= TIMESTAMP '2026-08-29') AS outbound_attempts_30d,
  count(*) FILTER (WHERE h.direction = 'outbound' AND h.channel_std = 'call' AND h.contact_ts_local >= TIMESTAMP '2026-09-21') AS outbound_calls_7d,
  count(*) FILTER (WHERE h.rpc_flag) AS rpc_total,
  count(*) FILTER (WHERE h.rpc_flag AND h.contact_ts_local >= TIMESTAMP '2026-08-29') AS rpc_30d,
  count(*) FILTER (WHERE h.direction = 'inbound') AS inbound_contacts_total,
  count(*) FILTER (WHERE h.hardship_mentioned_flag) AS hardship_mentions_total,
  count(*) FILTER (WHERE h.hardship_mentioned_flag AND h.contact_ts_local >= TIMESTAMP '2026-06-30') AS hardship_mentions_90d,
  count(*) FILTER (WHERE h.dispute_raised_flag) AS disputes_raised,
  count(*) FILTER (WHERE h.complaint_raised_flag) AS complaints_raised,
  bool_or(h.cease_request_flag) AS cease_requested_on_contact,
  sum(h.payment_amount) FILTER (WHERE h.payment_taken_flag) AS payments_taken_on_contacts,
  max(h.contact_ts_local) AS last_contact_ts,
  max(h.contact_ts_local) FILTER (WHERE h.rpc_flag) AS last_rpc_ts,
  arg_max(h.channel_std, h.contact_ts_local) AS last_contact_channel,
  arg_max(h.outcome_code, h.contact_ts_local) AS last_outcome_code,
  mode(h.contact_hour_local) FILTER (WHERE h.rpc_flag) AS most_common_rpc_hour
FROM curated.contact_history h JOIN golden._g_contact_person p ON p.contact_id = h.contact_id
WHERE p.c360_id IS NOT NULL AND h.dq_duplicate_of IS NULL GROUP BY 1
"""

STAGES["_g_notes"] = """
SELECT cp.c360_id,
  count(*) AS notes_total,
  list({'note_id': n.note_id, 'note_ts': n.note_ts, 'note_type': n.note_type, 'agent_id': n.agent_id,
        'case_id': n.case_id, 'note_text': n.note_text} ORDER BY n.note_ts DESC)[1:5] AS recent_notes
FROM curated.agent_notes n JOIN golden._g_case_person cp ON cp.case_id = n.case_id GROUP BY 1
"""

STAGES["_g_transcripts"] = """
SELECT cp.c360_id,
  list({'transcript_id': t.transcript_id, 'contact_id': t.contact_id, 'case_id': t.case_id,
        'call_start_ts': t.call_start_ts, 'duration_sec': t.duration_sec, 'direction': t.direction,
        'language': t.language, 'voice_sample_id': t.voice_sample_id, 'file_path': t.file_path}
       ORDER BY t.call_start_ts DESC) AS call_transcripts
FROM curated.call_transcripts t JOIN golden._g_case_person cp ON cp.case_id = t.case_id GROUP BY 1
"""

STAGES["_g_scores"] = """
WITH s AS (
    SELECT coalesce(pm.c360_id, cp.c360_id) AS c360_id, m.*
    FROM curated.model_scores m
    LEFT JOIN golden.crm_person_map pm ON pm.crm_customer_id = m.crm_customer_id
    LEFT JOIN golden._g_case_person cp ON cp.case_id = m.case_id
    WHERE coalesce(pm.c360_id, cp.c360_id) IS NOT NULL AND NOT m.dq_superseded_duplicate
    QUALIFY row_number() OVER (PARTITION BY coalesce(pm.c360_id, cp.c360_id), m.model_name ORDER BY m.score_ts DESC, m.score_id DESC) = 1
)
SELECT c360_id, list({'model_name': model_name, 'model_version': model_version, 'score_date': score_date,
    'score': score, 'score_band': score_band, 'recommended_action': recommended_action,
    'top_reason_1_text': top_reason_1_text, 'uses_protected_attributes': uses_protected_attributes}
    ORDER BY model_name) AS model_scores
FROM s GROUP BY 1
"""


FINAL = """
CREATE OR REPLACE TABLE golden.c360_customer AS
WITH acc AS (
    SELECT p.c360_id,
      coalesce(list_sum([x.current_balance FOR x IN c.cards]), 0) AS card_balance_total,
      coalesce(list_sum([x.total_outstanding FOR x IN l.loans IF x.role = 'primary']), 0) AS loan_outstanding_total,
      coalesce(list_sum([x.current_balance FOR x IN d.deposits IF x.role = 'primary' AND x.currency = 'CAD']), 0) AS deposit_balance_total,
      coalesce(list_sum([x.past_due_amount FOR x IN c.cards]), 0) + coalesce(list_sum([x.past_due_amount FOR x IN l.loans IF x.role = 'primary']), 0) AS past_due_total,
      greatest(coalesce(list_max([x.dpd FOR x IN c.cards]), 0), coalesce(list_max([x.dpd FOR x IN l.loans IF x.role = 'primary']), 0),
               coalesce(list_max([x.overdraft_dpd FOR x IN d.deposits]), 0)) AS max_dpd,
      coalesce(len(list_filter(c.cards, lambda f: f.dpd >= 1)), 0) + coalesce(len(list_filter(l.loans, lambda f: f.dpd >= 1 AND f.role = 'primary')), 0) AS delinquent_accounts,
      coalesce(list_bool_or([x.hardship_program_flag FOR x IN c.cards]), false) OR coalesce(list_bool_or([x.hardship_program_flag FOR x IN l.loans]), false) AS on_hardship_program,
      coalesce(list_bool_or([x.ei_deposit_flag FOR x IN d.deposits]), false) AS ei_deposits,
      coalesce(list_max([x.payroll_delay_days FOR x IN d.deposits]), 0) AS max_payroll_delay_days,
      list_min([x.payroll_amount_change_pct FOR x IN d.deposits]) AS min_payroll_change_pct,
      coalesce(list_bool_or([x.garnishment_flag FOR x IN d.deposits]), false) AS garnishment,
      coalesce(list_sum([x.nsf_count_mtd FOR x IN d.deposits]), 0) AS nsf_count_mtd
    FROM golden._g_profile p
    LEFT JOIN golden._g_cards c USING (c360_id) LEFT JOIN golden._g_loans l USING (c360_id) LEFT JOIN golden._g_deposits d USING (c360_id)
)
SELECT
  p.c360_id,
  p.crm_ids[1] AS master_crm_id,
  {'crm_ids': p.crm_ids, 'crm_record_count': p.crm_record_count,
   'card_customer_refs': i.card_customer_refs, 'borrower_refs': i.borrower_refs, 'cifs': i.cifs,
   'source_keys': i.source_keys,
   'min_link_confidence': round(coalesce(i.min_link_confidence, 1.0), 4),
   'links_pending_review': coalesce(i.links_pending_review, 0)} AS identity,
  {'first_name': p.first_name, 'middle_name': p.middle_name, 'last_name': p.last_name,
   'preferred_name': p.preferred_name, 'date_of_birth': p.date_of_birth, 'age': p.age, 'age_band': p.age_band,
   'customer_since_date': p.customer_since_date, 'tenure_months': p.tenure_months, 'segment': p.segment,
   'crm_status': p.crm_status, 'all_crm_records_deleted': p.all_crm_records_deleted} AS profile,
  {'primary_phone': p.primary_phone, 'primary_phone_type': p.primary_phone_type, 'email': p.email,
   'address_line1': p.address_line1, 'city': p.city, 'province_code': p.province_code,
   'postal_code': p.postal_code, 'time_zone': p.time_zone, 'returned_mail_flag': p.returned_mail_flag,
   'preferred_language': p.preferred_language, 'preferred_channel': p.preferred_channel,
   'preferred_contact_window': [p.preferred_contact_window_start, p.preferred_contact_window_end],
   'permitted_call_window_local': [p.permitted_call_start_local, p.permitted_call_end_local]} AS contact,
  {'consent_call': p.consent_call, 'consent_autodialer': p.consent_autodialer, 'consent_sms': p.consent_sms,
   'consent_email': p.consent_email, 'voicemail_permitted': p.voicemail_permitted,
   'contact_at_work_permitted': p.contact_at_work_permitted, 'internal_do_not_call': p.internal_do_not_call,
   'cease_communication': p.cease_communication OR coalesce(ct.cease_requested_on_contact, false),
   'third_party_authorised': p.third_party_authorised, 'credit_counselling_agency': p.credit_counselling_agency,
   'power_of_attorney': p.power_of_attorney, 'legal_representative': p.legal_representative,
   'deceased': p.deceased, 'bankruptcy': p.bankruptcy, 'consumer_proposal': p.consumer_proposal,
   'fraud_victim': p.fraud_victim, 'military_or_reserve': p.military_or_reserve,
   'national_dncl_telemarketing_only': p.national_dncl_telemarketing_only} AS servicing_constraints,
  {'employment_status': p.employment_status, 'occupation_category': p.occupation_category,
   'employer_name': p.employer_name, 'pay_frequency': p.pay_frequency,
   'declared_annual_income': p.declared_annual_income, 'income_verified_flag': p.income_verified_flag,
   'income_source': p.income_source, 'housing_status': p.housing_status,
   'monthly_housing_cost': p.monthly_housing_cost, 'internal_risk_grade': p.internal_risk_grade,
   'salary_avg_6m': s.salary_avg_6m, 'salary_avg_prior_6m': s.salary_avg_prior_6m,
   'salary_gap_months_6m': s.salary_gap_months_6m, 'salary_last_credit_date': s.salary_last_credit_date} AS financial_profile,
  {'cards': coalesce(c.cards, []), 'loans': coalesce(l.loans, []), 'deposits': coalesce(d.deposits, [])} AS accounts,
  {'card_balance_total': round(a.card_balance_total, 2), 'loan_outstanding_total': round(a.loan_outstanding_total, 2),
   'deposit_balance_total': round(a.deposit_balance_total, 2), 'past_due_total': round(a.past_due_total, 2),
   'max_dpd': a.max_dpd, 'worst_bucket': dpd_bucket_from_dpd(a.max_dpd), 'delinquent_accounts': a.delinquent_accounts,
   'product_count': coalesce(len(c.cards), 0) + coalesce(len(l.loans), 0) + coalesce(len(d.deposits), 0)} AS exposure,
  b.bureau,
  {'cases': coalesce(cs.cases, []),
   'open_cases': coalesce(len(list_filter(cs.cases, lambda f: f.is_open)), 0),
   'contacts': {'outbound_attempts_total': ct.outbound_attempts_total, 'outbound_attempts_30d': ct.outbound_attempts_30d,
                'outbound_calls_7d': ct.outbound_calls_7d, 'rpc_total': ct.rpc_total, 'rpc_30d': ct.rpc_30d,
                'inbound_contacts_total': ct.inbound_contacts_total, 'disputes_raised': ct.disputes_raised,
                'complaints_raised': ct.complaints_raised, 'payments_taken_on_contacts': ct.payments_taken_on_contacts,
                'last_contact_ts': ct.last_contact_ts, 'last_rpc_ts': ct.last_rpc_ts,
                'last_contact_channel': ct.last_contact_channel, 'last_outcome_code': ct.last_outcome_code,
                'most_common_rpc_hour': ct.most_common_rpc_hour},
   'notes_total': coalesce(n.notes_total, 0), 'recent_notes': coalesce(n.recent_notes, []),
   'call_transcripts': coalesce(t.call_transcripts, [])} AS collections,
  -- Structured hardship indicators (rule: flag financial hardship). Text and voice signals are added by Layer 3.
  {'financial_hardship_flag':
       coalesce(list_bool_or([x.hardship_flag FOR x IN cs.cases IF x.is_open]), false) OR a.on_hardship_program
       OR a.ei_deposits OR a.max_payroll_delay_days > 7 OR coalesce(a.min_payroll_change_pct, 0) <= -0.2
       OR coalesce(ct.hardship_mentions_90d, 0) > 0 OR a.garnishment,
   'reasons': list_filter([
       CASE WHEN coalesce(list_bool_or([x.hardship_flag FOR x IN cs.cases IF x.is_open]), false) THEN 'open collections case is coded as hardship' END,
       CASE WHEN a.on_hardship_program THEN 'an account is on a hardship programme' END,
       CASE WHEN a.ei_deposits THEN 'Employment Insurance deposits are arriving' END,
       CASE WHEN a.max_payroll_delay_days > 7 THEN 'pay is ' || a.max_payroll_delay_days || ' days late' END,
       CASE WHEN coalesce(a.min_payroll_change_pct, 0) <= -0.2 THEN 'pay fell ' || round(-100 * a.min_payroll_change_pct) || '% versus the 3-month average' END,
       CASE WHEN coalesce(ct.hardship_mentions_90d, 0) > 0 THEN 'customer mentioned hardship on ' || ct.hardship_mentions_90d || ' contact(s) in the last 90 days' END,
       CASE WHEN a.garnishment THEN 'wage garnishment on a deposit account' END], lambda f: f IS NOT NULL),
   'hardship_mentions_total': coalesce(ct.hardship_mentions_total, 0),
   'nsf_count_mtd': a.nsf_count_mtd} AS hardship_indicators,
  coalesce(sc.model_scores, []) AS model_scores,
  list_filter([
    CASE WHEN p.crm_record_count > 1 THEN 'merged ' || p.crm_record_count || ' duplicate CRM records' END,
    CASE WHEN coalesce(i.links_pending_review, 0) > 0 THEN i.links_pending_review || ' source link(s) awaiting review' END,
    CASE WHEN list_bool_or([x.link_method <> 'crm_id_exact' FOR x IN cs.cases]) THEN 'collections case customer id was repaired' END,
    CASE WHEN p.all_crm_records_deleted THEN 'all CRM records soft-deleted' END,
    CASE WHEN b.bureau.dq_shifted_row THEN 'bureau pull affected by load incident DFI-0812' END
  ], lambda f: f IS NOT NULL) AS data_quality_notes,
  {'pipeline_version': '""" + PIPELINE_VERSION + """', 'built_ts': now()::TIMESTAMP, 'snapshot_date': DATE '2026-09-28',
   'sources': ['customers', 'card_accounts', 'loan_accounts', 'deposit_accounts', 'external', 'bureau_history',
               'salary_credit_history', 'collections_cases', 'promises_to_pay', 'offers', 'contact_history',
               'agent_notes', 'call_transcripts', 'model_scores']} AS lineage
FROM golden._g_profile p
LEFT JOIN golden._g_identity i USING (c360_id)
LEFT JOIN golden._g_cards c USING (c360_id)
LEFT JOIN golden._g_loans l USING (c360_id)
LEFT JOIN golden._g_deposits d USING (c360_id)
LEFT JOIN golden._g_salary s USING (c360_id)
LEFT JOIN golden._g_bureau b USING (c360_id)
LEFT JOIN golden._g_cases cs USING (c360_id)
LEFT JOIN golden._g_contacts ct USING (c360_id)
LEFT JOIN golden._g_notes n USING (c360_id)
LEFT JOIN golden._g_transcripts t USING (c360_id)
LEFT JOIN golden._g_scores sc USING (c360_id)
LEFT JOIN acc a USING (c360_id)
"""




def build_golden(con=None):
    own = con is None
    con = con or config.connect()
    from c360.layer1 import normalize
    normalize.install(con)
    t0 = time.time()
    for name, sql in STAGES.items():
        t1 = time.time()
        con.execute(f"CREATE OR REPLACE TABLE golden.{name} AS {sql}")
        print(f"    golden.{name:<20} {con.execute(f'SELECT count(*) FROM golden.{name}').fetchone()[0]:>10,} rows ({time.time() - t1:.1f}s)", flush=True)
    con.execute(FINAL)
    # Protected attributes: separate table, for fairness testing only.
    cols = ", ".join(f"c.{c}" for c in PROTECTED_COLUMNS)
    con.execute(f"""
    CREATE OR REPLACE TABLE golden.c360_protected AS
    SELECT pm.c360_id, c.crm_customer_id, {cols}
    FROM golden.crm_person_map pm JOIN curated.customers c ON c.crm_customer_id = pm.crm_customer_id
    WHERE pm.is_master
    """)
    con.execute("COMMENT ON TABLE golden.c360_protected IS 'RESTRICTED: protected attributes for fairness testing only. Never a decision input.'")
    n = con.execute("SELECT count(*) FROM golden.c360_customer").fetchone()[0]
    print(f"  golden.c360_customer (staging): {n:,} documents ({time.time() - t0:.1f}s)", flush=True)
    for name in STAGES:  # staging tables are rebuilt each run
        if name not in ("_g_case_person", "_g_contact_person"):
            con.execute(f"DROP TABLE IF EXISTS golden.{name}")
    if own:
        con.close()


if __name__ == "__main__":
    build_golden()
