# Layer 2 performance check (runbook Step 8)

Serving build 20261004T100107; each governed metric run 3 times through `compute_metric` (validator + execution); median wall time.

Before: `roll_rate` took 10–11 s because the insolvency-hold exclusion was a correlated `NOT EXISTS … list_contains(account_ids, …)` over every snapshot row. Rewritten as a one-off set of held account ids (`held` CTE + `NOT IN`): identical result (Q2 2026: 0.055255, same as the hand-written SQL), now well under a second. No pre-aggregated tables needed.

| Metric | Group by | Rows | Median (s) |
|---|---|---:|---:|
| roll_rate | from_bucket | 8 | 0.094 |
| contact_attempts_per_case | channel | 5 | 0.085 |
| roll_rate | - | 1 | 0.084 |
| rpc_rate | - | 1 | 0.078 |
| rpc_rate | queue | 7 | 0.077 |
| cost_per_rpc | - | 1 | 0.074 |
| payroll_delay_share | with_open_case | 2 | 0.073 |
| cost_per_rpc | channel | 7 | 0.071 |
| contact_attempts_per_case | - | 1 | 0.067 |
| cost_per_contact | channel | 7 | 0.058 |
| payroll_delay_share | - | 1 | 0.056 |
| contact_attempts | channel | 5 | 0.056 |
| contact_attempts | - | 1 | 0.055 |
| cost_per_contact | - | 1 | 0.049 |
| ptp_rate | channel | 2 | 0.049 |
| ptp_rate | - | 1 | 0.045 |
| broken_promise_rate | - | 1 | 0.045 |
| promise_kept_rate | product | 6 | 0.044 |
| broken_promise_rate | product | 6 | 0.044 |
| promise_kept_rate | - | 1 | 0.041 |
| promise_count | status | 5 | 0.041 |
| payment_on_contact_amount | - | 1 | 0.040 |
| payment_on_contact_amount | channel | 3 | 0.038 |
| promise_count | - | 1 | 0.033 |
| collected_amount | - | 1 | 0.026 |
| hardship_offer_acceptance_rate | program | 10 | 0.025 |
| collected_amount | product | 6 | 0.024 |
| hardship_offer_acceptance_rate | - | 1 | 0.020 |
| delinquency_rate | product_family | 2 | 0.013 |
| cure_rate | product | 6 | 0.012 |
| cure_rate | - | 1 | 0.011 |
| delinquency_rate | - | 1 | 0.011 |
| write_off_rate | product_family | 2 | 0.010 |
| write_off_rate | - | 1 | 0.009 |
| hardship_rate | product | 6 | 0.008 |
| write_off_amount | product_family | 2 | 0.008 |
| average_dpd | product | 6 | 0.008 |
| write_off_count | product_family | 2 | 0.008 |
| write_off_amount | - | 1 | 0.007 |
| past_due_amount | product | 6 | 0.007 |
| open_case_count | product | 6 | 0.006 |
| average_dpd | - | 1 | 0.006 |
| write_off_count | - | 1 | 0.006 |
| hardship_rate | - | 1 | 0.005 |
| past_due_amount | - | 1 | 0.005 |
| open_case_count | - | 1 | 0.004 |

Slowest: roll_rate (0.09 s). All 46 runs together: 1.6 s. SQL timeout: 30 s.
