# Layer 2 evaluation: benchmark_benchmark_questions_extra

Run 2026-10-04 20:28 · model `rotation: gemini-3.8-flash, gemini-3.7-flash, gemini-3.6-flash, gemini-3-flash-preview, gemini-3.5-flash` · serving build 20261004T141506 · data as of 2026-09-28

## Summary

- Accuracy: **n/a** (0 of 0 auto-scored; 6 manual, 0 skipped)
- Refusal accuracy: n/a
- Paraphrase groups answered consistently: n/a
- Average tool calls: 2.33 · average latency: 26.68 s
- Trap-rule hits (validator caught a mistake): 0
- Answers containing a phone number, email or deny-listed field: 0
- Governed-metric answers: 1 · custom-SQL answers: 3

### By tag

| Tag | Passed | Scored | Accuracy |
|---|---:|---:|---:|
| benchmark | 0 | 0 | n/a |
| split:extra | 0 | 0 | n/a |

## Questions

### BQ-104 — MANUAL

**Q:** Summarise the most recent recorded call on case CS-2026-453815: what did the customer say and agree to?

- Expected (manual): `""`
- Got (answered): In the most recent call on September 27, 2026, the customer stated he is currently waiting for Employment Insurance (EI) benefits after applying in August and does not have sufficient funds. He complained about the frequency of contact attempts and requested a payment reduction. He agreed to a promise to pay $50 by October 17, 2026, and accepted a 'Skip Payment' hardship offer; the call was subseq
- Check: manual review
- Tools: get_customer, search_text, search_text · no query · 3 call(s) · 33.34 s
- SQL: `SELECT t.transcript_id AS id, t.case_id, t.c360_id, t.call_start_ts AS ts, t.direction AS extra, t.transcript_text AS body, 0 AS score FROM l2.call_transcripts t WHERE t.transcript_text IS NOT NULL AND t.case_id = 'CS-2026-453815' ORDER BY ts DESC LIMIT 20`
- SQL: `SELECT n.note_id AS id, n.case_id, n.c360_id, n.note_ts AS ts, n.note_type AS extra, n.note_text AS body, fts_l2_agent_notes.match_bm25(n.note_id, 'EI complaint reduced payment') AS score FROM l2.agent_notes n WHERE score IS NOT NULL AND n.note_text IS NOT NULL AND n.case_id = 'CS-2026-453815' ORDER BY score DESC, ts DESC LIMIT 20 ; SELECT t.transcript_id AS id, t.case_id, t.c360_id, t.call_start_ts AS ts, t.direction AS extra, t.transcript_text AS body, fts_l2_call_transcripts.match_bm25(t.tran`
- Warnings: Removed 1 customer name mention(s) from the answer.

### BQ-108 — MANUAL

**Q:** In September 2026, what share of payment links sent by SMS were clicked?

- Expected (manual): `""`
- Got (answered): In September 2026, 12.11% of payment links sent via SMS were clicked. Out of 38,321 links sent during this period, 4,639 were clicked by customers. The click rate remained relatively stable throughout the month, ranging from 11.54% to 12.68% on a weekly basis. [table: 4 rows]
- Check: manual review
- Tools: run_sql, run_sql, run_sql · custom · 3 call(s) · 37.74 s
- SQL: `SELECT * FROM l2.ops_events WHERE start_ts <= '2026-09-28' AND (end_ts >= '2026-09-01' OR end_ts IS NULL) LIMIT 51`
- SQL: `SELECT COUNT(*) AS links_sent, COUNT(CASE WHEN link_clicked_flag = TRUE THEN 1 END) AS links_clicked, CAST(COUNT(CASE WHEN link_clicked_flag = TRUE THEN 1 END) AS DOUBLE) / COUNT(*) AS click_rate FROM l2.contact_history WHERE channel = 'sms' AND NOT payment_link_id IS NULL AND is_duplicate = FALSE AND CAST(contact_ts_local AS DATE) BETWEEN '2026-09-01' AND '2026-09-28' LIMIT 51`
- SQL: `SELECT DATE_TRUNC('WEEK', CAST(contact_ts_local AS DATE)) AS week_start, COUNT(*) AS links_sent, COUNT(CASE WHEN link_clicked_flag = TRUE THEN 1 END) AS links_clicked, CAST(COUNT(CASE WHEN link_clicked_flag = TRUE THEN 1 END) AS DOUBLE) / COUNT(*) AS click_rate FROM l2.contact_history WHERE channel = 'sms' AND NOT payment_link_id IS NULL AND is_duplicate = FALSE AND CAST(contact_ts_local AS DATE) BETWEEN '2026-09-01' AND '2026-09-28' GROUP BY 1 ORDER BY 1 LIMIT 51`
- Warnings: `affected_customers_est` is 35,000, larger than the biggest table in the query (6 rows): a join is probably multiplying rows. Count DISTINCT ids.

### BQ-112 — MANUAL

**Q:** What was the promise-kept rate for promises due between 1 and 20 September 2026, by the channel the promise was taken on?

- Expected (manual): `""`
- Got (answered): For promises due between 1 and 20 September 2026, the web channel had the highest promise-kept rate at 43.98%, followed closely by IVR at 43.12%. The call channel, which handled the largest volume of promises (over 18,000), had a kept rate of 34.86%. [table: 5 rows]
- Check: manual review
- Tools: compute_metric · governed · 1 call(s) · 9.88 s
- SQL: `SELECT p.ptp_channel AS channel, count(*) FILTER (WHERE p.ptp_status = 'kept') AS numerator, count(*) AS denominator, CAST(count(*) FILTER (WHERE p.ptp_status = 'kept') AS DOUBLE) / nullif(count(*), 0) AS value FROM l2.promises_to_pay p LEFT JOIN l2.collections_cases c ON c.case_id = p.case_id LEFT JOIN l2.agents ag ON ag.agent_id = p.agent_id WHERE CAST(p.ptp_due_date AS DATE) BETWEEN DATE '2026-09-01' AND DATE '2026-09-20' AND p.ptp_status IN ('kept', 'partially_kept', 'broken') GROUP BY ALL O`
- Warnings: number not found in results: 18,000

### BQ-113 — MANUAL

**Q:** Summarise the most recent recorded call on case CS-2026-123126: what did the customer say and agree to?

- Expected (manual): `""`
- Got (answered): On the most recent call on September 27, 2026, the customer stated that their hours at work were reduced in June, making the full past-due amount unaffordable. The customer agreed to a promise to pay $23.08 due on October 10, 2026, and expressed interest in hardship options, resulting in a referral to the hardship team.
- Check: manual review
- Tools: run_sql, search_text, search_text, run_sql, search_text · custom · 5 call(s) · 45.5 s
- SQL: `SELECT contact_id, contact_ts_local, channel, direction FROM l2.contact_history WHERE case_id = 'CS-2026-123126' AND channel = 'call' AND is_duplicate = FALSE ORDER BY contact_ts_local DESC LIMIT 1`
- SQL: `SELECT n.note_id AS id, n.case_id, n.c360_id, n.note_ts AS ts, n.note_type AS extra, n.note_text AS body, CASE WHEN n.note_text ILIKE '%3128646%' THEN 1 ELSE 0 END AS score FROM l2.agent_notes n WHERE (CASE WHEN n.note_text ILIKE '%3128646%' THEN 1 ELSE 0 END) > 0 AND n.note_text IS NOT NULL ORDER BY score DESC, ts DESC LIMIT 20 ; SELECT t.transcript_id AS id, t.case_id, t.c360_id, t.call_start_ts AS ts, t.direction AS extra, t.transcript_text AS body, CASE WHEN t.transcript_text ILIKE '%3128646`
- SQL: `SELECT n.note_id AS id, n.case_id, n.c360_id, n.note_ts AS ts, n.note_type AS extra, n.note_text AS body, 0 AS score FROM l2.agent_notes n WHERE n.note_text IS NOT NULL AND n.case_id = 'CS-2026-123126' ORDER BY ts DESC LIMIT 20 ; SELECT t.transcript_id AS id, t.case_id, t.c360_id, t.call_start_ts AS ts, t.direction AS extra, t.transcript_text AS body, 0 AS score FROM l2.call_transcripts t WHERE t.transcript_text IS NOT NULL AND t.case_id = 'CS-2026-123126' ORDER BY ts DESC LIMIT 20`
- SQL: `SELECT ptp_id, ptp_amount, ptp_due_date, ptp_status, ptp_channel, ptp_created_ts FROM l2.promises_to_pay WHERE case_id = 'CS-2026-123126' ORDER BY ptp_created_ts DESC LIMIT 51`
- SQL: `SELECT t.transcript_id AS id, t.case_id, t.c360_id, t.call_start_ts AS ts, t.direction AS extra, t.transcript_text AS body, fts_l2_call_transcripts.match_bm25(t.transcript_id, 'reduced work hours hardship') AS score FROM l2.call_transcripts t WHERE score IS NOT NULL AND t.transcript_text IS NOT NULL AND t.case_id = 'CS-2026-123126' ORDER BY score DESC, ts DESC LIMIT 20`
- Warnings: No notes or transcripts matched: try other keywords or fewer filters.

### BQ-114 — MANUAL

**Q:** During what hours may we make collection calls, and what applies on Sundays?

- Expected (manual): `""`
- Got (answered): Under POL-COLL-001 §5.1, collection calls and SMS messages may only be made between 07:00 and 21:00 in the customer's local time. Where Sunday contact is permitted, calls are strictly limited to the window of 13:00 to 17:00 local time. Contact is completely prohibited on statutory holidays.
- Check: manual review
- Tools: search_policy · no query · 1 call(s) · 16.3 s

### BQ-115 — MANUAL

**Q:** How many open cases are on insolvency hold, and how much is overdue on them?

- Expected (manual): `""`
- Got (answered): There are 6,581 open collections cases currently on insolvency hold, with a total overdue balance of $9,305,609.24.
- Check: manual review
- Tools: run_sql · custom · 1 call(s) · 17.29 s
- SQL: `SELECT COUNT(*) AS total_insolvency_cases, COUNT(CASE WHEN is_open = TRUE THEN 1 END) AS open_insolvency_cases_by_flag, SUM(CASE WHEN is_open = TRUE THEN total_overdue ELSE 0 END) AS total_overdue_by_flag, COUNT(CASE WHEN is_open = TRUE AND case_status = 'insolvency_hold' THEN 1 END) AS open_cases_status_insolvency, SUM(CASE WHEN is_open = TRUE AND case_status = 'insolvency_hold' THEN total_overdue ELSE 0 END) AS total_overdue_status_insolvency FROM l2.collections_cases WHERE insolvency_hold_fla`
- Warnings: number not found in results: $1,414
