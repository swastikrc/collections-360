# Feature catalogue (ptp-features-1.0)

Generated from `c360/features/definitions.yaml` (`python -m c360.features describe`). Entity: one promise to pay, as of the end of the call in which it was made.

| Feature | Group | Source | Used by model | Meaning |
|---|---|---|---|---|
| `ptp_amount_log` | promise | structured | yes | log(1 + promised amount) |
| `ptp_cover_ratio` | promise | structured | yes | promised amount / overdue amount at the promise (capped at 3) |
| `ptp_full_cover` | promise | structured | yes | 1 if the promise clears the whole overdue amount (the cover ratio's effect is U-shaped: full and token payments are usually kept, part payments in between usually are not) |
| `ptp_token_payment` | promise | structured | yes | 1 if the promise is under 25% of the overdue amount (a small good-faith payment) |
| `days_to_due` | promise | structured | yes | days from the call to the promised date |
| `dpd_at_promise` | promise | structured | yes | days past due when the promise was made |
| `dpd_120_plus` | promise | structured | yes | 1 if 120 or more days past due (close to write-off at 181 days) |
| `promise_number_in_case` | promise | structured | yes | this promise's sequence number within the case |
| `prior_broken_in_case` | promise | structured | yes | promises already broken on this case (as recorded at the promise) |
| `self_serve_promise` | promise | structured | yes | 1 if made through web, app, IVR or SMS reply rather than with an agent |
| `instalment_promise` | promise | structured | yes | 1 if the promise is one instalment of several |
| `cust_prior_promises` | promise_history | structured | yes | the customer's resolved earlier promises (any case) |
| `cust_prior_kept_rate` | promise_history | structured | yes | share of those kept, smoothed: (kept + 1) / (resolved + 2) |
| `contacts_30d` | contacts | structured | yes | contacts in the 30 days before the call |
| `inbound_30d` | contacts | structured | yes | customer-initiated contacts in the 30 days before |
| `rpc_rate_90d` | contacts | structured | yes | share of 90-day contacts that reached the customer, smoothed |
| `hardship_flagged_90d` | contacts | structured | yes | 1 if hardship was flagged on any contact in the 90 days before |
| `dispute_flagged_90d` | contacts | structured | yes | 1 if a dispute was raised on any contact in the 90 days before |
| `has_salary_history` | income | structured | yes | 1 if salary credits are visible for the customer |
| `salary_gap_months_3m` | income | structured | yes | months with a salary gap among the last 3 completed months |
| `salary_change_pct` | income | structured | yes | last month's salary vs the 3 months before (-1..1) |
| `has_bureau` | bureau | structured | yes | 1 if a bureau pull exists on or before the call |
| `bureau_score` | bureau | structured | yes | latest bureau risk score (715, the median, if none) |
| `bureau_score_change` | bureau | structured | yes | change since the previous pull |
| `bureau_utilisation` | bureau | structured | yes | revolving utilisation at the latest pull (capped at 2) |
| `has_call_text` | call_text | text | yes | 1 if the promise call has a note or transcript |
| `txt_hedged` | call_text | text | yes | hedged commitment ('maybe', 'will try', 'not sure', 'refuses') |
| `txt_firm` | call_text | text | yes | firm commitment ('PTP $x', 'promised', 'can pay') |
| `txt_payday` | call_text | text | yes | payment tied to payday |
| `txt_hardship` | call_text | text | yes | hardship language (job loss, reduced hours, illness, separation, EI, disaster, rate reset, struggling) |
| `txt_hardship_ask` | call_text | text | yes | the customer asked about reducing the payment or hardship options |
| `txt_dispute` | call_text | text | yes | dispute language (already paid, fee wrong, fraud, billing error) |
| `txt_distress` | call_text | text | no (safeguard only) | distress or hostility on the call (crying, upset, hung up). Safeguard signal only: close to vulnerability, so it is shown to the agent and never used by a model |

Groups:

- **promise** (structured): The promise itself, as agreed on the call.
- **promise_history** (structured): The customer's earlier promises whose outcome was already known at the call (due date + grace days passed).
- **contacts** (structured): Contact history before the promise call (duplicates removed).
- **income** (structured): Salary credits into the customer's deposit accounts, completed months before the call.
- **bureau** (structured): Credit bureau pulls on or before the call date.
- **call_text** (text): The agent note and call transcript of the promise call (same contact_id), matched with the patterns above. Only flags are stored; no text leaves the serving database.
