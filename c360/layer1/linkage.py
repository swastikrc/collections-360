"""Final link decisions and crosswalks.

Precedence for every source key:
  1. A person's review decision (golden.match_review_decisions) always wins.
  2. Auto-link: confident, clearly ahead of the runner-up, and not contradicted by the bridge.
  3. LLM adjudication of the review queue (only if it was run; see llm_adjudicator.py).
  4. Provisional link to the best candidate, flagged for review.
  5. Unmatched: kept in the review queue with its candidates, never deleted.
"""
import time

from c360 import config

SYSTEM_LABEL = {"cards": "Cards (card-system customer number)", "loans": "Lending (borrower number)",
                "core": "Core banking (CIF)", "collections": "Collections (CRM id on case)", "crm": "CRM"}


def ensure_review_tables(con):
    con.execute("""CREATE TABLE IF NOT EXISTS golden.match_review_decisions(
        src_system VARCHAR, src_ref VARCHAR, decision VARCHAR,  -- 'accept' | 'reject' | 'relink'
        c360_id VARCHAR, reviewer VARCHAR, decided_ts TIMESTAMP, note VARCHAR)""")
    con.execute("""CREATE TABLE IF NOT EXISTS golden.match_llm_verdicts(
        src_system VARCHAR, src_ref VARCHAR, c360_id VARCHAR, llm_match_pct DOUBLE,
        llm_reason VARCHAR, model VARCHAR, verdict_ts TIMESTAMP)""")


def sync_review_inputs(con):
    """Reviewer decisions and LLM verdicts live in MongoDB; copy them in before deciding links."""
    import pandas as pd
    from c360 import mongo
    d = mongo.db()
    for coll, table, cols in [
        ("match_review_decisions", "golden.match_review_decisions",
         ["src_system", "src_ref", "decision", "c360_id", "reviewer", "decided_ts", "note"]),
        ("match_llm_verdicts", "golden.match_llm_verdicts",
         ["src_system", "src_ref", "c360_id", "llm_match_pct", "llm_reason", "model", "verdict_ts"])]:
        rows = list(d[coll].find({}, {"_id": 0, **{c: 1 for c in cols}}))
        con.execute(f"DELETE FROM {table}")
        if rows:
            con.register("_sync_df", pd.DataFrame(rows, columns=cols))
            con.execute(f"INSERT INTO {table} SELECT * FROM _sync_df")
            con.unregister("_sync_df")
    return d


def finalize_source_xref(con):
    t0 = time.time()
    ensure_review_tables(con)
    sync_review_inputs(con)
    union = " UNION ALL ".join(f"SELECT * FROM golden._decided_{s}" for s in ["cards", "loans", "core"])
    con.execute(f"""
    CREATE OR REPLACE TABLE golden.source_customer_xref AS
    WITH d AS ({union}),
    hum AS (SELECT * FROM golden.match_review_decisions
            QUALIFY row_number() OVER (PARTITION BY src_system, src_ref ORDER BY decided_ts DESC) = 1),
    llm AS (SELECT src_system, src_ref, arg_max(c360_id, llm_match_pct) AS c360_id, max(llm_match_pct) AS pct,
                   arg_max(llm_reason, llm_match_pct) AS reason
            FROM golden.match_llm_verdicts GROUP BY 1, 2),
    flagged AS (
        SELECT d.*, contains(coalesce(d.evidence, ''), 'bridge:0') AS bridge_conflict FROM d
    )
    SELECT f.src_system, f.src_ref,
        CASE WHEN h.decision IN ('accept') THEN f.c360_id
             WHEN h.decision = 'relink' THEN h.c360_id
             WHEN h.decision = 'reject' THEN NULL
             WHEN f.decision = 'auto_link' AND NOT f.bridge_conflict THEN f.c360_id
             WHEN l.pct >= 90 THEN l.c360_id
             WHEN f.decision IN ('auto_link', 'review') THEN f.c360_id
             ELSE NULL END AS c360_id,
        CASE WHEN h.decision IN ('accept', 'relink') THEN 'human_confirmed'
             WHEN h.decision = 'reject' THEN 'human_rejected'
             WHEN f.decision = 'auto_link' AND NOT f.bridge_conflict THEN 'auto_link'
             WHEN l.pct >= 90 THEN 'llm_link'
             WHEN f.decision IN ('auto_link', 'review') THEN 'provisional'
             ELSE 'unmatched' END AS link_status,
        CASE WHEN h.decision IN ('accept', 'relink') THEN 1.0
             WHEN l.pct >= 90 AND NOT (f.decision = 'auto_link' AND NOT f.bridge_conflict) THEN l.pct / 100
             ELSE f.match_confidence END AS match_confidence,
        f.match_confidence AS model_confidence,
        f.match_confidence_pii_only, f.matched_crm_id, f.evidence, f.bridge_conflict,
        f.runner_up_c360_id, f.runner_up_confidence, f.top_candidates,
        l.pct AS llm_match_pct, l.reason AS llm_reason, h.reviewer, h.note AS review_note
    FROM flagged f
    LEFT JOIN hum h ON h.src_system = f.src_system AND h.src_ref = f.src_ref
    LEFT JOIN llm l ON l.src_system = f.src_system AND l.src_ref = f.src_ref
    """)
    # Review queue: everything not settled automatically or by a person.
    con.execute("""
    CREATE OR REPLACE TABLE golden.match_review_queue AS
    SELECT x.src_system, x.src_ref, x.link_status,
        CASE WHEN x.bridge_conflict THEN 'identity data and account history point to different people'
             WHEN x.link_status = 'unmatched' AND x.top_candidates IS NULL THEN 'no candidate shares enough identity data'
             WHEN x.link_status = 'unmatched' THEN 'best candidate below review threshold'
             WHEN x.runner_up_confidence >= 0.1 THEN 'two plausible people'
             ELSE 'confidence below auto-link threshold' END AS review_reason,
        round(x.model_confidence, 4) AS model_confidence, x.c360_id AS proposed_c360_id,
        x.top_candidates, x.llm_match_pct, x.llm_reason,
        p.name_raw, p.dob_raw, p.phone_raw, p.postal_raw
    FROM golden.source_customer_xref x
    JOIN (SELECT 'cards' s, src_ref, name_raw, dob_raw, phone_raw, postal_raw FROM golden._src_profile_cards
          UNION ALL SELECT 'loans', src_ref, name_raw, dob_raw, phone_raw, postal_raw FROM golden._src_profile_loans
          UNION ALL SELECT 'core', src_ref, name_raw, dob_raw, phone_raw, postal_raw FROM golden._src_profile_core) p
      ON p.s = x.src_system AND p.src_ref = x.src_ref
    WHERE x.link_status IN ('provisional', 'unmatched', 'llm_link')
    """)
    print(f"  source xref ({time.time() - t0:.1f}s)", flush=True)


def build_account_xref(con):
    """Every account -> person, with the role and the confidence of the owning key's link."""
    t0 = time.time()
    con.execute("""
    CREATE OR REPLACE TABLE golden.account_xref AS
    WITH acct AS (
        SELECT card_account_id AS account_id, 'card' AS product_family, product_code AS product, 'cards' AS src_system,
               src_customer_ref AS src_ref, 'primary' AS role FROM curated.card_accounts
        UNION ALL
        SELECT loan_id, 'loan', product_type, 'loans', src_customer_ref, 'primary' FROM curated.loan_accounts
        UNION ALL
        SELECT loan_id, 'loan', product_type, 'loans', co_borrower_ref, 'co_borrower' FROM curated.loan_accounts WHERE co_borrower_ref IS NOT NULL
        UNION ALL
        SELECT deposit_account_id, 'deposit', product_type, 'core', cif, 'primary' FROM curated.deposit_accounts
        UNION ALL
        SELECT deposit_account_id, 'deposit', product_type, 'core', joint_holder_cif, 'joint' FROM curated.deposit_accounts WHERE joint_holder_cif IS NOT NULL
    )
    SELECT a.*, x.c360_id, coalesce(x.link_status, 'unresolved_key') AS link_status, x.match_confidence
    FROM acct a LEFT JOIN golden.source_customer_xref x ON x.src_system = a.src_system AND x.src_ref = a.src_ref
    """)
    print(f"  account xref ({time.time() - t0:.1f}s)", flush=True)


def link_collections(con):
    """Collections cases carry the CRM id copied at case open; about 3% no longer exist in CRM.

    Valid id   -> person via CRM dedup map (confidence 1.0, or lower if the case's own
                  accounts belong to someone else).
    Orphan id  -> repaired from the owner of the case's accounts, cross-checked against
                  real CRM ids one character away.
    Collision  -> a valid id whose case accounts belong to a customer one digit away: the
                  typo landed on another real customer, so the account owner wins.
    """
    t0 = time.time()
    con.execute("""
    CREATE OR REPLACE TABLE golden.case_xref AS
    WITH c AS (
        SELECT case_id, coll_customer_ref, account_ids, primary_account_id FROM curated.collections_cases
    ), acct_owner AS (
        SELECT c.case_id, ax.c360_id, max(ax.match_confidence) AS conf, count(*) AS n_accts
        FROM (SELECT case_id, unnest(account_ids) AS account_id FROM c) c
        JOIN golden.account_xref ax ON ax.account_id = c.account_id AND ax.role = 'primary' AND ax.c360_id IS NOT NULL
        GROUP BY ALL
    ), owner_top AS (
        SELECT case_id, arg_max(c360_id, n_accts) AS owner_c360, max(conf) AS owner_conf, count(*) AS n_owners
        FROM acct_owner GROUP BY 1
    ), near AS (
        SELECT c.case_id, list(DISTINCT pm.c360_id) AS near_c360
        FROM c JOIN golden.crm_person_map pm ON left(pm.crm_customer_id, 4) = left(c.coll_customer_ref, 4)
              AND levenshtein(pm.crm_customer_id, c.coll_customer_ref) = 1
        GROUP BY 1
    )
    SELECT c.case_id, c.coll_customer_ref,
        CASE WHEN pm.c360_id IS NOT NULL AND (o.owner_c360 IS NULL OR o.owner_c360 = pm.c360_id) THEN pm.c360_id
             WHEN pm.c360_id IS NOT NULL AND list_contains(n.near_c360, o.owner_c360) THEN o.owner_c360
             WHEN pm.c360_id IS NOT NULL THEN pm.c360_id
             WHEN o.owner_c360 IS NOT NULL THEN o.owner_c360
             WHEN len(n.near_c360) = 1 THEN n.near_c360[1] END AS c360_id,
        CASE WHEN pm.c360_id IS NOT NULL AND (o.owner_c360 IS NULL OR o.owner_c360 = pm.c360_id) THEN 'crm_id_exact'
             -- a mistyped digit that happens to land on another real customer's id
             WHEN pm.c360_id IS NOT NULL AND list_contains(n.near_c360, o.owner_c360) THEN 'repaired_id_collision'
             WHEN pm.c360_id IS NOT NULL THEN 'crm_id_exact_account_conflict'
             WHEN o.owner_c360 IS NOT NULL AND list_contains(n.near_c360, o.owner_c360) THEN 'repaired_account_owner_and_id_edit'
             WHEN o.owner_c360 IS NOT NULL THEN 'repaired_account_owner'
             WHEN len(n.near_c360) = 1 THEN 'repaired_id_edit_only'
             ELSE 'unmatched' END AS link_method,
        CASE WHEN pm.c360_id IS NOT NULL AND (o.owner_c360 IS NULL OR o.owner_c360 = pm.c360_id) THEN 1.0
             WHEN pm.c360_id IS NOT NULL AND list_contains(n.near_c360, o.owner_c360) THEN least(o.owner_conf, 0.98)
             WHEN pm.c360_id IS NOT NULL THEN 0.6
             WHEN o.owner_c360 IS NOT NULL AND list_contains(n.near_c360, o.owner_c360) THEN least(o.owner_conf, 0.999)
             WHEN o.owner_c360 IS NOT NULL THEN least(o.owner_conf, 0.95)
             WHEN len(n.near_c360) = 1 THEN 0.7
             ELSE 0.0 END AS match_confidence,
        o.owner_c360 AS account_owner_c360_id
    FROM c
    LEFT JOIN golden.crm_person_map pm ON pm.crm_customer_id = c.coll_customer_ref
    LEFT JOIN owner_top o ON o.case_id = c.case_id
    LEFT JOIN near n ON n.case_id = c.case_id
    """)
    # Contacts whose case is missing from collections_cases fall back to their own CRM id.
    con.execute("""
    CREATE OR REPLACE TABLE golden.contact_orphan_xref AS
    SELECT h.contact_id, h.case_id, h.crm_customer_id, pm.c360_id,
           CASE WHEN pm.c360_id IS NOT NULL THEN 'crm_id_exact' ELSE 'unmatched' END AS link_method
    FROM curated.contact_history h
    LEFT JOIN golden.case_xref cx ON cx.case_id = h.case_id
    LEFT JOIN golden.crm_person_map pm ON pm.crm_customer_id = h.crm_customer_id
    WHERE cx.case_id IS NULL
    """)
    # Case-level review items join the same queue
    con.execute("""
    INSERT INTO golden.match_review_queue (src_system, src_ref, link_status, review_reason, model_confidence, proposed_c360_id)
    SELECT 'collections', case_id,
           CASE WHEN c360_id IS NULL THEN 'unmatched' ELSE 'provisional' END,
           CASE link_method WHEN 'crm_id_exact_account_conflict' THEN 'case CRM id and case accounts point to different people'
                            WHEN 'repaired_id_edit_only' THEN 'CRM id repaired from a one-character edit only'
                            ELSE 'case CRM id not in CRM and accounts unresolved' END,
           match_confidence, c360_id
    FROM golden.case_xref WHERE link_method IN ('crm_id_exact_account_conflict', 'repaired_id_edit_only', 'unmatched')
    """)
    print(f"  collections links ({time.time() - t0:.1f}s)", flush=True)


def run():
    con = config.connect()
    finalize_source_xref(con)
    build_account_xref(con)
    link_collections(con)
    con.close()


if __name__ == "__main__":
    run()
