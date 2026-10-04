"""Phase 1: the synthetic world matches the expected schema and contains every planted trap."""
import json

import duckdb
import pytest
import yaml

from c360.layer2 import schema_util
from c360.layer2.synth import generate as gen


@pytest.fixture(scope="module")
def con(world):
    c = duckdb.connect(str(world["db"]), read_only=True)
    yield c
    c.close()


def one(con, sql):
    return con.execute(sql).fetchone()[0]


def test_synth_is_fast(world):
    assert world["seconds"] < 120


def test_every_expected_table_and_column_exists(con):
    actual = {(s, t, c): typ for s, t, c, typ in con.execute(
        "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns").fetchall()}
    missing, wrong = [], []
    for col in schema_util.expected_columns():
        key = (col.schema, col.table, col.column)
        if key not in actual:
            missing.append(".".join(key))
        elif not schema_util.compatible(col.type, actual[key]):
            wrong.append((".".join(key), col.type, actual[key]))
    assert not missing, missing
    assert not wrong, wrong


def test_scale_is_close_to_the_spec(world):
    c = world["counts"]
    assert c["mongo.customers"] == 2000
    assert 2020 <= c["curated.customers"] <= 2070
    assert 950 <= c["curated.card_accounts"] <= 1250
    assert 600 <= c["curated.loan_accounts"] <= 800
    assert 1400 <= c["curated.deposit_accounts"] <= 1800
    assert 650 <= c["curated.collections_cases"] <= 950
    assert 5000 <= c["curated.contact_history"] <= 8000
    assert 380 <= c["curated.promises_to_pay"] <= 620
    assert 200 <= c["curated.offers"] <= 400
    assert 1600 <= c["curated.agent_notes"] <= 2500
    assert c["curated.call_transcripts"] == 100
    assert one(duckdb.connect(str(world["db"]), read_only=True),
               "SELECT count(DISTINCT month_end_date) FROM curated.account_monthly_snapshot") == 12


def test_documents_load_into_mongomock_and_resolve_by_any_key(world, mock_db, con):
    from c360.mongo import CUSTOMER_VALIDATOR
    from c360.store import C360Store
    assert mock_db.customers.count_documents({}) == 2000
    store = C360Store(database=mock_db)
    case_id, c360_id = con.execute(
        "SELECT case_id, c360_id FROM golden.case_xref WHERE c360_id IS NOT NULL ORDER BY case_id LIMIT 1").fetchone()
    doc = store.get(case_id)
    assert doc and doc["_id"] == c360_id
    for field in CUSTOMER_VALIDATOR["$jsonSchema"]["required"]:
        assert field in doc, field
    assert doc["identity"]["crm_ids"] and "financial_hardship_flag" in doc["hardship_indicators"]
    card = con.execute("SELECT account_id FROM golden.account_xref WHERE product_family = 'card' AND c360_id IS NOT NULL LIMIT 1").fetchone()[0]
    assert store.get(card) is not None
    assert store.get(doc["_id"])["_id"] == doc["_id"]


def test_trap_duplicate_contacts(con):
    dup, total = con.execute("SELECT count(*) FILTER (WHERE dq_duplicate_of IS NOT NULL), count(*) FROM curated.contact_history").fetchone()
    assert 0.03 <= dup / total <= 0.08


def test_trap_stored_bucket_disagrees_with_dpd(con):
    for table in ("card_accounts", "loan_accounts", "collections_cases", "account_monthly_snapshot"):
        assert one(con, f"SELECT count(*) FROM curated.{table} WHERE dq_bucket_mismatch") > 0, table


def test_trap_protected_and_personal_columns_are_populated_in_curated(con):
    from c360.layer1.golden import PROTECTED_COLUMNS
    for col in PROTECTED_COLUMNS + ["first_name", "last_name", "date_of_birth", "primary_phone_raw", "primary_phone_std",
                                    "email", "email_std", "address_line1", "postal_code", "national_id_hash"]:
        assert one(con, f"SELECT count({col}) FROM curated.customers") > 0, col
    assert one(con, "SELECT count(cardholder_phone_raw) FROM curated.card_accounts") > 0
    assert one(con, "SELECT count(subject_name_reported) FROM curated.external") > 0


def test_trap_small_groups(con):
    assert 0 < one(con, f"SELECT count(*) FROM curated.collections_cases WHERE queue = '{gen.RARE_QUEUE}'") < 5
    assert 0 < one(con, f"SELECT count(*) FROM curated.collections_cases WHERE hardship_reason = '{gen.RARE_HARDSHIP_REASON}'") < 5


def test_trap_superseded_scores_and_shifted_bureau_rows(con):
    assert one(con, "SELECT count(*) FROM curated.model_scores WHERE dq_superseded_duplicate") > 0
    assert one(con, "SELECT count(*) FROM curated.external WHERE dq_shifted_row") > 0
    assert one(con, "SELECT count(*) FROM curated.external WHERE dq_shifted_row AND inquiries_hard_3m_std IS NOT NULL") == 0


def test_trap_repaired_and_unmatched_case_links(con):
    methods = dict(con.execute("SELECT link_method, count(*) FROM golden.case_xref GROUP BY ALL").fetchall())
    assert methods["crm_id_exact"] > 600
    assert methods.get("repaired_account_owner_and_id_edit", 0) > 0
    assert 0 < methods.get("unmatched", 0) < 20
    assert one(con, "SELECT count(*) FROM golden.case_xref WHERE link_method = 'unmatched' AND c360_id IS NOT NULL") == 0


def test_trap_note_language_and_prompt_injection(con):
    for phrase in ("lost my job", "off work sick", "reduced hours", "dispute"):
        assert one(con, f"SELECT count(*) FROM curated.agent_notes WHERE note_text ILIKE '%{phrase}%'") > 0, phrase
    for text in gen.INJECTIONS:
        needle = text.replace("'", "''")
        assert one(con, f"SELECT count(*) FROM curated.agent_notes WHERE contains(note_text, '{needle}')") == 1, text
    assert len(gen.INJECTIONS) >= 3


def test_trap_missing_transcript_files_and_an_injected_transcript(world, con):
    paths = [r[0] for r in con.execute("SELECT file_path FROM curated.call_transcripts").fetchall()]
    existing = [p for p in paths if (world["data_root"] / p).exists()]
    assert 0 < len(paths) - len(existing) < 20
    payload = json.loads((world["data_root"] / existing[0]).read_text())
    assert payload["turns"][0]["speaker"] == "agent" and payload["turns"][0]["text"]
    texts = "".join((world["data_root"] / p).read_text() for p in existing)
    assert gen.TRANSCRIPT_INJECTION in texts


def test_trap_channel_rename_and_schema_drift(con):
    assert one(con, "SELECT count(*) FROM curated.contact_history WHERE dq_channel_renamed") > 0
    assert one(con, "SELECT count(*) FROM curated.contact_history WHERE channel_std IS NULL") == 0


def test_account_xref_has_exactly_one_primary_row_per_account(con):
    assert one(con, """SELECT count(*) FROM (SELECT account_id FROM golden.account_xref WHERE role = 'primary'
                       GROUP BY 1 HAVING count(*) > 1)""") == 0


def test_synthetic_questions_have_expected_answers(world):
    questions = yaml.safe_load(world["questions"].read_text())
    assert len(questions) >= 30
    assert len({q["id"] for q in questions}) == len(questions)
    for q in questions:
        assert q["question"] and q["expected"]["kind"] in ("scalar", "table", "set", "contains", "manual", "refusal"), q["id"]
        if q["expected"]["kind"] == "scalar":
            assert q["expected"]["value"] is not None, q["id"]
        if q["expected"]["kind"] == "table":
            assert q["expected"]["rows"], q["id"]
    kinds = {q["expected"]["kind"] for q in questions}
    assert {"scalar", "table", "contains"} <= kinds


def test_generation_is_deterministic(tmp_path):
    a = gen.generate(out=tmp_path / "a", people=250, seed=11, questions_out=tmp_path / "a.yaml", quiet=True)
    b = gen.generate(out=tmp_path / "b", people=250, seed=11, questions_out=tmp_path / "b.yaml", quiet=True)
    assert a["counts"] == b["counts"]
    assert (tmp_path / "a.yaml").read_text() == (tmp_path / "b.yaml").read_text()
    c = gen.generate(out=tmp_path / "c", people=250, seed=12, questions_out=tmp_path / "c.yaml", quiet=True)
    assert c["counts"] != a["counts"]


def test_generator_refuses_to_overwrite_a_real_warehouse(tmp_path):
    real = tmp_path / "warehouse"
    real.mkdir()
    (real / "c360.duckdb").write_bytes(b"not a synthetic database")
    with pytest.raises(SystemExit, match="Refusing to overwrite"):
        gen.generate(out=real, people=50, seed=1, questions_out=tmp_path / "q.yaml", quiet=True)
    assert (real / "c360.duckdb").read_bytes() == b"not a synthetic database"
