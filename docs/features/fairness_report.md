# Fairness check: promise risk model

Generated 2026-10-04T16:07:35 by `python -m c360.models.fairness` (model ptp-risk-1.0). Measurement only: protected attributes come from MongoDB `restricted_protected_attributes`, are joined in memory to the out-of-time test predictions (2026-05-01 to 2026-09-01, promises the evaluation model never trained on), and are never stored with the features or used by the model.

Overall: 62,583 promises (62,575 matched to a protected-attribute record), not kept 60.8%, mean predicted 57.8%, high band 58.4%, AUC 0.888.

What a high score does: Layer 3 suggests a supportive step (a reminder call before the date, an affordability check, a smaller plan) for a person to approve. So the main fairness question is whether the score is equally *accurate* for every group (calibration, AUC, error rates), and the high-band share is shown for transparency.

Flags: share ratio vs the largest group outside 0.80-1.25; calibration gap more than 5 points from overall; AUC more than 0.05 below overall. Groups under 500 promises are shown but not flagged.

## Gender (`gender_code`)

| Group | Promises | Not kept | Predicted | Gap | High band | Ratio | AUC | High band when kept | Missed when not kept | Flags |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| M (reference) | 30,604 | 61.0% | 58.0% | -3.0% | 58.6% | 1.00 | 0.887 | 22.9% | 18.5% | none |
| F | 30,216 | 60.5% | 57.6% | -2.9% | 58.2% | 0.99 | 0.890 | 22.2% | 18.3% | none |
| X | 596 | 61.9% | 57.5% | -4.4% | 58.7% | 1.00 | 0.906 | 19.8% | 17.3% | none |
| not recorded | 589 | 61.1% | 57.7% | -3.4% | 58.9% | 1.00 | 0.899 | 20.1% | 16.4% | none |
| U | 578 | 61.1% | 57.5% | -3.6% | 58.3% | 0.99 | 0.861 | 24.4% | 20.1% | none |

## Marital status (`marital_status`)

| Group | Promises | Not kept | Predicted | Gap | High band | Ratio | AUC | High band when kept | Missed when not kept | Flags |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| married (reference) | 19,554 | 60.3% | 57.5% | -2.8% | 58.2% | 1.00 | 0.890 | 22.5% | 18.3% | none |
| single | 16,842 | 60.0% | 57.0% | -3.0% | 57.5% | 0.99 | 0.889 | 21.9% | 18.8% | none |
| common_law | 7,752 | 60.2% | 57.1% | -3.2% | 57.7% | 0.99 | 0.887 | 22.3% | 18.9% | none |
| separated | 5,226 | 66.4% | 63.3% | -3.1% | 64.7% | 1.11 | 0.877 | 27.6% | 16.6% | none |
| not recorded | 4,985 | 60.5% | 57.5% | -3.0% | 57.5% | 0.99 | 0.892 | 21.4% | 18.9% | none |
| divorced | 3,675 | 61.4% | 58.2% | -3.2% | 58.8% | 1.01 | 0.890 | 22.7% | 18.4% | none |
| undisclosed | 2,776 | 60.0% | 57.3% | -2.7% | 58.2% | 1.00 | 0.880 | 23.9% | 18.9% | none |
| widowed | 1,773 | 59.7% | 56.0% | -3.7% | 56.5% | 0.97 | 0.900 | 18.5% | 17.9% | none |

## Citizenship status (`citizenship_status`)

| Group | Promises | Not kept | Predicted | Gap | High band | Ratio | AUC | High band when kept | Missed when not kept | Flags |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| citizen (reference) | 47,440 | 61.3% | 58.3% | -3.0% | 59.0% | 1.00 | 0.887 | 23.0% | 18.3% | none |
| permanent_resident | 9,556 | 59.4% | 56.3% | -3.1% | 56.6% | 0.96 | 0.892 | 20.7% | 19.0% | none |
| work_permit | 2,790 | 59.9% | 56.5% | -3.4% | 57.6% | 0.98 | 0.889 | 21.1% | 17.9% | none |
| study_permit | 2,151 | 56.6% | 54.9% | -1.7% | 55.6% | 0.94 | 0.884 | 22.9% | 19.3% | none |
| not recorded | 646 | 58.2% | 55.9% | -2.3% | 56.3% | 0.96 | 0.896 | 19.6% | 17.3% | none |

## Newcomer programme (`newcomer_program_flag`)

| Group | Promises | Not kept | Predicted | Gap | High band | Ratio | AUC | High band when kept | Missed when not kept | Flags |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| False (reference) | 55,982 | 61.3% | 58.3% | -3.0% | 58.9% | 1.00 | 0.888 | 22.8% | 18.3% | none |
| True | 6,593 | 56.0% | 53.6% | -2.4% | 53.9% | 0.91 | 0.891 | 20.1% | 19.5% | none |
| not recorded | 8 | 75.0% | 71.2% | -3.8% | 87.5% | 1.48 | 1.000 | 50.0% | 0.0% | none |

## Accessibility needs (`accessibility_needs_flag`)

| Group | Promises | Not kept | Predicted | Gap | High band | Ratio | AUC | High band when kept | Missed when not kept | Flags |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| False (reference) | 60,801 | 60.8% | 57.8% | -3.0% | 58.5% | 1.00 | 0.888 | 22.6% | 18.4% | none |
| True | 1,774 | 60.4% | 56.9% | -3.6% | 56.5% | 0.97 | 0.899 | 20.1% | 19.7% | none |
| not recorded | 8 | 75.0% | 71.2% | -3.8% | 87.5% | 1.50 | 1.000 | 50.0% | 0.0% | none |

## Vulnerability recorded (`vulnerability_flag`)

| Group | Promises | Not kept | Predicted | Gap | High band | Ratio | AUC | High band when kept | Missed when not kept | Flags |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| False (reference) | 53,202 | 58.7% | 55.9% | -2.8% | 56.1% | 1.00 | 0.891 | 21.0% | 19.2% | none |
| True | 9,373 | 72.5% | 68.7% | -3.8% | 71.4% | 1.27 | 0.855 | 35.2% | 14.8% | high-band share ratio outside 0.80-1.25 |
| not recorded | 8 | 75.0% | 71.2% | -3.8% | 87.5% | 1.56 | 1.000 | 50.0% | 0.0% | none |

## Reading

- Vulnerability recorded = True: high-band share ratio outside 0.80-1.25. Actually not kept 72.5% vs 58.7% for False; calibration gap -3.8% vs -2.8%; AUC 0.855 vs 0.891; kept promises placed in the high band 35.2% vs 21.0%. A flag asks a person to look at why, not to use the attribute.
- Mitigation in place for vulnerability: a high score never leads to more pressure. Layer 3's next best action routes vulnerable customers to a certified agent with the vulnerable-customer script (POL-COLL-004 §4.5), puts hardship options before any request for payment, and every action needs a person's approval.
- Differences in the high-band share largely follow differences in the actual not-kept rate; the model has no access to the attributes, so any difference comes through correlated inputs (e.g. DPD, promise size vs overdue amount).
