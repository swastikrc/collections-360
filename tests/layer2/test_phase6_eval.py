"""Phase 6 (offline): comparers, loaders and the harness, run end to end with the scripted fake LLM."""
import json

import duckdb
import pytest
import yaml

from c360.layer2 import config
from c360.layer2.answer import Answer, Refusal, Table
from c360.layer2.eval import __main__ as eval_cli
from c360.layer2.eval import compare as cmp
from c360.layer2.eval import harness, loaders


def ans(text="", table=None, status="answered", insight=None):
    return Answer(status=status, answer_text=text, insight=insight,
                  table=Table(columns=table[0], rows=table[1]) if table else None,
                  refusal=Refusal(rule="x", reason="y") if status == "refused" else None)


RATIO = {"kind": "scalar", "value": 0.510204, "unit": "ratio", "tolerance": 0.005}


@pytest.mark.parametrize("text,ok", [
    ("The promise-kept rate was 51.0%.", True), ("It was 51%.", True), ("The rate is 0.51.", True),
    ("About 51.02 percent of promises were kept.", True), ("51.6% were kept.", False), ("The rate was 49%.", False),
    ("25 of 49 promises were kept.", False), ("No number here.", False),
])
def test_scalar_ratio(text, ok):
    assert cmp.compare(RATIO, ans(text))[0] is ok


def test_scalar_counts_and_amounts_use_relative_tolerance():
    count = {"kind": "scalar", "value": 270, "unit": "count", "relative_tolerance": 0.005}
    assert cmp.compare(count, ans("There are 270 open cases."))[0]
    assert cmp.compare(count, ans("There are 271 open cases."))[0]
    assert not cmp.compare(count, ans("There are 280 open cases."))[0]
    amount = {"kind": "scalar", "value": 294750.18, "unit": "amount", "relative_tolerance": 0.005}
    assert cmp.compare(amount, ans("Total past due is $294,750.18."))[0]
    assert cmp.compare(amount, ans("Total past due is about $295k."))[0]
    assert cmp.compare(amount, ans("See table.", table=(["total"], [[294750.18]])))[0]
    assert not cmp.compare(amount, ans("Total past due is $312,000."))[0]


TABLE = {"kind": "table", "key": ["hardship_reason"], "unit": "ratio", "tolerance": 0.005,
         "rows": [{"hardship_reason": "job_loss", "value": 0.2857}, {"hardship_reason": "illness", "value": 0.75}]}


def test_table_matches_on_label_and_value_whatever_the_headers():
    good = ans("Illness leads.", table=(["Hardship reason", "Kept rate"], [["Illness", "75.0%"], ["Job loss", 0.2857], ["none", 0.58]]))
    assert cmp.compare(TABLE, good) == (True, "2 of 2 rows match")
    percent_cells = ans("x", table=(["reason", "n", "rate (%)"], [["job_loss", 7, 28.6], ["illness", 8, 75.0]]))
    assert cmp.compare(TABLE, percent_cells)[0]
    wrong = ans("x", table=(["reason", "rate"], [["job_loss", 0.40], ["illness", 0.75]]))
    passed, detail = cmp.compare(TABLE, wrong)
    assert not passed and "1 of 2" in detail and "job_loss = 0.2857" in detail
    missing_row = ans("x", table=(["reason", "rate"], [["illness", 0.75]]))
    assert not cmp.compare(TABLE, missing_row)[0]


def test_table_can_be_answered_in_prose():
    assert cmp.compare(TABLE, ans("Illness cases kept 75% of promises, while job-loss cases kept only 28.6%."))[0]
    assert not cmp.compare(TABLE, ans("Illness cases kept 75% of promises; job loss was lower."))[0]


def test_table_any_match_for_ties_and_single_row_rankings():
    top = {"kind": "table", "key": ["channel"], "unit": "ratio", "tolerance": 0.005, "rows": [{"channel": "call", "value": 0.31}]}
    assert cmp.compare(top, ans("Calls had the highest right-party contact rate at 31.0%."))[0]
    tie = {**top, "match": "any", "rows": [{"channel": "call", "value": 0.31}, {"channel": "sms", "value": 0.31}]}
    assert cmp.compare(tie, ans("SMS was highest at 31%."))[0] and not cmp.compare(tie, ans("Email was highest at 40%."))[0]


def test_set_contains_refusal_and_manual():
    assert cmp.compare({"kind": "contains", "value": ["hardship_specialist", "job_loss"]}, ans("It is in the Hardship Specialist queue (job loss)."))[0]
    assert not cmp.compare({"kind": "contains", "value": ["legal"]}, ans("It is in the hardship queue."))[0]
    assert cmp.compare({"kind": "set", "value": ["card", "loan"]}, ans("x", table=(["product"], [["card"], ["loan"]])))[0]
    assert cmp.compare({"kind": "refusal"}, ans("No.", status="refused")) == (True, "refused")
    assert cmp.compare({"kind": "refusal"}, ans("The rate is 51%."))[0] is False
    assert cmp.compare(RATIO, ans("I cannot answer that.", status="refused")) == (False, "refused a question that should be answered")
    assert cmp.compare({"kind": "manual"}, ans("anything"))[0] is None


# ---- loaders -------------------------------------------------------------------------------------
def test_question_sets_meet_the_spec_sizes(world):
    synthetic = loaders.load_set("synthetic", synthetic_path=world["questions"])
    heldout = loaders.load_set("heldout", synthetic_path=world["questions"])
    paraphrase = loaders.load_set("paraphrase", synthetic_path=world["questions"])
    refusal = loaders.load_set("refusal")
    assert len(synthetic) >= 30 and len(heldout) >= 15 and len(refusal) >= 12
    groups = {}
    for q in paraphrase:
        groups.setdefault(q["group"], []).append(q)
    assert len(groups) == 10 and all(2 <= len(v) <= 3 for v in groups.values())
    assert all(q["expected"] == {"kind": "refusal"} for q in refusal)
    ids = [q["id"] for s in (synthetic, heldout, paraphrase, refusal) for q in s]
    assert len(ids) == len(set(ids))
    asked = {q["question"].lower() for q in synthetic}
    assert not [q["id"] for q in heldout + paraphrase if q["question"].lower() in asked], "held-out and paraphrases must be new wordings"


def test_heldout_expected_answers_come_from_truth(world, truth):
    heldout = {q["id"]: q for q in loaders.load_set("heldout", synthetic_path=world["questions"])}
    assert not [q["id"] for q in heldout.values() if q.get("skip")]
    h01 = heldout["H01"]["expected"]
    assert h01["kind"] == "scalar" and h01["unit"] == "ratio" and h01["tolerance"] == 0.005
    assert h01["value"] == pytest.approx(truth.promise_kept_rate("2026-01-01", "2026-06-30", where={"product": "card"}))
    h03 = heldout["H03"]["expected"]
    assert h03["kind"] == "table" and h03["key"] == ["queue"] and len(h03["rows"]) >= 3
    case = truth.sample_case("open_hardship")
    h19 = heldout["H19"]
    assert case["case_id"] in h19["question"] and h19["expected"] == {"kind": "contains", "value": [case["primary_product"], case["queue"]]}
    assert h19["fake_plan"][0]["args"]["key"] == case["case_id"]


def test_paraphrases_inherit_the_expected_answer(world):
    synthetic = {q["id"]: q for q in loaders.load_set("synthetic", synthetic_path=world["questions"])}
    for q in loaders.load_set("paraphrase", synthetic_path=world["questions"]):
        assert q["expected"] == synthetic[q["of"]]["expected"] and q["fake_plan"] == synthetic[q["of"]]["fake_plan"]
        assert q["question"] != synthetic[q["of"]]["question"]


def test_truth_backed_sets_are_skipped_outside_the_synthetic_world(world, monkeypatch, tmp_path):
    monkeypatch.setenv("C360_WAREHOUSE", str(tmp_path))          # a warehouse with no SYNTHETIC marker
    heldout = loaders.load_set("heldout", synthetic_path=world["questions"])
    assert [q["id"] for q in heldout if q.get("skip")] == [q["id"] for q in heldout if q["id"] not in ("H20",)]
    # DB machine: heldout_real.yaml now exists; its answers are literal (hand-checked SQL), so nothing is skipped
    real = loaders.load_set("heldout_real")
    assert len(real) >= 15 and not [q for q in real if q.get("skip")]
    assert all("value" in q["expected"] or "rows" in q["expected"] or q["expected"]["kind"] == "refusal" for q in real)


@pytest.mark.parametrize("text,expected", [
    ("270", {"kind": "scalar", "value": 270.0, "unit": "count"}),
    ("1,234", {"kind": "scalar", "value": 1234.0, "unit": "count"}),
    ("$294,750.18", {"kind": "scalar", "value": 294750.18, "unit": "count"}),
    ("51.0%", {"kind": "scalar", "value": 0.51, "unit": "ratio", "tolerance": 0.005}),
    ("0.51", {"kind": "scalar", "value": 0.51, "unit": "ratio"}),
    ("card, loan, overdraft", {"kind": "set", "value": ["card", "loan", "overdraft"]}),
    ('["card", "loan"]', {"kind": "set", "value": ["card", "loan"]}),
    ("refuse", {"kind": "refusal"}), ("Must refuse.", {"kind": "refusal"}),
    (None, {"kind": "manual"}), ("", {"kind": "manual"}),
])
def test_parse_expected_answer(text, expected):
    assert loaders.parse_expected_answer(text) == expected


def test_parse_expected_answer_tables_and_free_text():
    table = loaders.parse_expected_answer('[{"queue": "legal", "cases": 4}, {"queue": "early_stage", "cases": 120}]')
    assert table == {"kind": "table", "key": ["queue"], "unit": "count",
                     "rows": [{"queue": "legal", "value": 4.0}, {"queue": "early_stage", "value": 120.0}]}
    free = loaders.parse_expected_answer("Customers mostly mention job loss. Some mention illness, and a few mention reduced hours.")
    assert free["kind"] == "manual" and free["note"].startswith("Customers mostly")


def test_benchmark_loader_on_the_synthetic_table(world, truth):
    bench = {q["id"]: q for q in loaders.load_benchmark()}
    assert list(bench) == ["BQ01", "BQ02", "BQ03", "BQ04", "BQ05", "BQ06"]
    assert bench["BQ01"]["expected"] == {"kind": "scalar", "value": float(truth.open_case_count()), "unit": "count"}
    assert bench["BQ02"]["expected"]["unit"] == "ratio"
    assert bench["BQ02"]["expected"]["value"] == pytest.approx(truth.promise_kept_rate("2026-08-01", "2026-08-31"))
    by_queue = truth.open_case_count(by="queue")
    assert bench["BQ03"]["expected"]["kind"] == "table" and bench["BQ03"]["expected"]["key"] == ["queue"]
    assert {r["queue"]: r["value"] for r in bench["BQ03"]["expected"]["rows"]} == {k: float(v) for k, v in by_queue.items()}
    assert bench["BQ04"]["expected"]["value"] == float(truth.loans_with_co_borrower())
    assert bench["BQ05"]["expected"] == {"kind": "refusal"} and bench["BQ06"]["expected"]["kind"] == "manual"
    assert all("gold_sql" not in q and "fake_plan" not in q for q in bench.values()), "gold SQL never travels with the question"
    assert bench["BQ01"]["tags"] == ["benchmark", "metric"]


def test_benchmark_loader_detects_other_column_names_and_survives_bad_gold_sql():
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA curated")
    con.execute("""CREATE TABLE curated.benchmark_questions AS SELECT * FROM (VALUES
        (1, 'How many?', '42', 'SELECT 42 AS n', 'metric'),
        (2, 'Broken gold', '7', 'SELECT nope FROM missing_table', 'metric'),
        (3, 'By gender?', NULL, NULL, 'Refusal'),
        (4, 'Themes?', NULL, NULL, NULL)) t(id, prompt, gold_answer, expected_sql, type)""")
    bench = loaders.load_benchmark(con)
    assert [q["id"] for q in bench] == ["1", "2", "3", "4"] and bench[0]["question"] == "How many?"
    assert bench[0]["expected"] == {"kind": "scalar", "value": 42.0, "unit": "count"}
    # DB machine: a stated answer wins over gold SQL (gold SQL is only run when there is no stated answer)
    assert bench[1]["expected"]["value"] == 7.0 and "note" not in bench[1]["expected"]
    assert bench[2]["expected"] == {"kind": "refusal"} and bench[3]["expected"] == {"kind": "manual"}
    con.execute("CREATE OR REPLACE TABLE curated.benchmark_questions AS SELECT 1 AS a, 'x' AS b")
    with pytest.raises(ValueError, match="cannot find the question column"):
        loaders.load_benchmark(con)


# ---- harness, end to end with the fake LLM -----------------------------------------------------
@pytest.fixture(scope="module")
def fake_runs(world, serving, mock_db, tmp_root):
    from c360.layer2.tools.registry import ToolContext
    ctx = ToolContext.build(serving=serving, mongo=mock_db)
    return {name: harness.run_set(name, fake=True, synthetic_path=world["questions"], out_dir=tmp_root / "eval", ctx=ctx, quiet=True)
            for name in ("synthetic", "heldout", "paraphrase", "refusal", "benchmark")}


@pytest.mark.parametrize("name", ["synthetic", "heldout", "paraphrase", "refusal"])
def test_fake_run_proves_the_pipeline_for_every_set(fake_runs, name):
    summary, results = fake_runs[name]["summary"], fake_runs[name]["results"]
    failed = [(r["id"], r["detail"], r.get("got")) for r in results if r["passed"] is False]
    assert failed == [] and summary["accuracy"] == 1.0 and summary["skipped"] == 0
    assert summary["answers_with_personal_data"] == 0 and summary["avg_tool_calls"] <= 2.5


def test_fake_run_summaries(fake_runs):
    s = fake_runs["synthetic"]["summary"]
    assert s["questions"] == 38 and s["scored"] == 37 and s["manual"] == 1
    assert s["by_tag"]["governed"]["accuracy"] == 1.0 and s["by_tag"]["custom_sql"]["scored"] == 8
    assert s["governed_answers"] == 26 and s["custom_sql_answers"] == 8
    p = fake_runs["paraphrase"]["summary"]
    assert p["paraphrase_groups"] == 10 and p["paraphrase_groups_consistent"] == 1.0
    assert fake_runs["refusal"]["summary"]["refusal_accuracy"] == 1.0
    b = fake_runs["benchmark"]["summary"]
    assert b["questions"] == 6 and b["skipped"] == 5 and b["refusal_accuracy"] == 1.0


def test_reports_are_written(fake_runs):
    md, js = fake_runs["synthetic"]["report_md"], fake_runs["synthetic"]["report_json"]
    text = md.read_text()
    assert md.name.startswith("eval_synthetic_") and "**Fake LLM run.**" in text and "## Summary" in text and "### By tag" in text
    assert "### S01 — PASS" in text and "- SQL: `SELECT" in text and "governed" in text
    data = json.loads(js.read_text())
    assert data["set"] == "synthetic" and data["fake"] is True and len(data["results"]) == 38
    first = data["results"][0]
    assert {"id", "question", "passed", "detail", "status", "got", "tools", "sql", "warnings", "tool_calls", "latency_s"} <= set(first)


def test_harness_flags_a_wrong_answer_and_personal_data(world, serving, mock_db, tmp_root):
    from c360.layer2.llm.fake import ScriptedLLM, call, final_turn, tool_turn
    from c360.layer2.tools.registry import ToolContext
    ctx = ToolContext.build(serving=serving, mongo=mock_db)
    q = yaml.safe_load(world["questions"].read_text())[0]
    wrong = harness.run_question(q, ctx, ScriptedLLM([tool_turn(call("compute_metric", metric="promise_kept_rate", period="July 2026")),
                                                      final_turn("The promise-kept rate was 12.3%.")]))
    assert wrong["passed"] is False and wrong["tool_calls"] == 1 and wrong["governed"] == [True]
    trapped = harness.run_question(q, ctx, ScriptedLLM([
        tool_turn(call("run_sql", query="SELECT count(*) AS n FROM l2.contact_history", purpose="analysis")), final_turn("Unknown.")]))
    assert trapped["trap_hits"] == ["T1"]
    summary = harness.summarise([wrong, trapped])
    assert summary["accuracy"] == 0.0 and summary["trap_rule_hits"] == 1


def test_eval_cli(world, serving_path, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "QUESTIONS_DIR", config.QUESTIONS_DIR)
    monkeypatch.setenv("C360_L2_OUT", str(tmp_path))
    assert eval_cli.main(["--set", "refusal", "--fake", "--limit", "3"]) == 0
    out = capsys.readouterr().out
    assert "[eval] refusal: 100.0% (3/3)" in out and len(list(tmp_path.glob("eval_refusal_*.md"))) == 1
    assert eval_cli.main(["--set", "no_such_set", "--fake"]) == 0 and "skipped" in capsys.readouterr().out
    monkeypatch.delenv("C360_L2_MODEL", raising=False)
    assert eval_cli.main(["--set", "refusal"]) == 2
    assert "C360_L2_MODEL" in capsys.readouterr().err


# ---- DB machine: real benchmark answer formats (structure only; every value here is invented) -------------
def test_parse_real_answer_formats():
    from c360.layer2.eval.loaders import parse_expected_answer, parse_tolerance
    t = parse_expected_answer("team,rate_pct,n\nalpha,12.5,100\nbeta,7.0,40", parse_tolerance("±0.5 pts"))
    assert t["kind"] == "table" and t["key"] == ["team"] and t["unit"] == "ratio" and t["tolerance"] == pytest.approx(0.005)
    assert t["rows"] == [{"team": "alpha", "value": pytest.approx(0.125)}, {"team": "beta", "value": pytest.approx(0.07)}]
    d = parse_expected_answer("group,avg_days,n\nx,3.2,10", parse_tolerance("±0.1 days"))
    assert d["kind"] == "table" and "unit" not in d and d["tolerance"] == pytest.approx(0.1) and d["rows"][0]["value"] == 3.2
    c = parse_expected_answer("bucket,cases\nlow,10\nhigh,3", parse_tolerance("exact"))
    assert c["kind"] == "table" and c["tolerance"] <= 1e-9
    s = parse_expected_answer("some_rate_pct\n42.0", parse_tolerance("±0.1 pts"))
    assert s == {"kind": "scalar", "value": pytest.approx(0.42), "unit": "ratio", "tolerance": pytest.approx(0.001)}
    assert parse_expected_answer("Refuse: outside scope")["kind"] == "refusal"
    assert parse_expected_answer("Refuse: protected ground")["kind"] == "refusal"
    assert parse_expected_answer("Do the first thing, record it, and then offer an option before asking.")["kind"] == "manual"
    assert parse_expected_answer("red, green, blue")["kind"] == "set"
    assert parse_tolerance("direction of effect") is None and parse_tolerance("±1 CAD") == ("abs", 1.0)


def test_benchmark_answers_come_from_a_labels_file_never_the_prompt(world, tmp_path, monkeypatch):
    """Answers kept outside the database are merged by id inside the harness only."""
    import duckdb
    from c360.layer2.eval.loaders import load_benchmark
    labels = tmp_path / "answers.csv"
    labels.write_text("question_id,gold_answer,gold_sql_reference,gold_sources\nBQ02,\"rate_pct\n50.0\",,\n")
    con = duckdb.connect(str(world.db_path) if hasattr(world, "db_path") else str(config.settings().source_db), read_only=True)
    try:
        qs = {q["id"]: q for q in load_benchmark(con, answers_path=labels)}
    finally:
        con.close()
    assert "BQ02" in qs and qs["BQ02"]["expected"]["kind"] in ("scalar", "manual", "table")
