"""Identity profiles: one standardised identity row per customer key in each source system.

Systems and keys (README "Customer keys"):
  CRM          customers.crm_customer_id
  Cards        card_accounts.src_customer_ref       (card-system customer number)
  Lending      loan_accounts.src_customer_ref       (borrower number)
  Core banking deposit_accounts.src_customer_ref / external.bank_subject_ref (10-digit CIF)
  Collections  collections_cases.coll_customer_ref  (CRM id copied at case open)

Every profile has the same columns so one matcher handles them all:
  src_ref, sur, given, given_i, mid_i, dob, dob_swap, ph10, phmask, ph10_2, phmask_2, postal, postal_2
"""

PROFILE_SQL = {
    # CRM: structured fields. Up to three phones per record, so we unpivot them separately.
    "crm": r"""
CREATE OR REPLACE TABLE golden._crm_profile AS
SELECT crm_customer_id AS src_ref,
       norm_name(last_name) AS sur,
       norm_name(first_name) AS given,
       norm_name(preferred_name) AS pref,
       left(norm_name(first_name), 1) AS given_i,
       left(norm_name(middle_name), 1) AS mid_i,
       coalesce(date_of_birth, parse_date_any(dob_raw)) AS dob,
       phone10(primary_phone_raw) AS ph10, phone_mask_key(primary_phone_raw) AS phmask,
       phone10(secondary_phone_raw) AS ph10_2, phone_mask_key(secondary_phone_raw) AS phmask_2,
       phone10(work_phone_raw) AS ph10_3, phone_mask_key(work_phone_raw) AS phmask_3,
       coalesce(postal6(postal_code_raw), postal6(postal_code)) AS postal,
       email_std AS email,
       national_id_hash AS nid,
       record_created_ts, record_source, duplicate_of_crm_id, coalesce(is_deleted, false) AS is_deleted
FROM curated.customers;
""",

    # Cards: "L. GARCIA", "BRADLEY MATA", "HUANG/JASON"; DOB dd-mm-yyyy; phone often masked 672993xxx02.
    "cards": r"""
CREATE OR REPLACE TABLE golden._src_profile_cards AS
WITH latest AS (
    SELECT src_customer_ref,
           arg_max(cardholder_name_raw, snapshot_date) AS nm,
           arg_max(cardholder_dob_raw, snapshot_date) AS dob_raw,
           arg_max(cardholder_phone_raw, snapshot_date) AS ph_raw,
           arg_max(cardholder_postal_raw, snapshot_date) AS pc_raw,
           list(DISTINCT card_account_id) AS account_ids
    FROM curated.card_accounts WHERE src_customer_ref IS NOT NULL GROUP BY 1
), parsed AS (
    SELECT *, upper(trim(nm)) AS u FROM latest
)
SELECT src_customer_ref AS src_ref, account_ids,
       norm_name(CASE WHEN contains(u, '/') THEN split_part(u, '/', 1)
                      WHEN regexp_matches(u, '^[A-Z]\.\s') THEN regexp_extract(u, '^[A-Z]\.\s+(.+)$', 1)
                      ELSE regexp_extract(u, '^\S+\s+(.+)$', 1) END) AS sur,
       CASE WHEN contains(u, '/') THEN norm_name(split_part(u, '/', 2))
            WHEN regexp_matches(u, '^[A-Z]\.\s') THEN NULL
            ELSE norm_name(regexp_extract(u, '^(\S+)\s', 1)) END AS given,
       lower(left(CASE WHEN contains(u, '/') THEN norm_name(split_part(u, '/', 2)) ELSE norm_name(u) END, 1)) AS given_i,
       NULL::VARCHAR AS mid_i,
       coalesce(try_strptime(dob_raw, '%d-%m-%Y'), try_strptime(dob_raw, '%m-%d-%Y'))::DATE AS dob,
       phone10(ph_raw) AS ph10, phone_mask_key(ph_raw) AS phmask,
       NULL::VARCHAR AS ph10_2, NULL::VARCHAR AS phmask_2,
       postal6(pc_raw) AS postal, NULL::VARCHAR AS postal_2,
       nm AS name_raw, dob_raw, ph_raw AS phone_raw, pc_raw AS postal_raw
FROM parsed;
""",

    # Lending: "Rachael Dennis" (given surname) but ALL-CAPS rows are "ST-LAURENT PHILIPPE" (surname given).
    "loans": r"""
CREATE OR REPLACE TABLE golden._src_profile_loans AS
WITH latest AS (
    SELECT src_customer_ref,
           arg_max(borrower_name_raw, snapshot_date) AS nm,
           arg_max(borrower_dob_raw, snapshot_date) AS dob_raw,
           arg_max(borrower_phone_raw, snapshot_date) AS ph_raw,
           arg_max(borrower_postal_raw, snapshot_date) AS pc_raw,
           list(DISTINCT loan_id) AS account_ids
    FROM curated.loan_accounts WHERE src_customer_ref IS NOT NULL GROUP BY 1
), parsed AS (
    SELECT *, trim(nm) AS t, trim(nm) = upper(trim(nm)) AS caps FROM latest
)
SELECT src_customer_ref AS src_ref, account_ids,
       norm_name(CASE WHEN caps THEN regexp_extract(t, '^(\S+)', 1) ELSE regexp_extract(t, '^\S+\s+(.+)$', 1) END) AS sur,
       norm_name(CASE WHEN caps THEN regexp_extract(t, '^\S+\s+(.+)$', 1) ELSE regexp_extract(t, '^(\S+)', 1) END) AS given,
       left(norm_name(CASE WHEN caps THEN regexp_extract(t, '^\S+\s+(.+)$', 1) ELSE regexp_extract(t, '^(\S+)', 1) END), 1) AS given_i,
       NULL::VARCHAR AS mid_i,
       parse_date_any(dob_raw) AS dob,
       phone10(ph_raw) AS ph10, phone_mask_key(ph_raw) AS phmask,
       NULL::VARCHAR AS ph10_2, NULL::VARCHAR AS phmask_2,
       postal6(pc_raw) AS postal, NULL::VARCHAR AS postal_2,
       nm AS name_raw, dob_raw, ph_raw AS phone_raw, pc_raw AS postal_raw
FROM parsed;
""",

    # Core banking: CIF shared by deposits ("WANG T J", yyyymmdd) and bureau pulls ("YANG, WAYNE JACK").
    # Some deposit extracts dropped the CIF's leading zeros; cif10() restores them.
    "core": r"""
CREATE OR REPLACE TABLE golden._src_profile_core AS
WITH dep AS (
    SELECT cif,
           arg_max(holder_name_raw, snapshot_date) AS nm,
           arg_max(holder_dob_raw, snapshot_date) AS dob_raw,
           arg_max(holder_phone_raw, snapshot_date) AS ph_raw,
           arg_max(holder_postal_raw, snapshot_date) AS pc_raw,
           list(DISTINCT deposit_account_id) AS account_ids
    FROM curated.deposit_accounts WHERE cif IS NOT NULL GROUP BY 1
), dep_p AS (
    SELECT *, upper(trim(nm)) AS u,
           regexp_extract(upper(trim(nm)), '^(.+?)((\s[A-Z])+)$', 1) AS d_last,
           trim(regexp_extract(upper(trim(nm)), '^(.+?)((\s[A-Z])+)$', 2)) AS d_inits
    FROM dep
), ext AS (
    SELECT cif,
           arg_max(subject_name_reported, pull_date) AS e_nm,
           arg_max(subject_dob_reported, pull_date) AS e_dob,
           arg_max(subject_address_reported, pull_date) AS e_addr
    FROM curated.external WHERE cif IS NOT NULL AND subject_name_reported IS NOT NULL GROUP BY 1
), ext_p AS (
    SELECT *, norm_name(split_part(e_nm, ',', 1)) AS e_last,
           norm_name(regexp_extract(trim(split_part(e_nm, ',', 2)), '^(\S+)', 1)) AS e_given,
           left(norm_name(regexp_extract(trim(split_part(e_nm, ',', 2)), '^\S+\s+(\S+)', 1)), 1) AS e_mid,
           postal6(regexp_extract(upper(e_addr), '([A-Z][0-9][A-Z]\s?[0-9][A-Z][0-9])\s*$', 1)) AS e_postal
    FROM ext
)
SELECT coalesce(d.cif, e.cif) AS src_ref, d.account_ids,
       coalesce(norm_name(nullif(d.d_last, '')), norm_name(d.u), e.e_last) AS sur,
       e.e_given AS given,
       coalesce(left(e.e_given, 1), lower(left(d.d_inits, 1))) AS given_i,
       coalesce(e.e_mid, lower(nullif(substr(replace(d.d_inits, ' ', ''), 2, 1), ''))) AS mid_i,
       coalesce(try_strptime(d.dob_raw, '%Y%m%d')::DATE, e.e_dob::DATE) AS dob,
       phone10(d.ph_raw) AS ph10, phone_mask_key(d.ph_raw) AS phmask,
       NULL::VARCHAR AS ph10_2, NULL::VARCHAR AS phmask_2,
       coalesce(postal6(d.pc_raw), e.e_postal) AS postal,
       CASE WHEN postal6(d.pc_raw) IS NOT NULL AND e.e_postal IS DISTINCT FROM postal6(d.pc_raw) THEN e.e_postal END AS postal_2,
       coalesce(d.nm, e.e_nm) AS name_raw, coalesce(d.dob_raw, e.e_dob::VARCHAR) AS dob_raw,
       d.ph_raw AS phone_raw, coalesce(d.pc_raw, e.e_addr) AS postal_raw,
       d.cif IS NOT NULL AS has_deposits, e.cif IS NOT NULL AS has_bureau
FROM dep_p d FULL OUTER JOIN ext_p e ON d.cif = e.cif;
""",
}


def build_profiles(con):
    for name, sql in PROFILE_SQL.items():
        con.execute(sql)
    # All CRM phones in one long table for phone blocking
    con.execute("""
    CREATE OR REPLACE TABLE golden._crm_phone AS
    SELECT src_ref, ph10, phmask FROM golden._crm_profile WHERE phmask IS NOT NULL
    UNION SELECT src_ref, ph10_2, phmask_2 FROM golden._crm_profile WHERE phmask_2 IS NOT NULL
    UNION SELECT src_ref, ph10_3, phmask_3 FROM golden._crm_profile WHERE phmask_3 IS NOT NULL""")
