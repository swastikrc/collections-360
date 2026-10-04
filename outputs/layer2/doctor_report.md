# Layer 2 doctor report

Source database: `/home/swastikroychowdhury/Documents/cibc/warehouse/c360.duckdb`  
Generated: 2026-10-04 19:45:57  
**0 error(s), 0 warning(s), 108 note(s).** publish_l2 can run.

## Errors (must fix)

None.

## Warnings (explain or fix)

None.

## Notes

| Check | Subject | Finding |
|---|---|---|
| source | `src` | opened read-only; schemas found: curated, golden |
| schema | `curated.customers` | 1,020,000 rows, 156 columns |
| schema | `curated.card_accounts` | 660,000 rows, 154 columns |
| schema | `curated.loan_accounts` | 404,500 rows, 141 columns |
| schema | `curated.deposit_accounts` | 780,000 rows, 152 columns |
| schema | `curated.account_monthly_snapshot` | 12,569,692 rows, 19 columns |
| schema | `curated.collections_cases` | 367,229 rows, 137 columns |
| schema | `curated.promises_to_pay` | 238,815 rows, 31 columns |
| schema | `curated.offers` | 40,767 rows, 21 columns |
| schema | `curated.contact_history` | 2,688,337 rows, 133 columns |
| schema | `curated.agent_notes` | 760,594 rows, 28 columns |
| schema | `curated.call_transcripts` | 25,000 rows, 31 columns |
| schema | `curated.external` | 1,000,000 rows, 127 columns |
| schema | `curated.bureau_history` | 4,679,562 rows, 12 columns |
| schema | `curated.salary_credit_history` | 6,000,000 rows, 12 columns |
| schema | `curated.model_scores` | 1,718,841 rows, 36 columns |
| schema | `curated.transactions` | 12,401,924 rows, 27 columns |
| schema | `curated.metric_definitions` | 15 rows, 16 columns |
| schema | `curated.benchmark_questions` | 35 rows, 9 columns |
| schema | `curated.benchmark_questions.question` | inferred column missing (used by: nothing in l2_views.yaml) |
| schema | `curated.benchmark_questions.expected_answer` | inferred column missing (used by: nothing in l2_views.yaml) |
| schema | `curated.benchmark_questions.gold_sql` | inferred column missing (used by: nothing in l2_views.yaml) |
| schema | `curated.benchmark_questions.category` | inferred column missing (used by: nothing in l2_views.yaml) |
| schema | `curated.code_lookups` | 113 rows, 9 columns |
| schema | `curated.hardship_programs` | 18 rows, 23 columns |
| schema | `curated.agents` | 1,000 rows, 32 columns |
| schema | `curated.case_assignment_history` | 423,830 rows, 25 columns |
| schema | `curated.reference_documents` | 19 rows, 19 columns |
| schema | `curated.reference_documents.document_id` | inferred column missing (used by: nothing in l2_views.yaml) |
| schema | `curated.qa_checklist` | 10 rows, 12 columns |
| schema | `curated.agent_shifts` | 15,317 rows, 17 columns |
| schema | `curated.channel_capacity` | 287 rows, 13 columns |
| schema | `curated.channel_capacity.capacity_date` | inferred column missing (used by: nothing in l2_views.yaml) |
| schema | `curated.ops_events` | 6 rows, 12 columns |
| schema | `curated.geo_reference` | 656 rows, 17 columns |
| schema | `curated.geo_reference.province_name` | inferred column missing (used by: nothing in l2_views.yaml) |
| schema | `curated._column_docs` | 1,442 rows, 4 columns |
| schema | `curated._cast_failures` | 855 rows, 7 columns |
| schema | `golden.crm_person_map` | 1,020,000 rows, 7 columns |
| schema | `golden.source_customer_xref` | 1,873,567 rows, 17 columns |
| schema | `golden.account_xref` | 2,054,672 rows, 9 columns |
| schema | `golden.case_xref` | 367,229 rows, 6 columns |
| schema | `golden.contact_orphan_xref` | 26,864 rows, 5 columns |
| schema | `golden._g_contact_person` | 2,688,337 rows, 2 columns |
| schema | `golden._g_case_person` | 367,215 rows, 4 columns |
| schema | `golden.match_review_queue` | 801 rows, 13 columns |
| schema | `golden.dq_results` | 271 rows, 10 columns |
| views | `l2.customers` | binds (37 columns) |
| views | `l2.card_accounts` | binds (40 columns) |
| views | `l2.loan_accounts` | binds (45 columns) |
| views | `l2.deposit_accounts` | binds (39 columns) |
| views | `l2.account_monthly_snapshot` | binds (16 columns) |
| views | `l2.collections_cases` | binds (75 columns) |
| views | `l2.promises_to_pay` | binds (21 columns) |
| views | `l2.offers` | binds (20 columns) |
| views | `l2.contact_history` | binds (56 columns) |
| views | `l2.agent_notes` | binds (10 columns) |
| views | `l2.call_transcripts` | binds (11 columns) |
| views | `l2.bureau_latest` | binds (24 columns) |
| views | `l2.bureau_history` | binds (8 columns) |
| views | `l2.salary_credit_history` | binds (9 columns) |
| views | `l2.model_scores` | binds (17 columns) |
| views | `l2.hardship_programs` | binds (21 columns) |
| views | `l2.agents` | binds (16 columns) |
| views | `l2.case_assignment_history` | binds (20 columns) |
| views | `l2.code_lookups` | binds (7 columns) |
| views | `l2.metric_definitions` | binds (14 columns) |
| views | `l2.ops_events` | binds (10 columns) |
| views | `l2.qa_checklist` | binds (10 columns) |
| views | `l2.dq_results` | binds (10 columns) |
| unexpected | `curated.account_monthly_snapshot` | columns not mentioned anywhere (decide whether to allow-list): batch_id |
| unexpected | `curated.agent_notes` | columns not mentioned anywhere (decide whether to allow-list): abbreviation_count, batch_id, contains_amount_flag, contains_date_flag, edit_count, edited_flag, file_path, last_edited_ts, note_language, note_length_chars, note_ts_utc, redaction_status, retention_until, src_system, template_id, word_count |
| unexpected | `curated.agent_shifts` | columns not mentioned anywhere (decide whether to allow-list): absence_flag, absence_reason, break_minutes, end_local, evening_slot_capacity, outbound_call_capacity, overtime_flag, queue_assignment, scheduled_minutes, shift_id, site_time_zone, start_local, worked_minutes |
| unexpected | `curated.agents` | columns not mentioned anywhere (decide whether to allow-list): agent_display_name (deny-listed), avg_handle_time_sec, coaching_flag, complaints_90d, cure_rate_90d, current_case_load, fte, hire_date, max_concurrent_cases, ptp_kept_rate_90d, ptp_rate_90d, qa_score_avg_90d, rpc_rate_90d, team_lead_id |
| unexpected | `curated.benchmark_questions` | columns not mentioned anywhere (decide whether to allow-list): as_of_date, difficulty, metric_id, question_text, split, tolerance |
| unexpected | `curated.bureau_history` | columns not mentioned anywhere (decide whether to allow-list): risk_score_model |
| unexpected | `curated.call_transcripts` | columns not mentioned anywhere (decide whether to allow-list): agent_talk_ratio, asr_confidence_avg, asr_engine, asr_model_version, batch_id, call_recording_id, code_switch_flag, crm_customer_id, customer_talk_ratio, diarization_confidence, hold_count, overtalk_count, pci_redaction_applied, pii_redaction_applied, silence_ratio, speaker_count, src_system, wer_estimate |
| unexpected | `curated.card_accounts` | columns not mentioned anywhere (decide whether to allow-list): annual_fee, auth_declines_30d, authorized_user_count, autopay_source_account, autopay_type, balance_protection_insurance_flag, balance_transfer_balance, batch_id, block_code, block_date, block_reason_text, card_network, card_number_masked, cardholder_dob_raw (deny-listed), cardholder_name_raw (deny-listed), cardholder_postal_raw (deny-listed), cash_advance_balance, cash_advance_limit, cash_advances_mtd, cash_apr, dispute_amount, dpd_m01, dpd_m02, dpd_m03, dpd_m04, dpd_m05, dpd_m06, dpd_m07, dpd_m08, dpd_m09, dpd_m10, dpd_m11, dpd_m12, extract_ts, fees_charged_mtd, fraud_block_flag, hardship_end_date, hardship_start_date, instalment_plan_balance, instalment_plan_flag, insurance_claim_status, interest_charged_mtd, largest_purchase_30d, last_auth_decline_date, last_payment_channel, limit_change_date, limit_change_reason, min_payment_formula_code, nsf_payment_fee_mtd, overlimit_amount, overlimit_fee_mtd, payment_amount_m01, payment_amount_m02, payment_amount_m03, payment_amount_m04, payment_amount_m05, payment_amount_m06, payment_amount_m07, payment_amount_m08, payment_amount_m09, payment_amount_m10, payment_amount_m11, payment_amount_m12, payment_history_24m, payments_mtd, penalty_rate_flag, previous_credit_limit, promo_balance, promo_expiry_date, promo_rate, purchase_apr, purchase_balance, purchases_mtd, reage_date, reage_flag, record_hash, recovery_amount_to_date, rewards_points_balance, rewards_program, secured_card_flag, security_deposit_amount, snapshot_type, src_file_name, src_system, statement_cycle_day, statement_date, stmt_balance_m01, stmt_balance_m02, stmt_balance_m03, stmt_balance_m04, stmt_balance_m05, stmt_balance_m06, stmt_balance_m07, stmt_balance_m08, stmt_balance_m09, stmt_balance_m10, stmt_balance_m11, stmt_balance_m12, top_mcc_category_30d, utilisation_m01, utilisation_m02, utilisation_m03, utilisation_m04, utilisation_m05, utilisation_m06, utilisation_m07, utilisation_m08, utilisation_m09, utilisation_m10, utilisation_m11, utilisation_m12 |
| unexpected | `curated.card_statements` | table not in expected_schema.yaml (27 columns): _load_ts, _src_file, amount_paid_by_due, batch_id, card_account_id, cash_advances_amount, closing_balance, credit_limit_at_statement, cycle_no, cycle_start_date, cycles_delinquent_at_statement, days_late, dpd_at_statement, due_date, estatement_flag, fees_amount, fees_reversed_amount, first_payment_after_due_date, interest_amount, min_paid_flag, minimum_due, opening_balance, paid_in_full_flag, past_due_included, payments_amount, purchases_amount, statement_date |
| unexpected | `curated.case_assignment_history` | columns not mentioned anywhere (decide whether to allow-list): agent_case_load_at_assignment, agent_cure_rate_90d_at_assignment, assignment_reason, routing_rule_id |
| unexpected | `curated.channel_capacity` | columns not mentioned anywhere (decide whether to allow-list): capacity_unit_type, capacity_units, daily_budget_cad, date, ops_event_id, outage_flag, time_band, unit_cost_cad, used_units, vendor |
| unexpected | `curated.collections_cases` | columns not mentioned anywhere (decide whether to allow-list): accounts_in_case_count, agency_name, agency_placement_date, assignment_date, batch_id, bucket_entry_date, bucket_path, case_status_date, complaint_date, contact_rule_set, cost_to_collect_est, current_ptp_amount, current_ptp_due_date, current_ptp_id, dispute_date, dpd_at_day_14, dpd_at_day_21, dpd_at_day_30, dpd_at_day_45, dpd_at_day_60, dpd_at_day_7, dpd_at_day_90, entry_overdue, escalation_reason, expected_loss_est, extract_ts, hardship_plan_end, hardship_plan_start, human_override_flag, last_contact_channel, last_note_id, nba_accepted_flag, next_action_ts, notes_count, overdue_at_day_14, overdue_at_day_21, overdue_at_day_30, overdue_at_day_45, overdue_at_day_60, overdue_at_day_7, overdue_at_day_90, override_reason, payments_since_open_count, plan_instalments_paid, plan_instalments_total, prior_bucket, promises_open, qa_flag_count, record_hash, recovered_post_chargeoff, repossession_initiated_flag, roll_back_count, roll_forward_count, settlement_pct, skip_trace_flag, skip_trace_status, src_system, strategy_version, supervisor_review_flag, treatment_path, vulnerable_customer_flag (deny-listed) |
| unexpected | `curated.contact_history` | columns not mentioned anywhere (decide whether to allow-list): agent_coded_sentiment, agent_identified_self_flag, agent_tenure_band_at_contact, amd_result, answer_rate_prior_30d, app_payment_after_contact_flag, app_session_after_contact_flag, attempt_number_day, batch_id, broken_ptp_count_at_contact, call_recording_id, callback_ts, campaign_id, chat_message_count, chat_session_id, contacts_prior_7d, customer_time_zone, days_since_case_open_at_contact, device_type, dialer_mode, disclosure_version, email_bounce_type, email_clicked_flag, email_client, email_delivered_flag, email_open_ts, email_opened_flag, email_subject (deny-listed), email_unsubscribe_flag, extract_ts, hold_duration_sec, interpreter_used_flag, ivr_path, ivr_self_serve_payment_flag, language_used, letter_mailed_date, letter_returned_flag, link_expiry_ts, message_language, minutes_since_prev_contact, open_ptp_at_contact_flag, outcome_desc, overdue_at_contact, phone_number_hash (deny-listed), phone_type_dialled (deny-listed), push_delivered_flag, push_opened_flag, push_template_id, qa_reviewer_id, qa_score, qa_selected_flag, queue_wait_sec, record_hash, recording_notice_read_flag, recording_retention_until, ring_duration_sec, rpc_count_prior_30d, script_id, sms_delivered_ts, sms_opt_out_flag, sms_reply_flag, sms_reply_text (deny-listed), sms_segment_count, sms_text (deny-listed), src_file_name, src_system, sub_outcome_code, third_party_contact_flag, transfer_target, treatment_code, vendor_id, voice_sample_id, vulnerability_disclosed_flag (deny-listed), wrap_duration_sec |
| unexpected | `curated.customers` | columns not mentioned anywhere (decide whether to allow-list): address_line2 (deny-listed), address_since_date, aml_risk_rating, app_push_enabled, batch_id, casl_consent_date, casl_consent_source, casl_express_consent_email, cdc_operation, cease_communication_date, consent_call_mobile, consent_last_updated_ts, consent_letter, consent_push, correspondence_language, country_code, customer_uuid, deceased_notified_date, digital_banking_enrolled, effective_from_ts, effective_to_ts, email_bounce_flag, email_verified_flag, employment_start_date, estatements_flag, extract_ts, fsa (deny-listed), full_name_raw (deny-listed), geo_lat (deny-listed), geo_lon (deny-listed), has_auto_loan, has_chequing, has_credit_card, has_heloc, has_line_of_credit, has_mortgage, has_personal_loan, has_registered_investments, has_savings, home_branch_city, home_branch_transit, income_verified_date, industry_naics2, insolvency_filed_date, kyc_last_review_date, kyc_status, last_login_ts, licensed_insolvency_trustee_name, logins_30d, marketing_opt_out_flag, mobile_app_user_flag, name_suffix (deny-listed), national_id_present_flag (deny-listed), pep_flag, preferred_contact_days, primary_phone_e164 (deny-listed), product_count, record_hash, relationship_manager_id, secondary_phone_raw (deny-listed), sms_consent_date, src_system, sub_segment, sunday_contact_permitted, third_party_relationship, total_credit_exposure, total_relationship_balance, urban_rural, work_phone_raw (deny-listed) |
| unexpected | `curated.deposit_accounts` | columns not mentioned anywhere (decide whether to allow-list): atm_withdrawal_amount_mtd, avg_daily_balance_mtd, batch_id, bill_payment_amount_mtd, bill_payment_count_mtd, ccb_deposit_flag, credits_amount_m01, credits_amount_m02, credits_amount_m03, credits_amount_m04, credits_amount_m05, credits_amount_m06, credits_amount_m07, credits_amount_m08, credits_amount_m09, credits_amount_m10, credits_amount_m11, credits_amount_m12, credits_amount_mtd, credits_count_mtd, debits_amount_m01, debits_amount_m02, debits_amount_m03, debits_amount_m04, debits_amount_m05, debits_amount_m06, debits_amount_m07, debits_amount_m08, debits_amount_m09, debits_amount_m10, debits_amount_m11, debits_amount_m12, debits_amount_mtd, debits_count_mtd, discretionary_spend_amount_mtd, dormant_days, ei_first_deposit_date, estatement_flag, etransfer_in_amount_mtd, etransfer_in_count_mtd, etransfer_out_amount_mtd, etransfer_out_count_mtd, extract_ts, freeze_reason, gst_hst_credit_flag, hold_amount, holder_dob_raw (deny-listed), holder_name_raw (deny-listed), holder_phone_raw (deny-listed), holder_postal_raw (deny-listed), institution_number, max_balance_mtd, min_balance_mtd, month_end_balance, month_end_balance_m01, month_end_balance_m02, month_end_balance_m03, month_end_balance_m04, month_end_balance_m05, month_end_balance_m06, month_end_balance_m07, month_end_balance_m08, month_end_balance_m09, month_end_balance_m10, month_end_balance_m11, month_end_balance_m12, net_cash_flow_m01, net_cash_flow_m02, net_cash_flow_m03, net_cash_flow_m04, net_cash_flow_m05, net_cash_flow_m06, net_cash_flow_m07, net_cash_flow_m08, net_cash_flow_m09, net_cash_flow_m10, net_cash_flow_m11, net_cash_flow_m12, nsf_count_m01, nsf_count_m02, nsf_count_m03, nsf_count_m04, nsf_count_m05, other_lender_payment_count_mtd, overdraft_protection_flag, pad_debit_count_mtd, payroll_amount_m01, payroll_amount_m02, payroll_amount_m03, payroll_amount_m04, payroll_amount_m05, payroll_amount_m06, payroll_amount_m07, payroll_amount_m08, payroll_amount_m09, payroll_amount_m10, payroll_amount_m11, payroll_amount_m12, payroll_count_mtd, payroll_descriptor_raw (deny-listed), pos_debit_amount_mtd, provincial_benefit_flag, record_hash, rent_payment_detected_flag, returned_pad_count_mtd, snapshot_type, src_file_name, src_system, transit_number |
| unexpected | `curated.external` | columns not mentioned anywhere (decide whether to allow-list): avg_age_of_trades_months, balance_growth_6m_pct, batch_id, bureau_file_number, bureau_name, bureau_request_id, closed_trades_12m, collection_items_balance, consumer_statement_flag, file_freeze_flag, file_received_ts, file_since_date, heloc_trades_count, highest_revolving_limit, inquiries_hard_12m, inquiries_soft_12m, instalment_balance_total, instalment_count_i2_12m, instalment_count_i2_24m, instalment_count_i3_12m, instalment_count_i3_24m, instalment_count_i4_12m, instalment_count_i4_24m, instalment_count_i5_12m, instalment_count_i5_24m, instalment_count_i9_12m, instalment_count_i9_24m, instalment_past_due_total, instalment_trades_open, instalment_worst_rating_24m, instalment_worst_rating_current, judgment_count, months_since_last_delinquency_any, months_since_last_inquiry, months_since_newest_trade, months_since_oldest_trade, mortgage_balance_total, mortgage_trades_count, open_balance_total, open_count_o2_12m, open_count_o2_24m, open_count_o3_12m, open_count_o3_24m, open_count_o4_12m, open_count_o4_24m, open_count_o5_12m, open_count_o5_24m, open_count_o9_12m, open_count_o9_24m, open_past_due_total, open_trades_open, open_worst_rating_24m, open_worst_rating_current, payment_to_balance_ratio_3m, public_record_date, pull_type, record_hash, registered_items_count, revolving_balance_total, revolving_count_r2_12m, revolving_count_r2_24m, revolving_count_r3_12m, revolving_count_r3_24m, revolving_count_r4_12m, revolving_count_r4_24m, revolving_count_r5_12m, revolving_count_r5_24m, revolving_count_r9_12m, revolving_count_r9_24m, revolving_past_due_total, revolving_trades_open, revolving_util_trend_6m, revolving_worst_rating_24m, revolving_worst_rating_current, risk_score_m01, risk_score_m02, risk_score_m03, risk_score_m04, risk_score_m05, risk_score_m06, risk_score_m07, risk_score_m08, risk_score_m09, risk_score_m10, risk_score_m11, risk_score_m12, risk_score_model, score_reason_code_1, score_reason_code_2, score_reason_code_3, score_reason_code_4, src_file_name, src_system, subject_address_reported, subject_dob_reported, telecom_trades_delinquent, total_balance_revolving, total_limit_revolving |
| unexpected | `curated.geo_reference` | columns not mentioned anywhere (decide whether to allow-list): area_codes, broad_region, centroid_lat, centroid_lon, city (deny-listed), cma_name, common_region_name, economic_region, fsa (deny-listed), fsa_income_index (deny-listed), majority_language, population_weight, time_zone, urban_rural |
| unexpected | `curated.loan_accounts` | columns not mentioned anywhere (decide whether to allow-list): accrued_interest, amended_payment_amount, available_to_draw, batch_id, borrower_dob_raw (deny-listed), borrower_name_raw (deny-listed), borrower_phone_raw (deny-listed), borrower_postal_raw (deny-listed), co_borrower_flag, creditor_insurance_flag, dealer_id, dpd_m01, dpd_m02, dpd_m03, dpd_m04, dpd_m05, dpd_m06, dpd_m07, dpd_m08, dpd_m09, dpd_m10, dpd_m11, dpd_m12, extract_ts, fees_outstanding, hardship_end_date, hardship_start_date, insurance_claim_status, interest_only_min_flag, last_deferral_date, last_payment_method, loan_purpose, loan_to_value, loc_utilisation, nsf_fee_count_12m, original_term_months, origination_channel, pad_source_account, paid_amount_m01, paid_amount_m02, paid_amount_m03, paid_amount_m04, paid_amount_m05, paid_amount_m06, paid_amount_m07, paid_amount_m08, paid_amount_m09, paid_amount_m10, paid_amount_m11, paid_amount_m12, payment_due_day, payment_history_24m, payoff_amount, ppsa_registration_flag, prime_rate_at_snapshot, principal_balance_m01, principal_balance_m02, principal_balance_m03, principal_balance_m04, principal_balance_m05, principal_balance_m06, principal_balance_m07, principal_balance_m08, principal_balance_m09, principal_balance_m10, principal_balance_m11, principal_balance_m12, rate_spread_over_prime, rate_type, record_hash, recovery_amount_to_date, repossession_date, restructure_date, revolving_flag, scheduled_amount_m01, scheduled_amount_m02, scheduled_amount_m03, scheduled_amount_m04, scheduled_amount_m05, scheduled_amount_m06, scheduled_amount_m07, scheduled_amount_m08, scheduled_amount_m09, scheduled_amount_m10, scheduled_amount_m11, scheduled_amount_m12, snapshot_type, src_file_name, src_system, vehicle_make, vehicle_model, vehicle_value_estimate, vehicle_vin_hash, vehicle_year |
| unexpected | `curated.loan_instalments` | table not in expected_schema.yaml (20 columns): _load_ts, _src_file, batch_id, days_late_when_paid, deferred_flag, due_amount, due_date, fee_component, instalment_no, instalment_status, interest_component, loan_id, pad_attempt_date, pad_result, pad_retry_date, paid_amount, paid_date, principal_component, restructured_flag, schedule_version |
| unexpected | `curated.model_scores` | columns not mentioned anywhere (decide whether to allow-list): answer_rate_estimate, auc_at_validation, features_snapshot_id, model_owner, psi_last_month, ptp_id, slot_rank, threshold_used, top_reason_1, top_reason_1_contribution, top_reason_2, top_reason_2_contribution, top_reason_2_text, top_reason_3, top_reason_3_contribution, top_reason_3_text |
| unexpected | `curated.promises_to_pay` | columns not mentioned anywhere (decide whether to allow-list): batch_id, customer_stated_income_date, days_to_due_at_creation, payment_method_promised, ptp_to_overdue_ratio, reminder_scheduled_flag, src_system, status_ts |
| unexpected | `curated.reference_documents` | columns not mentioned anywhere (decide whether to allow-list): applies_to_products, applies_to_provinces, confidentiality, doc_id, doc_type, effective_date, file_path, language, owner, page_count, related_incident_id, related_table, section_ids, superseded_by, version, word_count |
| unexpected | `curated.salary_credit_history` | columns not mentioned anywhere (decide whether to allow-list): credit_count |
| unexpected | `curated.transactions` | columns not mentioned anywhere (decide whether to allow-list): account_type, balance_after, batch_id, benefit_type, category, channel, counterparty_name_clean (deny-listed), currency, is_payroll_flag, payroll_employer_key, posting_date, ptp_id_matched, return_reason, returned_flag, reversal_of_txn_id, target_account_id, txn_id, txn_type, value_date |
| unexpected | `curated.voice_samples` | table not in expected_schema.yaml (36 columns): _load_ts, _src_file, agent_accent_region, agent_voice_id, audio_format, background_noise_type, bit_depth, channel_map, channels, checksum_sha256, clipping_pct, consent_basis, contact_id, customer_accent_region, customer_energy_mean_db, customer_filler_rate, customer_pause_count, customer_pitch_mean_hz, customer_pitch_std_hz, customer_voice_id, dead_air_sec, duration_sec, file_path, file_size_bytes, hold_music_sec, language, original_codec, packet_loss_pct, sample_rate_hz, snr_db, speaking_rate_wpm_agent, speaking_rate_wpm_customer, synthetic_voice_flag, transcript_id, voice_sample_id, watermark_flag |
| unexpected | `golden.c360_interactions` | table not in expected_schema.yaml (9 columns): c360_id, case_id, interaction_id, interaction_type, occurred_ts, payload, source, written_by, written_ts |
| unexpected | `golden.match_llm_verdicts` | table not in expected_schema.yaml (7 columns): c360_id, llm_match_pct, llm_reason, model, src_ref, src_system, verdict_ts |
| unexpected | `golden.match_model_prior` | table not in expected_schema.yaml (2 columns): prior_match_prob, src_system |
| unexpected | `golden.match_model_weights` | table not in expected_schema.yaml (7 columns): field, level, log2_bf, m, meaning, src_system, u |
| unexpected | `golden.match_review_decisions` | table not in expected_schema.yaml (7 columns): c360_id, decided_ts, decision, note, reviewer, src_ref, src_system |
| unexpected | `golden.match_review_queue` | columns not mentioned anywhere (decide whether to allow-list): dob_raw (deny-listed), llm_match_pct, llm_reason, name_raw (deny-listed), phone_raw (deny-listed), postal_raw (deny-listed), top_candidates |
| reference | `curated.metric_definitions` | columns: metric_id, metric_name, business_definition, formula, grain, time_basis, default_filters, unit, good_direction, synonyms, source_tables, example_question, owner, certified_flag, _src_file, _load_ts |
| reference | `curated.benchmark_questions` | columns: question_id, question_text, difficulty, metric_id, as_of_date, tolerance, split, _src_file, _load_ts |
| reference | `curated.code_lookups` | columns: code_type, code, description_en, description_fr, attributes_json, active_flag, effective_from, _src_file, _load_ts |
| reference | `curated.hardship_programs` | columns: program_id, version, program_name, product_types, policy_section, description, min_hardship_level, max_dpd, min_months_on_book, max_uses_12m, excludes_insolvency, excludes_broken_plan_90d, requires_income_evidence, payment_reduction_pct, rate_override, duration_months, term_extension_max_months, fees_waived_flag, approver_level, effective_from, effective_to, _src_file, _load_ts |
| transcripts | `l2.call_transcripts` | 20 of 20 sampled files exist under /home/swastikroychowdhury/Documents/cibc/maple_collections_release (folders tried: files, .); 20 give text; top-level JSON keys of the first readable file: agent_id, agent_talk_ratio, asr_confidence_avg, asr_engine, asr_model_version, batch_id, call_end_ts, call_recording_id, call_start_ts, case_id, code_switch_flag, contact_id, crm_customer_id, customer_talk_ratio, diarization_confidence, direction, duration_sec, file_path, hold_count, language, overtalk_count, pci_redaction_applied, pii_redaction_applied, silence_ratio, speaker_count, src_system, transcript_id, turns, voice_sample_id, wer_estimate |
| mongo | `customers` | reachable, 1,000,008 documents |
| text_search | `fts` | full-text extension available: text_search_mode will be `fts` |

## Reference tables (first rows; reference data only)

### curated.metric_definitions

Columns: `metric_id`, `metric_name`, `business_definition`, `formula`, `grain`, `time_basis`, `default_filters`, `unit`, `good_direction`, `synonyms`, `source_tables`, `example_question`, `owner`, `certified_flag`, `_src_file`, `_load_ts`

| metric_id | metric_name | business_definition | formula | grain | time_basis | default_filters | unit | good_direction | synonyms | source_tables | example_question | owner | certified_flag | _src_file | _load_ts |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| roll_rate | Roll rate | Share of accounts in a bucket at the start of the period that are in a worse bucket at the end | count(worse_bucket_at_end) / count(in_bucket_at_start) | account | month-end to month-end; weekly variant uses Monday snapshots | exclude charged_off and insolvency_hold | pct | down | ['flow rate', 'roll forward', 'deterioration'] | ['account_monthly_snapshot', 'card_accounts', 'loan_accounts'] | Roll rate from 1-30 to 31-60 this month by product? | Collections Analytics | True | data/metric_definitions.csv | 2026-10-03 13:19:35.738714+05:30 |
| cure_rate | Cure rate | Share of cases open at the start of the period that return to current within the period | count(cases cured in period) / count(cases open at period start) | case | calendar month | exclude insolvency_hold and deceased_hold | pct | up | ['self-cure', 'back to current', 'cured'] | ['collections_cases'] | What was the cure rate for cases opened in August? | Collections Analytics | True | data/metric_definitions.csv | 2026-10-03 13:19:35.738714+05:30 |
| promise_kept_rate | Promise-kept rate | Share of resolved promises that were kept in full | count(ptp_status = 'kept') / count(ptp_status in ('kept','partially_kept','broken')) | promise | promise due date in period | exclude open and cancelled promises | pct | up | ['PTP kept rate', 'promise kept', 'kept promises'] | ['promises_to_pay'] | What is the promise-kept rate this month by queue? | Collections Analytics | True | data/metric_definitions.csv | 2026-10-03 13:19:35.738714+05:30 |

### curated.benchmark_questions

Columns: `question_id`, `question_text`, `difficulty`, `metric_id`, `as_of_date`, `tolerance`, `split`, `_src_file`, `_load_ts`

| question_id | question_text | difficulty | metric_id | as_of_date | tolerance | split | _src_file | _load_ts |
|---|---|---|---|---|---|---|---|---|
| BQ-001 | Is CASL express consent needed to send a collections payment reminder by email? | medium | None | 2026-09-28 | None | test | data/benchmark_questions.csv | 2026-10-03 13:19:35.745154+05:30 |
| BQ-002 | How many call attempts can we make on a mid-stage account in a week? | medium | None | 2026-09-28 | None | dev | data/benchmark_questions.csv | 2026-10-03 13:19:35.745154+05:30 |
| BQ-003 | How do broken-promise rates change with the number of previously broken promises? | medium | broken_promise_rate | 2026-09-28 | ±0.5 pts | dev | data/benchmark_questions.csv | 2026-10-03 13:19:35.745154+05:30 |

### curated.code_lookups

Columns: `code_type`, `code`, `description_en`, `description_fr`, `attributes_json`, `active_flag`, `effective_from`, `_src_file`, `_load_ts`

| code_type | code | description_en | description_fr | attributes_json | active_flag | effective_from | _src_file | _load_ts |
|---|---|---|---|---|---|---|---|---|
| outcome_code | PTP | Promise to pay | Promesse de paiement | {"counts_as_rpc": true} | True | 2025-01-01 | data/code_lookups.csv | 2026-10-03 13:19:37.155592+05:30 |
| outcome_code | RPC-NP | Right party contact - no promise | Contact avec le client - sans promesse | {"counts_as_rpc": true} | True | 2025-01-01 | data/code_lookups.csv | 2026-10-03 13:19:37.155592+05:30 |
| outcome_code | LM | Left message | Message laissé | {"counts_as_rpc": false} | True | 2025-01-01 | data/code_lookups.csv | 2026-10-03 13:19:37.155592+05:30 |

### curated.hardship_programs

Columns: `program_id`, `version`, `program_name`, `product_types`, `policy_section`, `description`, `min_hardship_level`, `max_dpd`, `min_months_on_book`, `max_uses_12m`, `excludes_insolvency`, `excludes_broken_plan_90d`, `requires_income_evidence`, `payment_reduction_pct`, `rate_override`, `duration_months`, `term_extension_max_months`, `fees_waived_flag`, `approver_level`, `effective_from`, `effective_to`, `_src_file`, `_load_ts`

| program_id | version | program_name | product_types | policy_section | description | min_hardship_level | max_dpd | min_months_on_book | max_uses_12m | excludes_insolvency | excludes_broken_plan_90d | requires_income_evidence | payment_reduction_pct | rate_override | duration_months | term_extension_max_months | fees_waived_flag | approver_level | effective_from | effective_to | _src_file | _load_ts |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| HP-CARD-RATE | 2025.1 | Reduced interest rate | ['card'] | §4.2 | Purchase APR reduced to 9.99% for 6 months | clear | 120 | 12 | 1 | True | True | False | None | 0.0999 | 6 | None | True | agent | 2025-03-01 | 2026-02-28 | data/hardship_programs.csv | 2026-10-03 13:19:35.781575+05:30 |
| HP-CARD-RATE | 2026.1 | Reduced interest rate | ['card'] | §4.2 | Purchase APR reduced to 9.99% for 6 months | clear | 120 | 12 | 1 | True | True | False | None | 0.0999 | 6 | None | True | agent | 2026-03-01 | 9999-12-31 | data/hardship_programs.csv | 2026-10-03 13:19:35.781575+05:30 |
| HP-CARD-MIN | 2025.1 | Reduced minimum payment | ['card'] | §4.2 | Minimum payment reduced by 50% for 3 months; fees waived | possible | 90 | 6 | 1 | True | True | False | 0.5 | None | 3 | None | True | agent | 2025-03-01 | 2026-02-28 | data/hardship_programs.csv | 2026-10-03 13:19:35.781575+05:30 |

