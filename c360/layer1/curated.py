"""Curated zone: typed, standardised, flagged — but never silently changed.

Rules
- Every table keeps every raw row and every raw column name, so metrics computed on the
  curated layer reconcile with the source systems.
- Columns are cast to the type declared in schema.json. Values that fail the cast become
  NULL and are counted in `curated._cast_failures` (feeds the data quality report).
- Fixes are added as *new* columns (`*_std`, `*_recomputed`, `dq_*` flags) so analysts and
  the Layer 2 agent can see both the stored value and the corrected one.
"""
import json
import re
import time

from c360.config import BRONZE_DB, SCHEMA_JSON, connect
from c360.layer1 import normalize

_CONTACT_DUP_WINDOW = ("PARTITION BY case_id, account_id, contact_ts_utc, coalesce(channel_v2, channel), direction, "
                       "outcome_code, agent_id, talk_duration_sec, sms_template_id, letter_template_id, "
                       "email_template_id, payment_amount, ptp_id ORDER BY contact_id")

# Extra columns per table: (sql expression over the typed columns, new column name)
DERIVED = {
    "customers": [
        ("postal6(postal_code_raw)", "postal_code_std"),
        ("phone10(primary_phone_raw)", "primary_phone_std"),
        ("lower(trim(email))", "email_std"),
        ("province_std(province_code, coalesce(postal_code, postal_code_raw))", "province_code_std"),
        ("province_code IS DISTINCT FROM province_std(province_code, coalesce(postal_code, postal_code_raw))", "dq_province_nonstandard"),
        ("national_id_hash IS NOT NULL AND count(*) OVER (PARTITION BY national_id_hash) > 1", "dq_shared_national_id"),
    ],
    "card_accounts": [
        ("dpd_bucket_from_dpd(dpd)", "dpd_bucket_recomputed"),
        ("dpd IS NULL", "dq_dpd_missing"),
        ("dpd IS NOT NULL AND dpd_bucket IS DISTINCT FROM dpd_bucket_from_dpd(dpd)", "dq_bucket_mismatch"),
        ("snapshot_date < DATE '2026-09-28'", "dq_stale_snapshot"),
    ],
    "loan_accounts": [
        ("dpd_bucket_from_dpd(dpd)", "dpd_bucket_recomputed"),
        ("dpd IS NULL", "dq_dpd_missing"),
        ("dpd IS NOT NULL AND dpd_bucket IS DISTINCT FROM dpd_bucket_from_dpd(dpd)", "dq_bucket_mismatch"),
        ("snapshot_date < DATE '2026-09-28'", "dq_stale_snapshot"),
    ],
    "deposit_accounts": [
        ("cif10(src_customer_ref)", "cif"),
        ("cif10(joint_holder_ref)", "joint_holder_cif"),
        ("length(trim(src_customer_ref)) <> 10", "dq_cif_leading_zeros_lost"),
        ("dpd_bucket_from_dpd(overdraft_dpd)", "overdraft_dpd_bucket_recomputed"),
        ("snapshot_date < DATE '2026-09-28'", "dq_stale_snapshot"),
    ],
    "external": [
        ("cif10(bank_subject_ref)", "cif"),
        # Bureau load incident DFI-0812: a shifted column put another field into
        # inquiries_hard_3m, which then exceeds the 6-month count (impossible).
        ("coalesce(inquiries_hard_3m > inquiries_hard_6m, false)", "dq_shifted_row"),
        ("CASE WHEN inquiries_hard_3m > inquiries_hard_6m THEN NULL ELSE inquiries_hard_3m END", "inquiries_hard_3m_std"),
    ],
    "bureau_history": [("cif10(bank_subject_ref)", "cif")],
    "collections_cases": [
        ("dpd_bucket_from_dpd(current_dpd)", "current_bucket_recomputed"),
        ("current_dpd IS NOT NULL AND current_bucket IS DISTINCT FROM dpd_bucket_from_dpd(current_dpd)", "dq_bucket_mismatch"),
        ("outcome IS NULL", "is_open"),
    ],
    "contact_history": [
        ("coalesce(channel_v2, channel)", "channel_std"),
        ("channel IS NULL AND channel_v2 IS NOT NULL", "dq_channel_renamed"),
        # Double-loaded contacts: identical event under a second contact_id. Flagged, not deleted.
        (f"CASE WHEN row_number() OVER ({_CONTACT_DUP_WINDOW}) > 1 THEN first_value(contact_id) OVER ({_CONTACT_DUP_WINDOW}) END",
         "dq_duplicate_of"),
    ],
    "model_scores": [
        # Same entity, model and timestamp scored twice with different values: the later score_id wins.
        ("row_number() OVER (PARTITION BY entity_id, model_name, score_ts ORDER BY score_id DESC) > 1", "dq_superseded_duplicate"),
    ],
    "transactions": [
        ("count(*) OVER (PARTITION BY account_id, txn_ts, amount, direction, raw_description) > 1", "dq_possible_duplicate"),
    ],
    "account_monthly_snapshot": [
        ("dpd_bucket_from_dpd(dpd)", "dpd_bucket_recomputed"),
        ("dpd IS NOT NULL AND dpd_bucket IS DISTINCT FROM dpd_bucket_from_dpd(dpd)", "dq_bucket_mismatch"),
    ],
}

# Raw date/timestamp text that is not ISO — counted, then parsed by parse_date_any.
_ISO_DATE = r"^\d{4}-\d{2}-\d{2}$"
_ISO_TS = r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$"


def _duck_type(t):
    t = t.lower()
    if t in ("string", "enum"):
        return "VARCHAR"
    if t == "int":
        return "BIGINT"
    if t.startswith("decimal"):
        return "DOUBLE"
    if t == "boolean":
        return "BOOLEAN"
    if t == "date":
        return "DATE"
    if t == "timestamp":
        return "TIMESTAMP"
    if t.startswith("array"):
        return "VARCHAR[]"
    return "VARCHAR"  # json and anything else stays text


def _cast_expr(col, dtype, physical):
    c = f'"{col}"'
    if physical != "VARCHAR" or dtype == "VARCHAR":  # parquet columns arrive typed
        return c
        return c
    if dtype == "BIGINT":
        return f"try_cast(try_cast({c} AS DOUBLE) AS BIGINT)"
    if dtype == "DOUBLE":
        return f"try_cast({c} AS DOUBLE)"
    if dtype == "BOOLEAN":
        return (f"CASE lower(trim({c})) WHEN 'true' THEN true WHEN 'false' THEN false "
                f"WHEN '1' THEN true WHEN '0' THEN false WHEN 'y' THEN true WHEN 'n' THEN false END")
    if dtype == "DATE":
        return f"parse_date_any({c})"
    if dtype == "TIMESTAMP":
        return f"parse_ts_any({c})"
    if dtype == "VARCHAR[]":
        # Arrays arrive both as JSON text '["C-1","L-2"]' and as a bare value 'C-1' (schema drift)
        return (f"CASE WHEN {c} IS NULL THEN NULL "
                f"WHEN starts_with(trim({c}), '[') THEN try_cast(json({c}) AS VARCHAR[]) "
                f"ELSE string_split(regexp_replace({c}, '[\\s]', '', 'g'), ',') END")
    return c


def build_curated(tables=None):
    schema = json.loads(SCHEMA_JSON.read_text())
    con = connect()
    normalize.install(con)
    con.execute(f"ATTACH IF NOT EXISTS '{BRONZE_DB}' AS bronze (READ_ONLY)")
    con.execute("CREATE SCHEMA IF NOT EXISTS curated")
    con.execute("""CREATE TABLE IF NOT EXISTS curated._cast_failures(
        table_name VARCHAR, column_name VARCHAR, declared_type VARCHAR,
        rows_non_null BIGINT, cast_failures BIGINT, non_iso_values BIGINT, sample_bad VARCHAR)""")
    con.execute("CREATE TABLE IF NOT EXISTS curated._column_docs(table_name VARCHAR, column_name VARCHAR, type VARCHAR, description VARCHAR)")

    for name, spec in schema.items():
        if tables and name not in tables:
            continue
        t0 = time.time()
        phys = {r[0]: r[1] for r in con.execute(f"DESCRIBE bronze.{name}").fetchall()}
        selects, checks = [], []
        for col in spec["columns"]:
            cn = col["name"]
            if cn not in phys:
                continue
            dt = _duck_type(col["type"])
            expr = _cast_expr(cn, dt, phys[cn])
            selects.append(f'{expr} AS "{cn}"')
            if phys[cn] == "VARCHAR" and dt != "VARCHAR":
                iso = _ISO_DATE if dt == "DATE" else _ISO_TS if dt == "TIMESTAMP" else None
                non_iso = f"count(*) FILTER (WHERE raw IS NOT NULL AND NOT regexp_matches(raw, '{iso}'))" if iso else "0"
                checks.append((cn, col["type"], expr, non_iso))
        # keep any undeclared raw columns (schema drift) untouched
        declared = {c["name"] for c in spec["columns"]}
        for cn in phys:
            if cn not in declared and not cn.startswith("_") and cn != "year":
                selects.append(f'"{cn}"')
        typed_sql = f"SELECT {', '.join(selects)}, _src_file, _load_ts FROM bronze.{name}"
        derived = DERIVED.get(name, [])
        if derived:
            extra = ", ".join(f"{e} AS {n}" for e, n in derived)
            sql = f"SELECT *, {extra} FROM ({typed_sql})"
        else:
            sql = typed_sql
        con.execute(f"CREATE OR REPLACE TABLE curated.{name} AS {sql}")

        # cast-failure accounting (one pass per table)
        con.execute("DELETE FROM curated._cast_failures WHERE table_name = ?", [name])
        if checks:
            parts = []
            for cn, decl, expr, non_iso in checks:
                parts.append(f"""SELECT '{name}' t, '{cn}' c, '{decl}' d,
                    count(raw) AS nn,
                    count(*) FILTER (WHERE raw IS NOT NULL AND typed IS NULL) AS bad,
                    {non_iso} AS noniso,
                    any_value(raw) FILTER (WHERE raw IS NOT NULL AND typed IS NULL) AS sample_bad
                    FROM (SELECT "{cn}" AS raw, {expr} AS typed FROM bronze.{name})""")
            con.execute("INSERT INTO curated._cast_failures " + " UNION ALL ".join(parts))
        con.execute("DELETE FROM curated._column_docs WHERE table_name = ?", [name])
        con.executemany("INSERT INTO curated._column_docs VALUES (?,?,?,?)",
                        [(name, c["name"], c["type"], c.get("description") or "") for c in spec["columns"]]
                        + [(name, n, "derived", f"Layer 1 derived: {e}") for e, n in derived])
        n = con.execute(f"SELECT count(*) FROM curated.{name}").fetchone()[0]
        print(f"  curated.{name:<26} {n:>12,} rows  ({time.time() - t0:5.1f}s)", flush=True)
    con.close()


if __name__ == "__main__":
    import sys
    build_curated(sys.argv[1:] or None)
