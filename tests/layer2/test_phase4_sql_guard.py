"""Phase 4: the SQL validator. Allowed queries pass; every rejection rule and trap fires with its message."""
import pytest

from c360.layer2.tools import sql_guard
from c360.layer2.tools.sql_guard import validate


@pytest.fixture(scope="module")
def g(serving):
    from c360.layer2 import config
    return sql_guard.GuardContext.from_serving(serving, config.load_yaml(config.SEMANTIC_YAML), 50)


def rules(result):
    return [i.rule for i in result.issues if i.severity == "error"]


def messages(result):
    return " ".join(i.message for i in result.issues)


ALLOWED = [
    "SELECT count(*) AS n FROM l2.collections_cases WHERE is_open",
    "SELECT queue, count(*) AS n FROM l2.collections_cases GROUP BY queue ORDER BY n DESC",
    "SELECT queue, count(*) AS cases FROM collections_cases GROUP BY ALL",
    "WITH open_cases AS (SELECT case_id, queue FROM l2.collections_cases WHERE is_open) SELECT queue, count(*) AS n FROM open_cases GROUP BY 1",
    "SELECT c.queue, count(DISTINCT p.ptp_id) AS promises FROM l2.promises_to_pay p JOIN l2.collections_cases c ON c.case_id = p.case_id GROUP BY 1",
    "SELECT channel, count(*) AS contacts FROM l2.contact_history WHERE is_duplicate = false GROUP BY channel",
    "SELECT count(*) AS dupes FROM l2.contact_history WHERE dq_duplicate_of IS NOT NULL",
    "SELECT model_name, avg(score) AS avg_score FROM l2.model_scores WHERE dq_superseded_duplicate = false GROUP BY 1",
    "SELECT strftime(date_trunc('month', ptp_due_date), '%Y-%m') AS month, count(*) AS n FROM l2.promises_to_pay WHERE ptp_due_date >= DATE '2026-09-28' - INTERVAL 90 DAY GROUP BY 1 ORDER BY 1",
    "SELECT segment, median(declared_annual_income) AS med FROM l2.customers GROUP BY segment",
    "SELECT case_id, queue, current_bucket FROM l2.collections_cases WHERE is_open ORDER BY total_overdue DESC LIMIT 10",
    "SELECT count(*) AS n FROM l2.agent_notes WHERE note_text ILIKE '%lost my job%'",
    "SELECT note_type, count(*) FILTER (WHERE note_text ILIKE '%dispute%') AS dispute_notes FROM l2.agent_notes GROUP BY 1",
    "SELECT product_type, count(*) AS n FROM l2.loan_accounts WHERE secured_flag = false GROUP BY 1 UNION ALL SELECT 'all', count(*) FROM l2.loan_accounts",
    "SELECT queue, n FROM (SELECT queue, count(*) AS n FROM l2.collections_cases GROUP BY 1) t WHERE n > 5",
    "SELECT * FROM l2.code_lookups",
    "SELECT * FROM l2.dq_results ORDER BY pct DESC",
    "SELECT c360_id, row_number() OVER (ORDER BY past_due_total DESC) AS rk FROM l2.customer_summary QUALIFY rk <= 5",
    "SELECT CAST(count(*) FILTER (WHERE cure_flag) AS DOUBLE) / nullif(count(*), 0) AS cure_rate FROM l2.collections_cases WHERE outcome IS NOT NULL",
    "SELECT n.note_type, count(*) AS n FROM l2.agent_notes n GROUP BY 1 ORDER BY n DESC",
    "(SELECT count(*) AS n FROM l2.offers)",
    "SELECT account_id, count(*) AS n FROM (SELECT unnest(account_ids) AS account_id FROM l2.collections_cases) t GROUP BY 1 ORDER BY n DESC LIMIT 5",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed_queries_pass_and_run(g, serving, sql):
    result = validate(sql, g)
    assert result.ok, messages(result)
    serving.execute(result.sql)          # the rewritten SQL is valid DuckDB


def test_data_quality_purpose_unlocks_recomputed_columns(g, serving):
    sql = "SELECT count(*) AS n FROM l2.card_accounts WHERE dpd_bucket <> dpd_bucket_recomputed"
    assert rules(validate(sql, g)) == ["T3"]
    ok = validate(sql, g, purpose="data_quality")
    assert ok.ok and serving.execute(ok.sql)[1][0][0] > 0


def test_unqualified_tables_are_rewritten_to_l2(g):
    result = validate("SELECT count(*) AS n FROM offers o JOIN collections_cases c ON c.case_id = o.case_id", g)
    assert result.ok and "l2.offers" in result.sql and "l2.collections_cases" in result.sql


def test_limit_is_added_and_capped(g):
    assert validate("SELECT queue FROM l2.collections_cases", g).sql.endswith("LIMIT 51")
    assert validate("SELECT queue FROM l2.collections_cases LIMIT 10", g).sql.endswith("LIMIT 10")
    assert validate("SELECT queue FROM l2.collections_cases LIMIT 5000", g).sql.endswith("LIMIT 51")
    assert validate("SELECT queue FROM l2.collections_cases UNION ALL SELECT queue FROM l2.collections_cases", g).sql.endswith("LIMIT 51")


REJECTED = [
    # R1: one SELECT statement only
    ("", "R1", "empty"),
    ("SELECT 1; SELECT 2", "R1", "exactly one"),
    ("SELECT count(*) FROM l2.offers; DROP TABLE l2.offers", "R1", "exactly one"),
    ("DROP TABLE l2.customers", "R1", "Only SELECT"),
    ("DELETE FROM l2.promises_to_pay", "R1", "Only SELECT"),
    ("UPDATE l2.customers SET segment = 'x'", "R1", "Only SELECT"),
    ("INSERT INTO l2.offers SELECT * FROM l2.offers", "R1", "Only SELECT"),
    ("CREATE TABLE l2.x AS SELECT 1", "R1", "Only SELECT"),
    ("ATTACH '/tmp/x.duckdb' AS y", "R1", "Only SELECT"),
    ("COPY l2.customers TO '/tmp/out.csv'", "R1", "Only SELECT"),
    ("COPY (SELECT segment FROM l2.customers) TO '/tmp/out.csv'", "R1", "Only SELECT"),
    ("PRAGMA database_list", "R1", "Only SELECT"),
    ("PRAGMA table_info('l2.customers')", "R1", "Only SELECT"),
    ("SET enable_external_access = true", "R1", "Only SELECT"),
    ("INSTALL httpfs", "R1", "Only SELECT"),
    ("DESCRIBE l2.customers", "R1", "Only SELECT"),
    ("SHOW TABLES", "R1", "Only SELECT"),
    ("SUMMARIZE l2.customers", "R1", "Only SELECT"),
    ("CALL pragma_version()", "R1", "Only SELECT"),
    ("SELECT segment INTO stolen FROM l2.customers", "R1", "INTO"),
    ("SELEC count(*) FRM l2.offers WHERE", "R1", ""),
    # R2: schema l2 only
    ("SELECT count(*) FROM curated.customers", "R2", "only schema `l2`"),
    ("SELECT first_name FROM curated.customers", "R2", "only schema `l2`"),
    ("SELECT count(*) FROM golden.crm_person_map", "R2", "only schema `l2`"),
    ("SELECT count(*) FROM src.curated.customers", "R2", "only tables in schema `l2`"),
    ("SELECT * FROM meta.build", "R2", "only schema `l2`"),
    ("SELECT count(*) FROM information_schema.tables", "R2", "only schema `l2`"),
    ("SELECT count(*) FROM l2.secret_table", "R2", "no table `l2.secret_table`"),
    ("SELECT count(*) FROM payments", "R2", "Unknown table `payments`"),
    ("SELECT count(*) FROM l2.offers WHERE case_id IN (SELECT case_id FROM curated.collections_cases)", "R2", "only schema `l2`"),
    ("WITH customers AS (SELECT 1 AS x) SELECT x FROM customers", "R2", "CTE cannot be named"),
    ("SELECT count(*) FROM '/etc/passwd'", "R2", "Unknown table"),
    # R3: table, file and system functions
    ("SELECT * FROM read_csv('/etc/passwd')", "R3", "read_csv"),
    ("SELECT * FROM read_parquet('s3://bucket/x.parquet')", "R3", "read_parquet"),
    ("SELECT * FROM read_json_auto('/tmp/x.json')", "R3", "Table functions"),
    ("SELECT * FROM glob('/home/*')", "R3", "glob"),
    ("SELECT * FROM query_table('l2.customers')", "R3", "query_table"),
    ("SELECT * FROM duckdb_tables()", "R3", "duckdb_tables"),
    ("SELECT * FROM duckdb_settings()", "R3", "duckdb_settings"),
    ("SELECT * FROM pragma_table_info('l2.customers')", "R3", "pragma_table_info"),
    ("SELECT getenv('GEMINI_API_KEY') AS k FROM l2.offers", "R3", "getenv"),
    ("SELECT current_setting('access_mode') AS s FROM l2.offers", "R3", "current_setting"),
    ("SELECT read_text('/etc/passwd') AS t FROM l2.offers", "R3", "read_text"),
    ("SELECT segment FROM l2.customers c, range(1000000) r", "R3", "Table functions"),
    ("SELECT some_new_function(segment) AS s FROM l2.customers", "R3", "not on the allowed list"),
    ("SELECT COLUMNS('.*') FROM l2.code_lookups", "R3", "columns"),
    ("SELECT current_database() AS d FROM l2.offers", "R3", "current_database"),
    # R4: SELECT * and whole rows on row-level tables
    ("SELECT * FROM l2.customers", "R4", "SELECT * is not allowed on l2.customers"),
    ("SELECT * FROM customer_summary LIMIT 5", "R4", "l2.customer_summary"),
    ("SELECT c.* FROM l2.card_accounts c", "R4", "l2.card_accounts"),
    ("SELECT * EXCLUDE (segment) FROM l2.customers", "R4", "l2.customers"),
    ("SELECT p.*, c.queue FROM l2.promises_to_pay p JOIN l2.collections_cases c USING (case_id)", "R4", "l2.promises_to_pay"),
    ("SELECT count(*) FROM (SELECT * FROM l2.loan_accounts)", "R4", "l2.loan_accounts"),
    ("SELECT c FROM l2.customers c", "R4", "whole row"),
    ("SELECT customers FROM l2.customers", "R4", "whole row"),
    # R5: free text only through search_text
    ("SELECT note_text FROM l2.agent_notes", "R5", "search_text"),
    ("SELECT note_id, note_text FROM l2.agent_notes WHERE case_id = 'CS-2026-000001'", "R5", "search_text"),
    ("SELECT transcript_text FROM l2.call_transcripts LIMIT 1", "R5", "search_text"),
    ("SELECT max(note_text) AS t FROM l2.agent_notes", "R5", "search_text"),
    ("SELECT string_agg(n.note_text, ' | ') AS t FROM l2.agent_notes n", "R5", "search_text"),
    ("SELECT substr(note_text, 1, 100) AS t FROM l2.agent_notes", "R5", "search_text"),
    ("SELECT note_type, count(*) AS n FROM l2.agent_notes GROUP BY note_type, note_text", "R5", "search_text"),
    ("SELECT note_id FROM l2.agent_notes ORDER BY note_text", "R5", "search_text"),
    ("SELECT x FROM (SELECT note_text AS x FROM l2.agent_notes) t", "R5", "search_text"),
    ("SELECT count(*) FROM l2.offers WHERE offer_id IN (SELECT note_text FROM l2.agent_notes)", "R5", "search_text"),
    # R6: deny-listed names
    ("SELECT first_name, last_name FROM l2.customers", "R6", "personal or protected"),
    ("SELECT primary_phone_std FROM l2.customers", "R6", "personal or protected"),
    ("SELECT gender_code, count(*) FROM l2.customers GROUP BY 1", "R6", "protected attribute"),
    ("SELECT count(*) FROM l2.customers WHERE date_of_birth < DATE '1960-01-01'", "R6", "personal or protected"),
    ("SELECT postal_code, count(*) FROM l2.customers GROUP BY 1", "R6", "personal or protected"),
    # traps
    ("SELECT count(*) AS n FROM l2.contact_history WHERE direction = 'outbound'", "T1", "is_duplicate = false"),
    ("SELECT c.queue, count(DISTINCT h.contact_id) AS n FROM l2.collections_cases c JOIN l2.contact_history h USING (case_id) GROUP BY 1", "T1", "double-loaded"),
    ("SELECT count(*) AS n FROM l2.promises_to_pay WHERE ptp_due_date >= current_date - INTERVAL 90 DAY", "T2", "DATE '2026-09-28'"),
    ("SELECT count(*) AS n FROM l2.promises_to_pay WHERE ptp_created_ts >= now() - INTERVAL 30 DAY", "T2", "snapshot date"),
    ("SELECT count(*) AS n FROM l2.promises_to_pay WHERE ptp_due_date >= today() - 7", "T2", "snapshot date"),
    ("SELECT count(*) AS n FROM l2.promises_to_pay WHERE ptp_created_ts >= CURRENT_TIMESTAMP - INTERVAL 1 DAY", "T2", "snapshot date"),
    ("SELECT dpd_bucket_recomputed, count(*) AS n FROM l2.card_accounts GROUP BY 1", "T3", "stored buckets"),
    ("SELECT count(*) AS n FROM l2.collections_cases WHERE current_bucket_recomputed = '31-60'", "T3", "data_quality"),
    ("SELECT model_name, avg(score) AS s FROM l2.model_scores GROUP BY 1", "T4", "dq_superseded_duplicate = false"),
]


@pytest.mark.parametrize("sql,rule,fragment", REJECTED)
def test_rejections(g, sql, rule, fragment):
    result = validate(sql, g)
    assert not result.ok and result.sql is None, sql
    assert rule in rules(result), (sql, rules(result), messages(result))
    assert fragment.lower() in messages(result).lower(), (fragment, messages(result))


def test_spec_named_attacks_are_all_rejected(g):
    for sql in ("SELECT * FROM read_csv('/etc/passwd')", "ATTACH 'x.duckdb' AS x", "SELECT * FROM curated.customers",
                "COPY l2.customers TO 'x.csv'", "PRAGMA version", "SELECT 1; SELECT 2", "SELECT * FROM l2.customers"):
        assert not validate(sql, g).ok, sql


def test_t5_count_star_over_a_join_is_a_warning_not_a_rejection(g, serving):
    result = validate("SELECT c.queue, count(*) AS n FROM l2.collections_cases c JOIN l2.promises_to_pay p ON p.case_id = c.case_id GROUP BY 1", g)
    assert result.ok
    assert [i.rule for i in result.issues] == ["T5"] and result.issues[0].severity == "warning"
    assert "count(DISTINCT" in sql_guard.warning_texts(result.issues)[0]
    assert not validate("SELECT queue, count(*) AS n FROM l2.collections_cases GROUP BY 1", g).issues
    assert not validate("SELECT c.queue, count(DISTINCT p.ptp_id) AS n FROM l2.collections_cases c JOIN l2.promises_to_pay p USING (case_id) GROUP BY 1", g).issues


def test_trap_catalogue(g):
    assert [t.id for t in sql_guard.TRAPS] == ["T1", "T2", "T3", "T4", "T5"]
    assert all(t.message and callable(t.detector) for t in sql_guard.TRAPS)
    assert [t.severity for t in sql_guard.TRAPS] == ["error", "error", "error", "error", "warning"]


def test_several_problems_are_reported_together(g):
    result = validate("SELECT first_name, count(*) FROM curated.customers c JOIN l2.contact_history h ON true WHERE h.contact_ts_local > now() GROUP BY 1", g)
    assert {"R2", "R6", "T1", "T2"} <= set(rules(result))
    text = sql_guard.error_text(result.issues)
    assert "[R2]" in text and "[T2]" in text


def test_unknown_table_in_semantic_layer_is_row_level_by_default(serving):
    ctx = sql_guard.GuardContext.from_serving(serving, {"tables": {}}, 50)
    assert "code_lookups" in ctx.row_level
    assert not validate("SELECT * FROM l2.code_lookups", ctx).ok


def test_guard_never_raises_on_garbage(g):
    for sql in (None, 42, "((((", "SELECT", "\x00", "SELECT '" + "a" * 5000, "SELECT " + "(" * 300 + "1" + ")" * 300):
        assert validate(sql, g).ok in (True, False)
