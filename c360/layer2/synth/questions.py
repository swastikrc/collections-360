"""The synthetic evaluation questions, with expected answers from truth.py.

Each question names a truth function with explicit dates. `plan` is the tool call a correct agent
would make; it is used only by the offline fake LLM (`eval --fake`) to prove the pipeline end to
end. The live agent never sees `plan`, `truth` or `expected`.
"""
from pathlib import Path

import yaml

from c360.layer2 import config as l2config
from c360.layer2.synth.truth import Truth

RATIO_TOL = 0.005      # absolute, for ratios
REL_TOL = 0.005        # relative, for counts, amounts and days

# Explicit calendar windows (snapshot date 2026-09-28). "last N days" = the N days ending on the
# snapshot date, inclusive.
P = {
    "August 2026": ("2026-08-01", "2026-08-31"),
    "July 2026": ("2026-07-01", "2026-07-31"),
    "June 2026": ("2026-06-01", "2026-06-30"),
    "May 2026": ("2026-05-01", "2026-05-31"),
    "last 90 days": ("2026-07-01", "2026-09-28"),
    "last 30 days": ("2026-08-30", "2026-09-28"),
    "last 7 days": ("2026-09-22", "2026-09-28"),
    "this quarter": ("2026-07-01", "2026-09-28"),
    "last quarter": ("2026-04-01", "2026-06-30"),
    "last month": ("2026-08-01", "2026-08-31"),
    "year to date": ("2026-01-01", "2026-09-28"),
    "last 6 months": ("2026-03-29", "2026-09-28"),
    "last 12 months": ("2025-09-29", "2026-09-28"),
    "2026-01-01 to 2026-06-30": ("2026-01-01", "2026-06-30"),
    "all time": ("1900-01-01", "2999-12-31"),
}

UNITS = {"promise_kept_rate": "ratio", "promise_broken_rate": "ratio", "broken_promise_rate": "ratio",
         "ptp_rate": "ratio", "hardship_rate": "ratio", "delinquency_rate": "ratio", "write_off_rate": "ratio",
         "contact_attempts_per_case": "ratio", "payroll_delay_share": "ratio", "cost_per_contact": "amount",
         "cost_per_rpc": "amount", "collected_amount": "amount", "write_off_amount": "amount", "write_off_count": "count", "cure_rate": "ratio", "roll_rate": "ratio",
         "rpc_rate": "ratio", "hardship_offer_acceptance_rate": "ratio", "promise_count": "count",
         "contact_attempts": "count", "open_case_count": "count", "payment_on_contact_amount": "amount",
         "past_due_amount": "amount", "average_dpd": "days"}


def metric_q(qid, question, metric, period, by=None, where=None, top=None, tags=()):
    """A question answered by one governed metric."""
    start, end = P[period]
    args = {"start": start, "end": end}
    if by:
        args["by"] = by
    if where:
        args["where"] = where
    plan = {"metric": metric, "period": period}
    if by:
        plan["group_by"] = [by]
    if where:
        plan["filters"] = [{"dimension": k, "op": "=", "value": str(v)} for k, v in where.items()]
    return dict(id=qid, question=question, truth={"fn": metric, "args": args}, unit=UNITS[metric],
                kind="table" if by else "scalar", key=by, top=top, tags=["governed", *tags],
                fake_plan=[{"tool": "compute_metric", "args": plan}])


def sql_q(qid, question, fn, sql, args=None, unit="count", kind="scalar", key=None, purpose="analysis", tags=()):
    """A question no governed metric covers: answered with custom SQL."""
    return dict(id=qid, question=question, truth={"fn": fn, "args": args or {}}, unit=unit, kind=kind, key=key,
                tags=["custom_sql", *tags], fake_plan=[{"tool": "run_sql", "args": {"query": sql, "purpose": purpose}}])


SYNTHETIC = [
    metric_q("S01", "What is the promise-kept rate for promises due in August 2026?", "promise_kept_rate", "August 2026", tags=["promises"]),
    metric_q("S02", "Show the promise-kept rate by hardship reason for promises due in the last 90 days.", "promise_kept_rate", "last 90 days", by="hardship_reason", tags=["promises"]),
    metric_q("S03", "What is the broken-promise rate by queue this quarter?", "broken_promise_rate", "this quarter", by="queue", tags=["promises"]),
    metric_q("S04", "How many promises to pay were made in July 2026?", "promise_count", "July 2026", tags=["promises"]),
    metric_q("S05", "How many promises to pay were made last quarter, by channel?", "promise_count", "last quarter", by="channel", tags=["promises"]),
    metric_q("S06", "What was the cure rate in August 2026?", "cure_rate", "August 2026", tags=["cases"]),
    metric_q("S07", "What was the cure rate by product in July 2026?", "cure_rate", "July 2026", by="product", tags=["cases"]),
    metric_q("S08", "What was the roll rate out of the 1-30 bucket in June 2026?", "roll_rate", "June 2026", where={"from_bucket": "1-30"}, tags=["roll"]),
    metric_q("S09", "Show the roll rate by starting bucket for July 2026.", "roll_rate", "July 2026", by="from_bucket", tags=["roll"]),
    metric_q("S10", "Compare the roll rate for cards and loans over the last 6 months.", "roll_rate", "last 6 months", by="product_family", tags=["roll"]),
    metric_q("S11", "What is the right-party contact rate over the last 30 days?", "rpc_rate", "last 30 days", tags=["contacts"]),
    metric_q("S12", "What was the right-party contact rate by queue in August 2026?", "rpc_rate", "August 2026", by="queue", tags=["contacts"]),
    metric_q("S13", "How many outbound contact attempts were made in the last 7 days?", "contact_attempts", "last 7 days", tags=["contacts"]),
    metric_q("S14", "How many outbound contact attempts were made last month, by queue?", "contact_attempts", "last month", by="queue", tags=["contacts"]),
    metric_q("S15", "What is the total amount of payments taken on contacts in August 2026?", "payment_on_contact_amount", "August 2026", tags=["contacts"]),
    metric_q("S16", "How much was collected in payments taken on contacts this quarter, by channel?", "payment_on_contact_amount", "this quarter", by="channel", tags=["contacts"]),
    metric_q("S17", "What is the hardship offer acceptance rate year to date?", "hardship_offer_acceptance_rate", "year to date", tags=["offers"]),
    metric_q("S18", "What is the hardship offer acceptance rate by programme year to date?", "hardship_offer_acceptance_rate", "year to date", by="program", tags=["offers"]),
    metric_q("S19", "How many collections cases are open right now?", "open_case_count", "all time", tags=["cases"]),
    metric_q("S20", "How many open collections cases are there in each queue?", "open_case_count", "all time", by="queue", tags=["cases"]),
    metric_q("S21", "What is the total past-due amount on open collections cases?", "past_due_amount", "all time", tags=["cases"]),
    metric_q("S22", "What is the past-due amount on open cases, split by product?", "past_due_amount", "all time", by="product", tags=["cases"]),
    metric_q("S23", "What is the average days past due of open collections cases?", "average_dpd", "all time", tags=["cases"]),
    metric_q("S24", "What is the average days past due of open cases in each queue?", "average_dpd", "all time", by="queue", tags=["cases"]),
    metric_q("S25", "Which hardship reason has the best promise-kept rate for promises due in the last 90 days, and what is that rate?", "promise_kept_rate", "last 90 days", by="hardship_reason", top="max", tags=["promises", "ranking"]),
    metric_q("S26", "Which collections queue had the highest right-party contact rate last month, and what was it?", "rpc_rate", "last month", by="queue", top="max", tags=["contacts", "ranking"]),
    sql_q("S27", "How many customers are flagged as being in financial hardship?", "customers_with_hardship_flag",
          "SELECT count(*) AS customers FROM l2.customer_summary WHERE financial_hardship_flag"),
    sql_q("S28", "How many card accounts are in the 31-60 delinquency bucket?", "cards_in_stored_bucket",
          "SELECT count(*) AS card_accounts FROM l2.card_accounts WHERE dpd_bucket = '31-60'", args={"bucket": "31-60"}),
    sql_q("S29", "Data quality: how many card accounts have a stored delinquency bucket that disagrees with the bucket recomputed from days past due?",
          "card_bucket_mismatches", "SELECT count(*) AS mismatches FROM l2.card_accounts WHERE dq_bucket_mismatch", purpose="data_quality", tags=["data_quality"]),
    sql_q("S30", "Data quality: how many contact records are flagged as double-loaded duplicates?", "duplicate_contacts",
          "SELECT count(*) AS duplicates FROM l2.contact_history WHERE is_duplicate", purpose="data_quality", tags=["data_quality"]),
    sql_q("S31", "How many loans have a co-borrower?", "loans_with_co_borrower",
          "SELECT count(*) AS loans FROM l2.loan_accounts WHERE has_co_borrower"),
    sql_q("S32", "What is the average bureau risk score across customers with a bureau file?", "avg_bureau_risk_score",
          "SELECT avg(risk_score) AS avg_risk_score FROM l2.bureau_latest", unit="amount"),
    sql_q("S33", "How many data quality checks were recorded in the latest Layer 1 run?", "dq_check_count",
          "SELECT count(*) AS checks FROM l2.dq_results", tags=["data_quality"]),
    sql_q("S34", "How many customers are there in each segment?", "customers_by_segment",
          "SELECT segment, count(*) AS customers FROM l2.customers GROUP BY segment ORDER BY customers DESC", kind="table", key="segment"),
]


def _customer_questions(truth):
    case = truth.sample_case("open_hardship")
    return [
        dict(id="S35", question=f"Which queue is collections case {case['case_id']} in, and what hardship reason is recorded on it?",
             kind="contains", value=[case["queue"], case["hardship_reason"]], tags=["customer"],
             fake_plan=[{"tool": "get_customer", "args": {"key": case["case_id"]}}]),
        dict(id="S36", question=f"How many open collections cases does customer {case['c360_id']} have?",
             truth={"fn": "customer_open_cases", "args": {"c360_id": case["c360_id"]}}, unit="count", kind="scalar",
             tags=["customer"], fake_plan=[{"tool": "get_customer", "args": {"key": case["c360_id"]}}]),
        dict(id="S37", question="What are customers saying in agent notes about losing their job?",
             kind="contains", value=["job"], tags=["text"],
             fake_plan=[{"tool": "search_text", "args": {"query": "lost my job", "source": "notes"}}]),
        dict(id="S38", question="What reasons do customers give on calls for needing a smaller payment?",
             kind="manual", tags=["text"],
             fake_plan=[{"tool": "search_text", "args": {"query": "smaller payment reduced hours sick", "source": "both"}}]),
    ]


def expected_for(truth, q):
    """Turn a question definition into the `expected` block of a question file."""
    kind = q["kind"]
    if kind in ("manual", "refusal"):
        return {"kind": kind}
    if kind == "contains":
        return {"kind": "contains", "value": [str(v) for v in q["value"]]}
    value = truth.resolve(q["truth"]["fn"], q["truth"].get("args"))
    unit = q.get("unit", "count")
    tol = {"tolerance": RATIO_TOL} if unit == "ratio" else {"relative_tolerance": REL_TOL}
    if kind == "scalar":
        return {"kind": "scalar", "value": value, "unit": unit, **tol}
    key = q["key"]
    rows = [{key: label, "value": v} for label, v in value.items() if v is not None]
    extra = {}
    if q.get("top"):
        best = (max if q["top"] == "max" else min)(r["value"] for r in rows)
        rows = [r for r in rows if r["value"] == best]
        if len(rows) > 1:
            extra["match"] = "any"      # a tie: naming any of the tied labels is correct
    return {"kind": "table", "key": [key], "rows": rows, "unit": unit, **tol, **extra}


def build(truth):
    out = []
    for q in SYNTHETIC + _customer_questions(truth):
        entry = {"id": q["id"], "question": q["question"], "expected": expected_for(truth, q), "tags": list(q["tags"])}
        if q.get("truth"):
            entry["truth"] = q["truth"]
        entry["fake_plan"] = q["fake_plan"]
        out.append(entry)
    return out


HEADER = """# GENERATED by `python -m c360.layer2.synth` -- do not edit by hand.
# Synthetic questions with expected answers computed independently by synth/truth.py (pandas over
# Layer 1's curated tables). `fake_plan` is used only by the offline fake LLM (`eval --fake`).
"""


def write_all(db_path, jsonl_path=None, out_path=None):
    truth = Truth.from_db(db_path, jsonl_path)
    out_path = Path(out_path) if out_path else l2config.QUESTIONS_DIR / "synthetic.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(HEADER + yaml.safe_dump(build(truth), sort_keys=False, width=110, allow_unicode=True))
    return out_path
