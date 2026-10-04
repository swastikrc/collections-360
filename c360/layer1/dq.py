"""Data quality checks: declared once, run every build, written to golden.dq_results.

Each check counts the records it affects and states how the pipeline handles them.
`python -m c360.layer1.dq` runs the checks and regenerates docs/data_quality_report.md.
"""
import datetime as dt
import time

from c360 import config
from c360.layer1.golden import PROTECTED_COLUMNS

# (id, category, table, issue, affected_sql, total_sql, handling, severity)
CHECKS = [
    # ---- identity and keys --------------------------------------------------------------
    ("DQ-ID-01", "identity", "customers", "Same person held in two CRM records",
     "SELECT count(*) FROM golden.crm_person_map WHERE NOT is_master",
     "SELECT count(*) FROM curated.customers",
     "Clustered into one person (national-ID hash, then name/DOB/phone/email rules). Master = live, most complete, earliest record; all CRM ids kept on the C360.", "high"),
    ("DQ-ID-02", "identity", "customers", "CRM's own duplicate pointer (duplicate_of_crm_id) is incomplete",
     "SELECT (SELECT count(*) FROM golden.crm_person_map WHERE NOT is_master) - count(duplicate_of_crm_id) FROM curated.customers",
     "SELECT count(*) FROM golden.crm_person_map WHERE NOT is_master",
     "Not relied on. Used only as a labelled sample to measure dedup recall (see identity summary).", "medium"),
    ("DQ-ID-03", "identity", "customers", "Soft-deleted CRM records (is_deleted = true)",
     "SELECT count(*) FROM curated.customers WHERE is_deleted",
     "SELECT count(*) FROM curated.customers",
     "Kept for matching (accounts still reference them) but never chosen as the master record.", "low"),
    ("DQ-ID-04", "identity", "account_monthly_snapshot", "crm_customer_id that does not exist in CRM (one digit mistyped)",
     "SELECT count(*) FROM curated.account_monthly_snapshot WHERE crm_customer_id NOT IN (SELECT crm_customer_id FROM curated.customers)",
     "SELECT count(*) FROM curated.account_monthly_snapshot",
     "Repaired by the account's own majority vote, or the unique real id one edit away. Only used as matching evidence, never copied blindly.", "high"),
    ("DQ-ID-05", "identity", "collections_cases", "coll_customer_ref not in CRM (orphan case)",
     "SELECT count(*) FROM curated.collections_cases WHERE coll_customer_ref NOT IN (SELECT crm_customer_id FROM curated.customers)",
     "SELECT count(*) FROM curated.collections_cases",
     "Re-linked to the owner of the case's accounts, cross-checked against real CRM ids one digit away.", "high"),
    ("DQ-ID-06", "identity", "collections_cases", "coll_customer_ref is a real CRM id but the wrong person (typo landed on another customer)",
     "SELECT count(*) FROM golden.case_xref WHERE link_method = 'repaired_id_collision'",
     "SELECT count(*) FROM curated.collections_cases",
     "Detected because the case's accounts belong to a customer one digit away; linked to the account owner.", "high"),
    ("DQ-ID-07", "identity", "contact_history", "crm_customer_id missing or not in CRM",
     "SELECT count(*) FROM curated.contact_history WHERE crm_customer_id IS NULL OR crm_customer_id NOT IN (SELECT crm_customer_id FROM curated.customers)",
     "SELECT count(*) FROM curated.contact_history",
     "Contacts are linked through their case, so the customer id on the contact is not needed.", "medium"),
    ("DQ-ID-08", "identity", "contact_history", "case_id not present in collections_cases",
     "SELECT count(*) FROM curated.contact_history WHERE case_id NOT IN (SELECT case_id FROM curated.collections_cases)",
     "SELECT count(*) FROM curated.contact_history",
     "Fall back to the contact's own CRM id; the rest are left unlinked and counted here.", "medium"),
    ("DQ-ID-09", "identity", "promises_to_pay / agent_notes", "crm_customer_id not in CRM (inherited from orphan case)",
     "SELECT (SELECT count(*) FROM curated.promises_to_pay WHERE crm_customer_id NOT IN (SELECT crm_customer_id FROM curated.customers)) + (SELECT count(*) FROM curated.agent_notes WHERE crm_customer_id NOT IN (SELECT crm_customer_id FROM curated.customers))",
     "SELECT (SELECT count(*) FROM curated.promises_to_pay) + (SELECT count(*) FROM curated.agent_notes)",
     "Linked through the repaired case.", "medium"),
    ("DQ-ID-10", "identity", "deposit_accounts", "CIF lost its leading zeros (8-9 digits instead of 10)",
     "SELECT count(*) FROM curated.deposit_accounts WHERE dq_cif_leading_zeros_lost",
     "SELECT count(*) FROM curated.deposit_accounts",
     "Left-padded to 10 digits (cif column) so deposits join to bureau data and to each other.", "high"),
    ("DQ-ID-11", "identity", "deposit_accounts / loan_accounts", "Joint holder / co-borrower key with no identity data anywhere",
     "SELECT count(*) FROM golden.account_xref WHERE role IN ('joint', 'co_borrower') AND link_status = 'unresolved_key'",
     "SELECT count(*) FROM golden.account_xref WHERE role IN ('joint', 'co_borrower')",
     "Not guessed. Account is still attached to its primary holder; secondary party stays unresolved.", "medium"),
    ("DQ-ID-12", "identity", "cards / loans / core", "Source customer key not auto-linked (provisional or unmatched)",
     "SELECT count(*) FROM golden.source_customer_xref WHERE link_status NOT IN ('auto_link', 'human_confirmed')",
     "SELECT count(*) FROM golden.source_customer_xref",
     "Sent to golden.match_review_queue with top candidates and reasons. Review, don't delete.", "medium"),
    ("DQ-ID-13", "identity", "cards / loans / core", "Account history (bridge) names a different person than the identity data",
     "SELECT count(*) FROM golden.source_customer_xref WHERE bridge_conflict",
     "SELECT count(*) FROM golden.source_customer_xref",
     "Provisionally linked to the best overall candidate and routed to human review.", "medium"),
    # ---- formats ----------------------------------------------------------------------------
    ("DQ-FMT-01", "format", "customers", "dob_raw keyed in mixed formats (ISO, dd/mm vs mm/dd, 'Mon dd yyyy')",
     "SELECT count(*) FROM curated.customers WHERE dob_raw IS NOT NULL AND NOT regexp_matches(dob_raw, '^\\d{4}-\\d{2}-\\d{2}$')",
     "SELECT count(*) FROM curated.customers WHERE dob_raw IS NOT NULL",
     "The ISO date_of_birth column is used; matching also accepts day/month swaps as a weaker agreement level.", "medium"),
    ("DQ-FMT-02", "format", "account_monthly_snapshot", "month_end_date in three formats (yyyy-mm-dd, yyyymmdd, dd/mm/yyyy and mm/dd/yyyy)",
     "SELECT non_iso_values FROM curated._cast_failures WHERE table_name = 'account_monthly_snapshot' AND column_name = 'month_end_date'",
     "SELECT count(*) FROM curated.account_monthly_snapshot",
     "Parsed to DATE. Month-ends are day 30/31 so slashes are unambiguous; mm/dd rows would be lost by a dd/mm-only parser.", "high"),
    ("DQ-FMT-03", "format", "customers", "Phone numbers in six formats; some have only 9 digits",
     "SELECT count(*) FROM curated.customers WHERE primary_phone_raw IS NOT NULL AND primary_phone_std IS NULL",
     "SELECT count(*) FROM curated.customers WHERE primary_phone_raw IS NOT NULL",
     "Normalised to 10 digits; 9-digit numbers are treated as missing for matching.", "low"),
    ("DQ-FMT-04", "format", "card_accounts", "Cardholder phone masked (first 6 + last 2 digits only)",
     "SELECT count(*) FROM curated.card_accounts WHERE cardholder_phone_raw LIKE '%x%'",
     "SELECT count(*) FROM curated.card_accounts",
     "Compared on the visible digits as its own (weaker) agreement level.", "low"),
    ("DQ-FMT-05", "format", "customers", "Province written as 'Ont.', 'B.C.', 'Quebec', 'PQ' etc., or missing",
     "SELECT count(*) FROM curated.customers WHERE dq_province_nonstandard",
     "SELECT count(*) FROM curated.customers",
     "Standardised to 2-letter codes (province_code_std); missing values derived from the postal code.", "medium"),
    ("DQ-FMT-06", "format", "customers", "Postal code keyed without space or in lower case",
     "SELECT count(*) FROM curated.customers WHERE postal_code_raw IS NOT NULL AND postal_code_raw <> postal_code",
     "SELECT count(*) FROM curated.customers WHERE postal_code_raw IS NOT NULL",
     "Normalised to A1A1A1 for matching.", "low"),
    ("DQ-FMT-07", "format", "collections_cases", "account_ids sometimes a bare id instead of a JSON array",
     "SELECT count(*) FROM bronze.collections_cases WHERE account_ids IS NOT NULL AND NOT starts_with(trim(account_ids), '[')",
     "SELECT count(*) FROM bronze.collections_cases",
     "Both forms parsed into one list column.", "medium"),
    ("DQ-FMT-08", "format", "card / loan / deposit / bureau", "Names stored per system ('L. GARCIA', 'HUANG/JASON', 'ST-LAURENT PHILIPPE', 'WANG T J', 'YANG, WAYNE JACK')",
     "SELECT (SELECT count(*) FROM golden._src_profile_cards WHERE given IS NULL)",
     "SELECT count(*) FROM golden._src_profile_cards",
     "Parsed per system; surname/given compared in both orders. Count = card keys holding only an initial.", "low"),
    # ---- schema drift ---------------------------------------------------------------------
    ("DQ-SCH-01", "schema_drift", "contact_history", "channel renamed to channel_v2 from 26 Sep 2026 (REL-0926)",
     "SELECT count(*) FROM curated.contact_history WHERE dq_channel_renamed",
     "SELECT count(*) FROM curated.contact_history",
     "channel_std = coalesce(channel_v2, channel).", "high"),
    # ---- values -------------------------------------------------------------------------------
    ("DQ-VAL-01", "values", "card_accounts", "dpd missing",
     "SELECT count(*) FROM curated.card_accounts WHERE dq_dpd_missing", "SELECT count(*) FROM curated.card_accounts",
     "Left null (not imputed); excluded from delinquency metrics; flagged on the account in the C360.", "high"),
    ("DQ-VAL-02", "values", "loan_accounts", "dpd missing",
     "SELECT count(*) FROM curated.loan_accounts WHERE dq_dpd_missing", "SELECT count(*) FROM curated.loan_accounts",
     "Left null; flagged.", "medium"),
    ("DQ-VAL-03", "values", "account_monthly_snapshot", "dpd missing",
     "SELECT count(*) FROM curated.account_monthly_snapshot WHERE dpd IS NULL", "SELECT count(*) FROM curated.account_monthly_snapshot",
     "Left null; excluded from roll-rate calculations.", "medium"),
    ("DQ-VAL-04", "values", "collections_cases", "current_bucket does not match current_dpd",
     "SELECT count(*) FROM curated.collections_cases WHERE dq_bucket_mismatch", "SELECT count(*) FROM curated.collections_cases",
     "current_bucket_recomputed derived from current_dpd; the C360 shows the recomputed bucket and keeps the stored one.", "high"),
    ("DQ-VAL-05", "values", "card_accounts", "dpd_bucket does not match dpd",
     "SELECT count(*) FROM curated.card_accounts WHERE dq_bucket_mismatch", "SELECT count(*) FROM curated.card_accounts",
     "dpd_bucket_recomputed.", "medium"),
    ("DQ-VAL-06", "values", "loan_accounts", "dpd_bucket does not match dpd",
     "SELECT count(*) FROM curated.loan_accounts WHERE dq_bucket_mismatch", "SELECT count(*) FROM curated.loan_accounts",
     "dpd_bucket_recomputed.", "medium"),
    ("DQ-VAL-07", "values", "account_monthly_snapshot", "dpd_bucket does not match dpd",
     "SELECT count(*) FROM curated.account_monthly_snapshot WHERE dq_bucket_mismatch", "SELECT count(*) FROM curated.account_monthly_snapshot",
     "dpd_bucket_recomputed.", "medium"),
    ("DQ-VAL-08", "values", "external", "Shifted column from bureau load incident DFI-0812 (inquiries_hard_3m > inquiries_hard_6m)",
     "SELECT count(*) FROM curated.external WHERE dq_shifted_row", "SELECT count(*) FROM curated.external",
     "inquiries_hard_3m nulled in inquiries_hard_3m_std; identity fields unaffected; row flagged on the C360.", "medium"),
    ("DQ-VAL-09", "values", "card / loan / deposit accounts", "Account row is an old snapshot (snapshot_date before 28 Sep 2026)",
     "SELECT (SELECT count(*) FROM curated.card_accounts WHERE dq_stale_snapshot) + (SELECT count(*) FROM curated.loan_accounts WHERE dq_stale_snapshot) + (SELECT count(*) FROM curated.deposit_accounts WHERE dq_stale_snapshot)",
     "SELECT (SELECT count(*) FROM curated.card_accounts) + (SELECT count(*) FROM curated.loan_accounts) + (SELECT count(*) FROM curated.deposit_accounts)",
     "Kept and flagged stale_snapshot on the account; current-state metrics filter on snapshot_date.", "medium"),
    ("DQ-VAL-10", "values", "customers", "Date of birth missing",
     "SELECT count(*) FROM curated.customers WHERE date_of_birth IS NULL", "SELECT count(*) FROM curated.customers",
     "Matching relies on the other fields (DOB counts as missing, not as disagreement).", "low"),
    ("DQ-VAL-11", "values", "external", "Bureau no-hit / thin file without reported identity",
     "SELECT count(*) FROM curated.external WHERE subject_name_reported IS NULL", "SELECT count(*) FROM curated.external",
     "CIF matched from deposit data where it exists; otherwise left unmatched in the review queue.", "low"),
    # ---- duplicates -------------------------------------------------------------------------
    ("DQ-DUP-01", "duplicates", "contact_history", "Same contact loaded twice under a second contact_id",
     "SELECT count(*) FROM curated.contact_history WHERE dq_duplicate_of IS NOT NULL", "SELECT count(*) FROM curated.contact_history",
     "Flagged (dq_duplicate_of) not deleted, so raw-based metrics still reconcile; C360 contact counts exclude them.", "high"),
    ("DQ-DUP-02", "duplicates", "model_scores", "Same entity, model and timestamp scored twice with different values",
     "SELECT count(*) FROM curated.model_scores WHERE dq_superseded_duplicate", "SELECT count(*) FROM curated.model_scores",
     "Latest score_id kept in the C360; others flagged dq_superseded_duplicate.", "medium"),
    ("DQ-DUP-03", "duplicates", "transactions", "Possible duplicate transactions (same account, time, amount, description)",
     "SELECT count(*) FROM curated.transactions WHERE dq_possible_duplicate", "SELECT count(*) FROM curated.transactions",
     "Flagged only: mostly same-day loan payments and benefits, which can be legitimate.", "low"),
    # ---- governance --------------------------------------------------------------------------
    ("DQ-GOV-01", "governance", "mongo: collections360.customers", "Protected attribute present in the customer document",
     lambda con: _protected_keys_in_mongo()[0], lambda con: _protected_keys_in_mongo()[1],
     "Must be 0. Protected attributes live only in collections360.restricted_protected_attributes (fairness testing).", "high"),
    # ---- operational events (expected data signatures) ---------------------------------------
    ("DQ-OPS-01", "operational", "contact_history", "Late-fee disputes spike after app outage INC-0914 (15-22 Sep 2026)",
     "SELECT count(*) FROM curated.contact_history WHERE dispute_raised_flag AND contact_ts_local >= TIMESTAMP '2026-09-15' AND contact_ts_local < TIMESTAMP '2026-09-23'",
     "SELECT count(*) FROM curated.contact_history WHERE dispute_raised_flag AND contact_ts_local >= TIMESTAMP '2026-09-07' AND contact_ts_local < TIMESTAMP '2026-09-23'",
     "Not a data error: annotated so models and agents don't read it as customer behaviour.", "info"),
    ("DQ-OPS-02", "operational", "contact_history", "Outbound call volume drop during dialer outage INC-0923 (23-24 Sep 2026)",
     "SELECT count(*) FROM curated.contact_history WHERE channel_std = 'call' AND direction = 'outbound' AND contact_ts_local >= TIMESTAMP '2026-09-23' AND contact_ts_local < TIMESTAMP '2026-09-25'",
     "SELECT count(*) FROM curated.contact_history WHERE channel_std = 'call' AND direction = 'outbound' AND contact_ts_local >= TIMESTAMP '2026-09-16' AND contact_ts_local < TIMESTAMP '2026-09-18'",
     "Not a data error. 'total' here is the same two weekdays one week earlier, for comparison.", "info"),
]


def _all_keys(doc, prefix=""):
    if isinstance(doc, dict):
        for k, v in doc.items():
            yield k
            yield from _all_keys(v, prefix + k + ".")
    elif isinstance(doc, list):
        for v in doc:
            yield from _all_keys(v, prefix)


_GOV_CACHE = {}


def _protected_keys_in_mongo(sample=5000):
    """(protected field names found, distinct field names scanned) over a random sample of documents."""
    if "r" not in _GOV_CACHE:
        from c360 import mongo
        keys = set()
        for doc in mongo.db().customers.aggregate([{"$sample": {"size": sample}}]):
            keys.update(_all_keys(doc))
        _GOV_CACHE["r"] = (len(keys & set(PROTECTED_COLUMNS)), len(keys))
    return _GOV_CACHE["r"]


def _count(con, q):
    return q(con) if callable(q) else con.execute(q).fetchone()[0]


def run_checks(con):
    con.execute(f"ATTACH IF NOT EXISTS '{config.BRONZE_DB}' AS bronze (READ_ONLY)")
    con.execute("""CREATE TABLE IF NOT EXISTS golden.dq_results(
        run_ts TIMESTAMP, check_id VARCHAR, category VARCHAR, table_name VARCHAR, issue VARCHAR,
        records_affected BIGINT, records_total BIGINT, pct DOUBLE, handling VARCHAR, severity VARCHAR)""")
    run_ts = dt.datetime.now()
    rows = []
    for cid, cat, tbl, issue, a_sql, t_sql, handling, sev in CHECKS:
        a = _count(con, a_sql) or 0
        t = _count(con, t_sql) or 0
        rows.append((run_ts, cid, cat, tbl, issue, a, t, round(100.0 * a / t, 3) if t else None, handling, sev))
    con.executemany("INSERT INTO golden.dq_results VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    from c360 import mongo
    keys = ["run_ts", "check_id", "category", "table_name", "issue", "records_affected", "records_total", "pct", "handling", "severity"]
    mongo.db().dq_results.insert_many([dict(zip(keys, r)) for r in rows])
    return run_ts


def identity_summary(con):
    q = lambda s: con.execute(s).fetchall()
    s = {}
    s["people"] = q("SELECT count(DISTINCT c360_id) FROM golden.crm_person_map")[0][0]
    s["crm_records"] = q("SELECT count(*) FROM golden.crm_person_map")[0][0]
    s["dedup_recall"] = q("""SELECT count(*), sum((a.c360_id = b.c360_id)::INT) FROM curated.customers c
        JOIN golden.crm_person_map a ON a.crm_customer_id = c.crm_customer_id
        JOIN golden.crm_person_map b ON b.crm_customer_id = c.duplicate_of_crm_id""")[0]
    s["links"] = q("""SELECT src_system, count(*), count(*) FILTER (WHERE link_status = 'auto_link'),
        count(*) FILTER (WHERE link_status = 'provisional'), count(*) FILTER (WHERE link_status = 'unmatched'),
        count(*) FILTER (WHERE link_status IN ('human_confirmed', 'llm_link')), avg(match_confidence) FILTER (WHERE c360_id IS NOT NULL)
        FROM golden.source_customer_xref GROUP BY 1 ORDER BY 1""")
    s["holdout"] = []
    for sysname in ["cards", "loans", "core"]:
        r = q(f"""WITH pii AS (
              SELECT c.src_ref, arg_max(pm.c360_id, c.match_weight - c.bf_bridge) AS pii_best
              FROM golden._scored_{sysname} c JOIN golden.crm_person_map pm ON pm.crm_customer_id = c.crm GROUP BY 1),
            br AS (SELECT b.src_ref, arg_max(pm.c360_id, b.votes) AS br_best FROM golden._bridge_key b
                   JOIN golden.crm_person_map pm ON pm.crm_customer_id = b.crm_customer_id WHERE b.src_system = '{sysname}' GROUP BY 1)
            SELECT count(*), sum((pii_best = br_best)::INT) FROM pii JOIN br USING (src_ref)""")[0]
        s["holdout"].append((sysname, r[0], r[1]))
    s["cases"] = q("SELECT link_method, count(*), avg(match_confidence) FROM golden.case_xref GROUP BY 1 ORDER BY 2 DESC")
    s["bridge"] = q("SELECT id_status, count(*) FROM golden._bridge GROUP BY 1 ORDER BY 2 DESC")
    s["queue"] = q("SELECT src_system, review_reason, count(*) FROM golden.match_review_queue GROUP BY 1, 2 ORDER BY 1, 3 DESC")
    s["weights"] = q("""SELECT field, meaning, src_system, round(log2_bf, 1) FROM golden.match_model_weights
        WHERE src_system = 'cards' ORDER BY field, level DESC""")
    return s


def _fmt(n):
    return f"{n:,}" if isinstance(n, int) else n


def write_report(con, run_ts, path=None):
    path = path or config.ROOT / "docs" / "data_quality_report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    res = con.execute("SELECT * FROM golden.dq_results WHERE run_ts = ? ORDER BY check_id", [run_ts]).fetchall()
    cast = con.execute("SELECT table_name, column_name, declared_type, rows_non_null, cast_failures, non_iso_values FROM curated._cast_failures WHERE cast_failures > 0 OR non_iso_values > 0").fetchall()
    s = identity_summary(con)
    L = []
    L.append("# Data quality report: Collections 360 (Layer 1)\n")
    L.append(f"Generated by `python -m c360.layer1.dq` on {run_ts:%Y-%m-%d %H:%M}. Source snapshot 2026-09-28. "
             "Every number below is computed from the data at build time; nothing is typed by hand.\n")
    L.append("## How issues are handled\n")
    L.append("- **Raw zone** keeps every file exactly as delivered (all text), so nothing is lost on load.\n"
             "- **Curated zone** keeps every row and every original column. Fixes are added as new columns "
             "(`*_std`, `*_recomputed`, `dq_*` flags), so both the stored value and the corrected value are visible.\n"
             "- **Golden zone (C360, MongoDB)** uses the corrected values, and lists each customer's data quality notes on their document.\n"
             "- Nothing is deleted. Records that can't be linked confidently go to a human review queue.\n")
    L.append("## Issues found\n")
    L.append("| ID | Table | Issue | Records affected | Of | % | Handling |")
    L.append("|---|---|---|---:|---:|---:|---|")
    for r in res:
        _, cid, cat, tbl, issue, a, t, pct, handling, sev = r
        L.append(f"| {cid} | {tbl} | {issue} | {_fmt(a)} | {_fmt(t)} | {pct if pct is not None else ''} | {handling} |")
    if cast:
        L.append("\n## Type conversion (raw text to typed columns)\n")
        L.append("| Table | Column | Declared type | Non-null values | Failed to convert | Not in ISO format |")
        L.append("|---|---|---|---:|---:|---:|")
        for t, c, d, nn, bad, noniso in cast:
            L.append(f"| {t} | {c} | {d} | {_fmt(nn)} | {_fmt(bad)} | {_fmt(noniso)} |")
    L.append("\n## Identity resolution summary\n")
    tot, ok = s["dedup_recall"]
    L.append(f"- **People:** {_fmt(s['people'])} unique customers from {_fmt(s['crm_records'])} CRM records "
             f"({_fmt(s['crm_records'] - s['people'])} duplicate records merged).")
    L.append(f"- **Dedup recall on CRM's own labelled duplicates:** {ok:,} of {tot:,} pairs merged ({100 * ok / tot:.2f}%).")
    L.append("\n| Source system | Keys | Auto-linked | Provisional (review) | Unmatched (review) | Human / LLM | Mean confidence |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for sysname, n, auto, prov, unm, hum, conf in s["links"]:
        L.append(f"| {sysname} | {n:,} | {auto:,} ({100 * auto / n:.2f}%) | {prov:,} | {unm:,} | {hum:,} | {conf:.4f} |")
    L.append("\n**Independent check.** For keys that have account-history evidence, the person chosen from identity "
             "data alone (name, DOB, phone, postal, with the bridge removed) is compared with the person the account history names:\n")
    L.append("| Source system | Keys with account history | Identity data agrees | Agreement |")
    L.append("|---|---:|---:|---:|")
    for sysname, n, ok in s["holdout"]:
        L.append(f"| {sysname} | {n:,} | {ok:,} | {100 * ok / n:.3f}% |")
    L.append("\n**Collections cases:**\n")
    L.append("| Link method | Cases | Mean confidence |")
    L.append("|---|---:|---:|")
    for m, n, c in s["cases"]:
        L.append(f"| {m} | {n:,} | {c:.3f} |")
    L.append("\n**Account-history bridge ids:**\n")
    L.append("| Status | Account-id pairs |")
    L.append("|---|---:|")
    for st, n in s["bridge"]:
        L.append(f"| {st} | {n:,} |")
    L.append("\n**Human review queue** (MongoDB `collections360.match_review_queue`):\n")
    L.append("| System | Reason | Items |")
    L.append("|---|---|---:|")
    for sysname, reason, n in s["queue"]:
        L.append(f"| {sysname} | {reason} | {n:,} |")
    L.append("\n**What each piece of evidence is worth** (cards model, log2 Bayes factor; positive = supports a match):\n")
    L.append("| Field | Level | Weight |")
    L.append("|---|---|---:|")
    for f, meaning, _, w in s["weights"]:
        L.append(f"| {f} | {meaning} | {w:+.1f} |")
    path.write_text("\n".join(L) + "\n")
    return path


def run():
    con = config.connect()
    t0 = time.time()
    run_ts = run_checks(con)
    p = write_report(con, run_ts)
    print(f"  data quality: {len(CHECKS)} checks -> {p} ({time.time() - t0:.1f}s)", flush=True)
    con.close()


if __name__ == "__main__":
    run()
