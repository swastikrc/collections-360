"""Identity resolution: link every source-system customer key to one person.

Pipeline (the design's "pre-filter, then score, then review"):

 1. CRM dedup     1,020,000 CRM records -> people. Records sharing a national-ID hash, or
                  scoring as the same person on name/DOB/phone/email, are clustered.
 2. Bridges       Exact-key evidence: account ids that appear next to a CRM id in
                  account_monthly_snapshot, salary_credit_history and collections_cases.
                  Corrupted CRM ids (last digit mistyped) are repaired by account-level
                  majority vote or, failing that, by a one-character edit to a real id.
 3. Pre-filter    Cheap blocking keys build a shortlist of CRM records per source key
                  (bridge, DOB + initial, swapped DOB, phone, postal + initial, surname + birth year)
                  instead of comparing every source key with every CRM record.
 4. Score         Each shortlisted pair gets a comparison vector (surname, given name, DOB,
                  phone, postal, bridge). A Fellegi-Sunter model turns it into a match
                  probability. There are no labels, so the model is cross-trained per source
                  system: PII weights are learned from bridge-confirmed pairs, bridge weights
                  from PII-confirmed pairs, chance agreement (u) from random pairs. Each field's Bayes factor is kept so
                  every link carries a plain-English explanation.
 5. Decide        Per source key, candidates are rolled up to people. Auto-link when the
                  best person is >= AUTO_LINK and clearly ahead of the runner-up; otherwise
                  the key goes to the review queue with its top candidates (review, don't delete).
"""
import json
import math
import time

import numpy as np

from c360 import config
from c360.layer1 import normalize, profiles

FIELDS = ["sur", "given", "dob", "phone", "postal", "bridge"]
LEVELS = {  # level -> label used in explanations; -1 is always "missing" (neutral)
    "sur": {3: "surname exact", 2: "surname near-identical (typo)", 1: "surname similar", 0: "surname different"},
    "given": {3: "given name agrees", 2: "given name near-identical", 1: "first initial agrees", 0: "given name different"},
    "dob": {3: "date of birth exact", 2: "date of birth day/month swapped", 1: "date of birth one digit off", 0: "date of birth different"},
    "phone": {2: "phone exact", 1: "phone agrees on visible digits", 0: "phone different"},
    "postal": {2: "postal code exact", 1: "same postal area (FSA)", 0: "postal code different"},
    "bridge": {3: "account history names this CRM id", 2: "account history names this CRM id (repaired id)",
               1: "account history names a related CRM record", 0: "account history names someone else"},
}


def _t(msg, t0):
    print(f"  {msg} ({time.time() - t0:.1f}s)", flush=True)


# ---------------------------------------------------------------------------------------
# 1. CRM de-duplication
# ---------------------------------------------------------------------------------------
def dedup_crm(con):
    t0 = time.time()
    con.execute("""
    CREATE OR REPLACE TABLE golden._crm_pairs AS
    WITH cand AS (
        SELECT a.src_ref l, b.src_ref r FROM golden._crm_profile a JOIN golden._crm_profile b
          ON a.nid = b.nid AND a.src_ref < b.src_ref
        UNION
        SELECT a.src_ref, b.src_ref FROM golden._crm_profile a JOIN golden._crm_profile b
          ON a.dob = b.dob AND a.given_i = b.given_i AND left(a.sur, 1) = left(b.sur, 1) AND a.src_ref < b.src_ref
        UNION
        SELECT a.src_ref, b.src_ref FROM golden._crm_profile a JOIN golden._crm_profile b
          ON a.email = b.email AND a.src_ref < b.src_ref
        UNION
        SELECT a.src_ref, b.src_ref FROM golden._crm_profile a JOIN golden._crm_profile b
          ON a.ph10 = b.ph10 AND a.src_ref < b.src_ref
    )
    SELECT c.l, c.r,
        CASE WHEN a.nid IS NULL OR b.nid IS NULL THEN -1 WHEN a.nid = b.nid THEN 1 ELSE 0 END AS nid,
        cmp_surname(a.sur, b.sur) AS sur,
        greatest(cmp_given(a.given, a.given_i, b.given, b.given_i),
                 cmp_given(coalesce(a.pref, a.given), a.given_i, b.given, b.given_i),
                 cmp_given(a.given, a.given_i, coalesce(b.pref, b.given), b.given_i)) AS given,
        cmp_dob(a.dob, b.dob) AS dob,
        CASE WHEN a.ph10 IS NULL OR b.ph10 IS NULL THEN -1 WHEN a.ph10 IN (b.ph10, b.ph10_2, b.ph10_3) OR b.ph10 IN (a.ph10_2, a.ph10_3) THEN 1 ELSE 0 END AS phone,
        CASE WHEN a.email IS NULL OR b.email IS NULL THEN -1 WHEN a.email = b.email THEN 1 ELSE 0 END AS email,
        cmp_postal(a.postal, b.postal) AS postal,
        a.duplicate_of_crm_id = b.src_ref OR b.duplicate_of_crm_id = a.src_ref AS labelled_dup
    FROM cand c JOIN golden._crm_profile a ON a.src_ref = c.l JOIN golden._crm_profile b ON b.src_ref = c.r
    """)
    # Same-person rule set (deliberately conservative: twins and spouses share surname, address, phone).
    # A different national id is a hard veto.
    con.execute("""
    CREATE OR REPLACE TABLE golden._crm_dup_edges AS
    SELECT l, r, labelled_dup,
      CASE
        WHEN nid = 0 THEN 0.0
        WHEN nid = 1 AND dob >= 2 AND sur >= 1 THEN 0.995
        WHEN nid = 1 AND (sur >= 2 OR given >= 2) THEN 0.98
        WHEN nid = -1 AND dob = 3 AND sur >= 2 AND given >= 2 AND (phone = 1 OR email = 1 OR postal = 2) THEN 0.97
        WHEN nid = -1 AND dob = 3 AND sur >= 2 AND given = 3 THEN 0.92
        WHEN nid = -1 AND dob >= 2 AND sur >= 2 AND given >= 2 AND (phone = 1 OR email = 1) THEN 0.90
        WHEN nid = -1 AND dob = 3 AND sur >= 1 AND given >= 1 AND (phone = 1 OR email = 1) AND postal >= 1 THEN 0.80
        ELSE 0.0 END AS p_same,
      nid, sur, given, dob, phone, email, postal
    FROM golden._crm_pairs
    """)
    edges = con.execute(f"SELECT l, r FROM golden._crm_dup_edges WHERE p_same >= {config.AUTO_LINK}").fetchall()
    ids = [r[0] for r in con.execute("SELECT src_ref FROM golden._crm_profile").fetchall()]
    root = _union_find(ids, edges)
    import pandas as pd
    uf = pd.DataFrame({"crm_customer_id": list(root.keys()), "grp": list(root.values())})
    con.register("uf_df", uf)
    # Survivor (master) record: not soft-deleted, most complete, earliest created.
    con.execute("""
    CREATE OR REPLACE TABLE golden.crm_person_map AS
    WITH g AS (
        SELECT u.crm_customer_id, u.grp, p.record_created_ts, p.is_deleted,
               (p.nid IS NOT NULL)::INT + (p.ph10 IS NOT NULL)::INT + (p.email IS NOT NULL)::INT
               + (p.postal IS NOT NULL)::INT + (p.dob IS NOT NULL)::INT AS completeness,
               p.record_source
        FROM uf_df u JOIN golden._crm_profile p ON p.src_ref = u.crm_customer_id
    ), ranked AS (
        SELECT *, row_number() OVER (PARTITION BY grp ORDER BY is_deleted, completeness DESC, record_created_ts, crm_customer_id) rn,
               count(*) OVER (PARTITION BY grp) AS group_size
        FROM g
    ), master AS (SELECT grp, crm_customer_id AS master_crm FROM ranked WHERE rn = 1)
    SELECT r.crm_customer_id, 'C360-' || m.master_crm AS c360_id, m.master_crm AS master_crm_id,
           r.rn = 1 AS is_master, r.group_size,
           CASE WHEN r.rn = 1 THEN 1.0 ELSE coalesce(e.p_same, 0.9) END AS match_confidence,
           CASE WHEN r.rn = 1 THEN 'master_record' ELSE 'crm_duplicate' END AS link_method
    FROM ranked r JOIN master m USING (grp)
    LEFT JOIN (SELECT l a, r b, p_same FROM golden._crm_dup_edges UNION ALL SELECT r, l, p_same FROM golden._crm_dup_edges) e
      ON e.a = r.crm_customer_id AND e.b = m.master_crm
    """)
    con.execute("""CREATE OR REPLACE TABLE golden.crm_person_map AS
        SELECT crm_customer_id, c360_id, master_crm_id, is_master, group_size,
               max(match_confidence) AS match_confidence, any_value(link_method) AS link_method
        FROM golden.crm_person_map GROUP BY ALL""")
    _t("CRM dedup", t0)


def _union_find(ids, edges):
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb
    return {i: find(i) for i in ids}


# ---------------------------------------------------------------------------------------
# 2. Bridges: exact-key evidence from tables that carry account id + CRM id
# ---------------------------------------------------------------------------------------
def build_bridges(con):
    t0 = time.time()
    con.execute("""
    CREATE OR REPLACE TABLE golden._bridge_raw AS
    SELECT account_id, crm_customer_id AS crm_raw, 'account_monthly_snapshot' AS bridge_source, count(*) AS votes
      FROM curated.account_monthly_snapshot WHERE crm_customer_id IS NOT NULL GROUP BY ALL
    UNION ALL
    SELECT deposit_account_id, crm_customer_id, 'salary_credit_history', count(*)
      FROM curated.salary_credit_history WHERE crm_customer_id IS NOT NULL GROUP BY ALL
    UNION ALL
    SELECT acct, coll_customer_ref, 'collections_cases', count(*)
      FROM (SELECT unnest(account_ids) AS acct, coll_customer_ref FROM curated.collections_cases
            WHERE coll_customer_ref IS NOT NULL) GROUP BY ALL
    """)
    # Repair ids that do not exist in CRM. First choice: a valid id on the same account that
    # differs by one character (the account's own history outvotes the typo). Second: the
    # unique valid CRM id within one edit (same letters, one digit different).
    con.execute("""
    CREATE OR REPLACE TABLE golden._bridge AS
    WITH v AS (
        SELECT b.*, k.src_ref IS NOT NULL AS valid FROM golden._bridge_raw b
        LEFT JOIN golden._crm_profile k ON k.src_ref = b.crm_raw
    ), same_acct AS (
        SELECT i.account_id, i.crm_raw, arg_max(g.crm_raw, g.votes) AS fix
        FROM v i JOIN v g ON g.account_id = i.account_id AND g.valid AND NOT i.valid
             AND left(g.crm_raw, 4) = left(i.crm_raw, 4) AND levenshtein(g.crm_raw, i.crm_raw) = 1
        GROUP BY ALL
    ), near AS (
        SELECT i.crm_raw, list(k.src_ref) AS fixes
        FROM (SELECT DISTINCT crm_raw FROM v WHERE NOT valid) i
        JOIN golden._crm_profile k ON left(k.src_ref, 4) = left(i.crm_raw, 4) AND levenshtein(k.src_ref, i.crm_raw) = 1
        GROUP BY 1
    )
    SELECT v.account_id, v.bridge_source, v.votes, v.crm_raw,
           CASE WHEN v.valid THEN v.crm_raw WHEN s.fix IS NOT NULL THEN s.fix
                WHEN len(n.fixes) = 1 THEN n.fixes[1] END AS crm_customer_id,
           CASE WHEN v.valid THEN 'valid' WHEN s.fix IS NOT NULL THEN 'repaired_same_account'
                WHEN len(n.fixes) = 1 THEN 'repaired_unique_neighbour'
                WHEN len(n.fixes) > 1 THEN 'ambiguous' ELSE 'unrepairable' END AS id_status
    FROM v LEFT JOIN same_acct s ON s.account_id = v.account_id AND s.crm_raw = v.crm_raw
    LEFT JOIN near n ON n.crm_raw = v.crm_raw
    """)
    # Account -> source key (which system key owns this account)
    con.execute("""
    CREATE OR REPLACE TABLE golden._account_owner_key AS
    SELECT DISTINCT card_account_id AS account_id, 'cards' AS src_system, src_customer_ref AS src_ref, 'primary' AS role
      FROM curated.card_accounts WHERE src_customer_ref IS NOT NULL
    UNION ALL SELECT DISTINCT loan_id, 'loans', src_customer_ref, 'primary' FROM curated.loan_accounts WHERE src_customer_ref IS NOT NULL
    UNION ALL SELECT DISTINCT deposit_account_id, 'core', cif, 'primary' FROM curated.deposit_accounts WHERE cif IS NOT NULL
    """)
    # Bridge votes rolled up to (source key, CRM id): share of that key's bridge votes naming the id
    con.execute("""
    CREATE OR REPLACE TABLE golden._bridge_key AS
    WITH j AS (
        SELECT o.src_system, o.src_ref, b.crm_customer_id, b.id_status, b.votes, b.bridge_source
        FROM golden._bridge b JOIN golden._account_owner_key o ON o.account_id = b.account_id
        WHERE b.crm_customer_id IS NOT NULL
    ), tot AS (SELECT src_system, src_ref, sum(votes) tv FROM j GROUP BY ALL)
    SELECT j.src_system, j.src_ref, j.crm_customer_id,
           sum(j.votes) AS votes, sum(j.votes) / any_value(t.tv) AS vote_share,
           bool_or(j.id_status = 'valid') AS any_valid,
           list(DISTINCT j.bridge_source) AS bridge_sources
    FROM j JOIN tot t USING (src_system, src_ref) GROUP BY ALL
    """)
    _t("bridges", t0)


# ---------------------------------------------------------------------------------------
# 3 + 4. Candidate shortlist and comparison vectors
# ---------------------------------------------------------------------------------------
def build_candidates(con, system):
    t0 = time.time()
    p = f"golden._src_profile_{system}"
    con.execute(f"""
    CREATE OR REPLACE TABLE golden._cand_{system} AS
    WITH c AS (
        -- bridge (exact key evidence)
        SELECT src_ref, crm_customer_id AS crm FROM golden._bridge_key WHERE src_system = '{system}'
        UNION
        -- DOB + an initial (either name order)
        SELECT s.src_ref, k.src_ref FROM {p} s JOIN golden._crm_profile k ON k.dob = s.dob
          WHERE left(s.sur, 1) IN (left(k.sur, 1), k.given_i) OR s.given_i = left(k.sur, 1)
        UNION
        -- DOB keyed with day and month swapped
        SELECT s.src_ref, k.src_ref FROM {p} s JOIN golden._crm_profile k
          ON day(s.dob) <= 12 AND k.dob = make_date(year(s.dob), day(s.dob), month(s.dob))
          WHERE left(s.sur, 1) = left(k.sur, 1)
        UNION
        -- phone (full or masked)
        SELECT s.src_ref, k.src_ref FROM {p} s JOIN golden._crm_phone k ON k.phmask = s.phmask
        UNION
        -- postal code + surname initial
        SELECT s.src_ref, k.src_ref FROM {p} s JOIN golden._crm_profile k ON k.postal = s.postal
          WHERE left(s.sur, 1) IN (left(k.sur, 1), k.given_i)
        UNION
        -- surname + birth year (catches DOB typos for people who moved)
        SELECT s.src_ref, k.src_ref FROM {p} s JOIN golden._crm_profile k ON k.sur = s.sur AND year(k.dob) = year(s.dob)
          WHERE s.given_i = k.given_i
    )
    SELECT c.src_ref, c.crm,
        greatest(cmp_surname(s.sur, k.sur), cmp_surname(s.sur, k.given) - 1) AS sur,
        greatest(cmp_given(s.given, s.given_i, k.given, k.given_i),
                 cmp_given(s.given, s.given_i, k.pref, left(k.pref, 1)),
                 CASE WHEN s.given IS NOT NULL AND s.given = k.sur THEN 2 ELSE -2 END) AS given,
        cmp_dob(s.dob, k.dob) AS dob,
        greatest(cmp_phone(s.ph10, s.phmask, k.ph10, k.phmask),
                 cmp_phone(s.ph10, s.phmask, k.ph10_2, k.phmask_2),
                 cmp_phone(s.ph10, s.phmask, k.ph10_3, k.phmask_3)) AS phone,
        greatest(cmp_postal(s.postal, k.postal), cmp_postal(s.postal_2, k.postal)) AS postal,
        CASE WHEN bk.crm_customer_id IS NOT NULL AND bk.vote_share >= 0.5 AND bk.any_valid THEN 3
             WHEN bk.crm_customer_id IS NOT NULL AND bk.vote_share >= 0.5 THEN 2
             WHEN bk.crm_customer_id IS NOT NULL THEN 1
             WHEN hb.src_ref IS NOT NULL THEN 0
             ELSE -1 END AS bridge
    FROM c
    JOIN {p} s ON s.src_ref = c.src_ref
    JOIN golden._crm_profile k ON k.src_ref = c.crm
    LEFT JOIN golden._bridge_key bk ON bk.src_system = '{system}' AND bk.src_ref = c.src_ref AND bk.crm_customer_id = c.crm
    LEFT JOIN (SELECT DISTINCT src_ref FROM golden._bridge_key WHERE src_system = '{system}') hb ON hb.src_ref = c.src_ref
    """)
    # given name compared against CRM surname (swapped order) can produce -2 sentinel; clamp
    con.execute(f"UPDATE golden._cand_{system} SET given = -1 WHERE given < -1")
    n = con.execute(f"SELECT count(*), count(DISTINCT src_ref) FROM golden._cand_{system}").fetchone()
    _t(f"candidates[{system}]: {n[0]:,} pairs for {n[1]:,} keys", t0)


# ---------------------------------------------------------------------------------------
# Fellegi-Sunter model: u from random pairs, m cross-trained on independent evidence
# ---------------------------------------------------------------------------------------
def estimate_u(con, system, n=2_000_000):
    """Chance agreement rates between unrelated records."""
    p = f"golden._src_profile_{system}"
    df = con.execute(f"""
    WITH s AS (SELECT * FROM {p} USING SAMPLE {n} ROWS (reservoir, 42)),
         k AS (SELECT * FROM golden._crm_profile USING SAMPLE {n} ROWS (reservoir, 7)),
         sr AS (SELECT *, row_number() OVER () rn FROM s),
         kr AS (SELECT *, row_number() OVER () rn FROM k)
    SELECT greatest(cmp_surname(s.sur, k.sur), cmp_surname(s.sur, k.given) - 1) AS sur,
           cmp_given(s.given, s.given_i, k.given, k.given_i) AS given,
           cmp_dob(s.dob, k.dob) AS dob,
           cmp_phone(s.ph10, s.phmask, k.ph10, k.phmask) AS phone,
           cmp_postal(s.postal, k.postal) AS postal,
           count(*) AS n
    FROM sr s JOIN kr k ON k.rn = s.rn GROUP BY ALL
    """).df()
    u = {}
    for f in ["sur", "given", "dob", "phone", "postal"]:
        g = df[df[f] >= 0].groupby(f)["n"].sum()
        tot = g.sum()
        u[f] = {int(l): max(float(c) / tot, 1e-7) for l, c in g.items()}
        for l in LEVELS[f]:
            u[f].setdefault(l, 1e-7)
    # Bridge: an unrelated CRM record is essentially never named by a key's account history.
    u["bridge"] = {3: 1e-6, 2: 1e-6, 1: 1e-4, 0: 1 - 2.1e-4}
    return u


def fit_cross_trained(con, system, u):
    """Learn m-probabilities without labels by letting independent evidence teach each other.

    PII fields: m = level distribution among pairs the account bridge confirms (bridge level 3).
                The bridge comes from a different system than the name/DOB/phone/postal on the
                record, so these pairs are an unbiased sample of true matches.
    Bridge:     m = level distribution among pairs that PII alone makes near-certain;
                u = level distribution among pairs PII alone makes near-impossible.
    Prior:      one person in the whole customer base, i.e. odds 1 : N.
    """
    n_people = con.execute("SELECT count(DISTINCT c360_id) FROM golden.crm_person_map").fetchone()[0]
    lam = 1.0 / n_people
    m = {}
    pii = [f for f in FIELDS if f != "bridge"]
    for f in pii:
        rows = dict(con.execute(f"SELECT {f}, count(*) FROM golden._cand_{system} WHERE bridge = 3 AND {f} >= 0 GROUP BY 1").fetchall())
        tot = sum(rows.values()) + len(LEVELS[f])
        m[f] = {l: (rows.get(l, 0) + 1) / tot for l in LEVELS[f]}  # Laplace-smoothed
    prior = math.log2(lam / (1 - lam))
    pii_w = " + ".join(
        "CASE " + " ".join(f"WHEN {f} = {l} THEN {math.log2(m[f][l] / u[f][l])}" for l in LEVELS[f]) + " ELSE 0 END"
        for f in pii)
    for label, cond in [("m", f"{prior} + {pii_w} >= 10"), ("u", f"{prior} + {pii_w} <= -10")]:
        rows = dict(con.execute(f"SELECT bridge, count(*) FROM golden._cand_{system} WHERE bridge >= 0 AND {cond} GROUP BY 1").fetchall())
        tot = sum(rows.values()) + len(LEVELS["bridge"])
        dist = {l: (rows.get(l, 0) + 1) / tot for l in LEVELS["bridge"]}
        if label == "m":
            m["bridge"] = dist
        else:
            u["bridge"] = dist
    return m, lam


def score(con, system, m, u, lam):
    """Write per-pair probability and per-field log2 Bayes factors."""
    rows = []
    for f in FIELDS:
        # Monotone weights: a closer agreement level never counts for less than a looser one.
        # (Middle levels are rare among true matches, so their raw estimates are noisy.)
        best = -math.inf
        for l in sorted(LEVELS[f]):
            best = max(best, math.log2(m[f][l] / u[f][l]))
            rows.append((system, f, l, LEVELS[f][l], m[f][l], u[f][l], best))
    con.execute("CREATE TABLE IF NOT EXISTS golden.match_model_weights(src_system VARCHAR, field VARCHAR, level INT, meaning VARCHAR, m DOUBLE, u DOUBLE, log2_bf DOUBLE)")
    con.execute("DELETE FROM golden.match_model_weights WHERE src_system = ?", [system])
    con.executemany("INSERT INTO golden.match_model_weights VALUES (?,?,?,?,?,?,?)", rows)
    con.execute("CREATE TABLE IF NOT EXISTS golden.match_model_prior(src_system VARCHAR, prior_match_prob DOUBLE)")
    con.execute("DELETE FROM golden.match_model_prior WHERE src_system = ?", [system])
    con.execute("INSERT INTO golden.match_model_prior VALUES (?, ?)", [system, lam])
    joins = "\n".join(
        f"LEFT JOIN (SELECT level, log2_bf FROM golden.match_model_weights WHERE src_system='{system}' AND field='{f}') w_{f} ON w_{f}.level = c.{f}"
        for f in FIELDS)
    bfs = " + ".join(f"coalesce(w_{f}.log2_bf, 0)" for f in FIELDS)
    prior = math.log2(lam / (1 - lam))
    con.execute(f"""
    CREATE OR REPLACE TABLE golden._scored_{system} AS
    SELECT c.*, {', '.join(f'coalesce(w_{f}.log2_bf, 0) AS bf_{f}' for f in FIELDS)},
           {prior} + {bfs} AS match_weight,
           1 / (1 + pow(2, -({prior} + {bfs}))) AS p_match
    FROM golden._cand_{system} c {joins}
    """)
    # PII-only probability (no bridge) — used to report how well identity data alone agrees with bridges
    bfs_pii = " + ".join(f"bf_{f}" for f in FIELDS if f != "bridge")
    con.execute(f"ALTER TABLE golden._scored_{system} ADD COLUMN p_match_pii DOUBLE")
    con.execute(f"UPDATE golden._scored_{system} SET p_match_pii = 1 / (1 + pow(2, -({prior} + {bfs_pii})))")


# ---------------------------------------------------------------------------------------
# 5. Decision per source key (rolled up to people)
# ---------------------------------------------------------------------------------------
def decide(con, system):
    t0 = time.time()
    expl = " || ".join(
        f"CASE WHEN c.{f} >= 0 THEN '{f}:' || c.{f} || ':' || round(c.bf_{f}, 1) || ';' ELSE '' END" for f in FIELDS)
    con.execute(f"""
    CREATE OR REPLACE TABLE golden._decided_{system} AS
    WITH per_person AS (
        SELECT c.src_ref, pm.c360_id,
               arg_max(c.crm, c.match_weight) AS best_crm,
               max(c.match_weight) AS w, max(c.p_match_pii) AS p_pii,
               arg_max({expl}, c.match_weight) AS evidence
        FROM golden._scored_{system} c JOIN golden.crm_person_map pm ON pm.crm_customer_id = c.crm
        GROUP BY ALL
    ), posterior AS (
        -- P(person i | evidence) = 2^w_i / (1 + sum_j 2^w_j): the "1" is "none of the shortlist".
        -- A strong runner-up (e.g. a relative at the same address) therefore lowers confidence.
        SELECT *, pow(2, w - mx) / (pow(2, -mx) + sum(pow(2, w - mx)) OVER (PARTITION BY src_ref)) AS p
        FROM (SELECT *, greatest(max(w) OVER (PARTITION BY src_ref), 0) AS mx FROM per_person)
    ), ranked AS (
        SELECT *, row_number() OVER (PARTITION BY src_ref ORDER BY p DESC, c360_id) rk FROM posterior
    ), top AS (
        SELECT src_ref,
               max(CASE WHEN rk = 1 THEN c360_id END) AS c360_id,
               max(CASE WHEN rk = 1 THEN best_crm END) AS matched_crm_id,
               max(CASE WHEN rk = 1 THEN p END) AS match_confidence,
               max(CASE WHEN rk = 1 THEN p_pii END) AS match_confidence_pii_only,
               max(CASE WHEN rk = 1 THEN evidence END) AS evidence,
               max(CASE WHEN rk = 2 THEN c360_id END) AS runner_up_c360_id,
               coalesce(max(CASE WHEN rk = 2 THEN p END), 0) AS runner_up_confidence,
               list({{'c360_id': c360_id, 'crm_customer_id': best_crm, 'p': round(p, 4), 'evidence': evidence}}
                    ORDER BY rk) FILTER (WHERE rk <= {config.MAX_CANDIDATES}) AS top_candidates
        FROM ranked GROUP BY 1
    )
    SELECT '{system}' AS src_system, s.src_ref, t.*  EXCLUDE (src_ref),
           CASE WHEN t.match_confidence >= {config.AUTO_LINK}
                     AND t.match_confidence - t.runner_up_confidence >= {config.AMBIGUITY_MARGIN} THEN 'auto_link'
                WHEN t.match_confidence >= {config.REVIEW_FLOOR} THEN 'review'
                ELSE 'unmatched' END AS decision
    FROM golden._src_profile_{system} s LEFT JOIN top t ON t.src_ref = s.src_ref
    """)
    _t(f"decisions[{system}]", t0)


def explain(evidence, system_weights=None):
    """'sur:3:7.1;dob:3:12.4;' -> 'surname exact (+7.1), date of birth exact (+12.4)'"""
    if not evidence:
        return "no candidate found"
    parts = []
    for tok in evidence.strip(";").split(";"):
        f, l, bf = tok.split(":")
        label = LEVELS[f].get(int(l), f)
        parts.append(f"{label} ({float(bf):+.1f})")
    return ", ".join(parts)


def resolve_system(con, system):
    build_candidates(con, system)
    t0 = time.time()
    u = estimate_u(con, system)
    m, lam = fit_cross_trained(con, system, u)
    score(con, system, m, u, lam)
    _t(f"model[{system}] prior=1/{1/lam:,.0f}", t0)
    decide(con, system)


def run():
    con = config.connect()
    normalize.install(con)
    con.execute("CREATE SCHEMA IF NOT EXISTS golden")
    t0 = time.time()
    profiles.build_profiles(con)
    _t("profiles", t0)
    dedup_crm(con)
    build_bridges(con)
    for system in ["cards", "loans", "core"]:
        resolve_system(con, system)
    con.close()


if __name__ == "__main__":
    run()
