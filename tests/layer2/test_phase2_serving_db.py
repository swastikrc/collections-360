"""Phase 2: the serving database is allow-listed, materialised, locked down and atomically switched."""
import copy

import duckdb
import pytest
import yaml

from c360.layer2 import config, deny_list, publish, serving as serving_mod


def columns_of(path):
    con = duckdb.connect(str(path), read_only=True)
    rows = con.execute("""SELECT table_schema, table_name, column_name FROM information_schema.columns
                          WHERE table_schema NOT IN ('information_schema', 'pg_catalog')""").fetchall()
    con.close()
    return rows


def test_publish_creates_the_file_and_the_pointer(serving_path):
    l2_dir = config.settings().l2_dir
    assert serving_path.exists() and serving_path.parent == l2_dir
    assert serving_path.name.startswith("l2_") and serving_path.suffix == ".duckdb"
    assert (l2_dir / "CURRENT").read_text().strip() == serving_path.name
    assert not list(l2_dir.glob("*.tmp*"))


def test_no_column_in_the_serving_database_is_deny_listed(serving_path):
    cols = columns_of(serving_path)
    assert len(cols) > 300
    assert [c for c in cols if deny_list.is_denied(c[2])] == []


def test_protected_and_personal_source_columns_did_not_come_through(serving_path, world):
    from c360.layer1.golden import PROTECTED_COLUMNS
    names = {c for _, _, c in columns_of(serving_path)}
    src = duckdb.connect(str(world["db"]), read_only=True)
    source_names = {r[0] for r in src.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'curated'").fetchall()}
    src.close()
    for col in PROTECTED_COLUMNS + ["first_name", "last_name", "date_of_birth", "age", "primary_phone_std", "email_std",
                                    "address_line1", "postal_code_std", "national_id_hash", "cardholder_phone_raw",
                                    "subject_name_reported", "employer_name", "city"]:
        assert col in source_names, f"{col} should exist in the synthetic source (the trap)"
        assert col not in names, f"{col} leaked into the serving database"
    for col in ("src_customer_ref", "co_borrower_ref", "joint_holder_ref", "cif", "coll_customer_ref", "crm_customer_id",
                "file_path", "voice_sample_id", "bank_subject_ref"):
        assert col not in names, f"{col} should not be copied"


def test_the_allow_list_is_the_only_way_in(serving_path):
    views = publish.load_views()
    actual: dict = {}
    for schema, table, col in columns_of(serving_path):
        if schema == "l2":
            actual.setdefault(table, set()).add(col)
    # One table is built by code, not from l2_views.yaml: the reference documents (not customer data).
    # It must have exactly its fixed column list; every other table must match the allow-list exactly.
    from c360.layer2 import policies
    code_built = {"policy_sections": set(policies.COLUMNS)}
    assert set(actual) == set(views) | set(code_built)
    for table, view in views.items():
        assert actual[table] == set(view["columns"]), table
    for table, cols in code_built.items():
        assert actual[table] == cols, table
    for name, view in views.items():
        assert "*" not in (view.get("source") or "").replace("count(*)", ""), f"{name}: SELECT * is not allowed in a source"


def test_person_linked_tables_carry_c360_id(serving):
    linked = [t for t, cols in serving.columns.items() if "c360_id" in cols]
    assert {"customers", "customer_summary", "card_accounts", "loan_accounts", "deposit_accounts", "account_monthly_snapshot",
            "collections_cases", "promises_to_pay", "offers", "contact_history", "agent_notes", "call_transcripts",
            "bureau_latest", "bureau_history", "salary_credit_history", "model_scores"} <= set(linked)
    for table in linked:
        _, rows = serving.execute(f"SELECT count(c360_id), count(*) FROM l2.{table}")
        linked_rows, total = rows[0]
        assert total > 0 and linked_rows / total >= 0.95, (table, linked_rows, total)


def test_meta_tables(serving, world):
    b = serving.build
    assert b["build_ts"] in serving.path.name
    assert str(b["snapshot_date"]) == "2026-09-28" and str(serving.snapshot_date) == "2026-09-28"
    assert b["pipeline_version"] == "layer1-1.0.0"
    assert b["l2_version"] == config.L2_VERSION and b["text_search_mode"] == "fts"
    assert b["source_db_path"] == str(world["db"]) and b["source_db_mtime"] is not None
    assert serving.row_counts["customers"] == 2000 and serving.row_counts["customer_summary"] == 2000
    for table, n in serving.row_counts.items():
        assert serving.execute(f"SELECT count(*) FROM l2.{table}")[1][0][0] == n
    assert serving.columns["promises_to_pay"]["ptp_status"]["type"] == "VARCHAR"
    assert serving.columns["collections_cases"]["account_ids"]["type"] == "VARCHAR[]"


def test_joins_do_not_multiply_or_drop_rows(serving, world):
    src = duckdb.connect(str(world["db"]), read_only=True)
    for l2_table, source in [("card_accounts", "curated.card_accounts"), ("loan_accounts", "curated.loan_accounts"),
                             ("deposit_accounts", "curated.deposit_accounts"), ("collections_cases", "curated.collections_cases"),
                             ("promises_to_pay", "curated.promises_to_pay"), ("offers", "curated.offers"),
                             ("contact_history", "curated.contact_history"), ("agent_notes", "curated.agent_notes"),
                             ("call_transcripts", "curated.call_transcripts"), ("model_scores", "curated.model_scores"),
                             ("account_monthly_snapshot", "curated.account_monthly_snapshot"),
                             ("salary_credit_history", "curated.salary_credit_history"), ("bureau_history", "curated.bureau_history")]:
        assert serving.row_counts[l2_table] == src.execute(f"SELECT count(*) FROM {source}").fetchone()[0], l2_table
    assert serving.row_counts["dq_results"] == src.execute(
        "SELECT count(*) FROM golden.dq_results WHERE run_ts = (SELECT max(run_ts) FROM golden.dq_results)").fetchone()[0]
    src.close()


def test_derived_columns(serving):
    _, rows = serving.execute("SELECT count(*) FILTER (WHERE is_duplicate), count(*) FILTER (WHERE dq_duplicate_of IS NOT NULL), count(*) FILTER (WHERE channel IS NULL) FROM l2.contact_history")
    assert rows[0][0] == rows[0][1] > 0 and rows[0][2] == 0
    assert serving.execute("SELECT count(*) FROM l2.loan_accounts WHERE has_co_borrower")[1][0][0] > 0
    assert serving.execute("SELECT count(*) FROM l2.bureau_latest WHERE dq_shifted_row AND inquiries_hard_3m IS NOT NULL")[1][0][0] == 0
    assert serving.execute("SELECT count(*) FROM l2.customers WHERE crm_record_count > 1")[1][0][0] > 0


def test_transcript_text_is_parsed_and_missing_files_are_null(serving):
    _, rows = serving.execute("SELECT count(transcript_text), count(*) FROM l2.call_transcripts")
    with_text, total = rows[0]
    assert total == 100 and 80 <= with_text < 100
    text = serving.execute("SELECT transcript_text FROM l2.call_transcripts WHERE transcript_text IS NOT NULL LIMIT 1")[1][0][0]
    assert text.startswith("agent: Thank you for calling") and "\ncustomer: " in text


def test_full_text_index_finds_hardship_notes(serving):
    assert serving.text_mode == "fts"
    _, rows = serving.execute("""SELECT n.note_id, n.note_text, fts_l2_agent_notes.match_bm25(n.note_id, 'lost job') AS score
                                 FROM l2.agent_notes n WHERE score IS NOT NULL ORDER BY score DESC LIMIT 5""")
    assert len(rows) == 5 and all("job" in r[1] for r in rows)
    _, rows = serving.execute("""SELECT t.transcript_id FROM l2.call_transcripts t
                                 WHERE fts_l2_call_transcripts.match_bm25(t.transcript_id, 'employment insurance') IS NOT NULL""")
    assert rows


def test_like_mode_can_be_forced(world, tmp_path):
    path = publish.publish_l2(quiet=True, text_mode="like", l2_dir=tmp_path / "l2_like")
    s = serving_mod.Serving(path)
    try:
        assert s.build["text_search_mode"] == "like" and s.text_mode == "like"
        schemas = {r[0] for r in s.execute("SELECT schema_name FROM information_schema.schemata")[1]}
        assert not [x for x in schemas if x.startswith("fts_")]
        assert s.execute("SELECT count(*) FROM l2.agent_notes WHERE note_text ILIKE '%lost my job%'")[1][0][0] > 0
    finally:
        s.close()


def test_publishing_again_keeps_two_files_and_moves_the_pointer(world, tmp_path):
    l2_dir = tmp_path / "l2_rotate"
    paths = [publish.publish_l2(quiet=True, l2_dir=l2_dir) for _ in range(3)]
    files = sorted(p.name for p in l2_dir.glob("l2_*.duckdb"))
    assert files == sorted(p.name for p in paths[1:])
    assert (l2_dir / "CURRENT").read_text().strip() == paths[-1].name
    assert serving_mod.current_path(l2_dir) == paths[-1]
    assert not list(l2_dir.glob("*.tmp*"))


def test_publish_aborts_when_a_deny_listed_column_is_allow_listed(world, tmp_path):
    views = copy.deepcopy(publish.load_views())
    views["customers"]["columns"]["contact_number"] = "arg_max(c.primary_phone_std, c.last_profile_update_ts)"
    views["customers"]["columns"]["date_of_birth"] = "any_value(c.date_of_birth)"
    bad = tmp_path / "views.yaml"
    bad.write_text(yaml.safe_dump(views, sort_keys=False))
    l2_dir = tmp_path / "l2_bad"
    with pytest.raises(publish.PublishError, match="doctor found"):
        publish.publish_l2(quiet=True, l2_dir=l2_dir, views_path=bad)
    assert not list(l2_dir.glob("l2_*")) and not (l2_dir / "CURRENT").exists()


def test_publish_final_deny_check_is_independent_of_the_doctor(world, tmp_path, monkeypatch):
    """Even if the doctor were bypassed, a deny-listed column name in the built file stops the publish."""
    from c360.layer2 import doctor
    views = copy.deepcopy(publish.load_views())
    views["agents"]["columns"]["agent_name"] = {"expr": "ag.site", "optional": True}
    bad = tmp_path / "views2.yaml"
    bad.write_text(yaml.safe_dump(views, sort_keys=False))
    monkeypatch.setattr(doctor, "check", lambda **kw: doctor.Report())
    l2_dir = tmp_path / "l2_bad2"
    with pytest.raises(publish.PublishError, match="deny-listed column"):
        publish.publish_l2(quiet=True, l2_dir=l2_dir, views_path=bad)
    assert not list(l2_dir.glob("l2_*")) and not (l2_dir / "CURRENT").exists()


def test_publish_needs_the_layer1_database(tmp_path):
    with pytest.raises(publish.PublishError, match="not found"):
        publish.publish_l2(quiet=True, l2_dir=tmp_path / "x", source_db=tmp_path / "missing.duckdb")


def test_optional_column_missing_in_source_is_skipped_not_fatal(world, tmp_path):
    views = copy.deepcopy(publish.load_views())
    views["agents"]["columns"]["not_in_source"] = {"expr": "ag.not_in_source", "optional": True}
    views["made_up_reference"] = {"grain": "x", "description": "x", "required": False, "confidence": "inferred",
                                  "source": "SELECT {cols} FROM src.curated.does_not_exist d", "columns": {"a": {"expr": "d.a", "optional": True}}}
    path = tmp_path / "views3.yaml"
    path.write_text(yaml.safe_dump(views, sort_keys=False))
    out = publish.publish_l2(quiet=True, l2_dir=tmp_path / "l2_opt", views_path=path)
    cols = {(t, c) for s, t, c in columns_of(out) if s == "l2"}
    assert ("agents", "team_id") in cols and ("agents", "not_in_source") not in cols
    assert not [c for c in cols if c[0] == "made_up_reference"]


# ---- the runtime connection ------------------------------------------------------------------------
def test_serving_connection_is_read_only(serving):
    for sql in ("CREATE TABLE l2.x AS SELECT 1", "INSERT INTO l2.promises_to_pay SELECT * FROM l2.promises_to_pay",
                "DELETE FROM l2.promises_to_pay", "DROP TABLE l2.customers", "UPDATE l2.customers SET segment = 'x'"):
        with pytest.raises(Exception, match="(?i)read-only|read only"):
            serving.execute(sql)


def test_serving_connection_cannot_read_files_even_without_the_guard(serving, world, tmp_path):
    victim = tmp_path / "secret.csv"
    victim.write_text("a,b\n1,2\n")
    for sql in (f"SELECT * FROM read_csv('{victim}')", f"SELECT * FROM '{victim}'",
                f"SELECT * FROM read_text('{victim}')", f"ATTACH '{world['db']}' AS stolen (READ_ONLY)",
                f"COPY (SELECT 1) TO '{tmp_path / 'out.csv'}'", "INSTALL httpfs", "LOAD httpfs"):
        with pytest.raises(Exception):
            serving.execute(sql)
    assert not (tmp_path / "out.csv").exists()


def test_serving_configuration_is_locked(serving):
    with pytest.raises(Exception, match="(?i)lock|cannot change"):
        serving.execute("SET enable_external_access = true")


def test_serving_cannot_see_the_layer1_database(serving):
    with pytest.raises(Exception, match="(?i)does not exist|not found"):
        serving.execute("SELECT count(*) FROM curated.customers")
    catalogs = {r[0] for r in serving.execute("SELECT DISTINCT table_catalog FROM information_schema.tables")[1]}
    assert not [c for c in catalogs if c in ("src", "c360")]


def test_serving_query_timeout(serving):
    with pytest.raises(serving_mod.QueryTimeout):
        serving.execute("SELECT count(*) FROM range(100000000) a, range(100000) b", timeout=0.3)
    assert serving.execute("SELECT 1")[1] == [(1,)]


def test_missing_pointer_gives_a_helpful_error(tmp_path):
    with pytest.raises(serving_mod.ServingError, match="publish_l2"):
        serving_mod.current_path(tmp_path)
