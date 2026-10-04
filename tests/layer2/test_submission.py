"""Benchmark submission CSV, extra-questions file, resume and --only. Offline (fake LLM)."""
import csv

from c360.layer2.eval import harness, loaders, submission


def _res(qid, status="answered", text="It is 3.", table=None, sources=None, refusal=None, warnings=None):
    return {"id": qid, "status": status, "warnings": warnings or [],
            "answer": {"status": status, "answer_text": text, "table": table, "refusal": refusal,
                       "sources": sources or [], "warnings": warnings or []}}


def read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_cells_follow_the_template_columns(tmp_path):
    results = {
        "BQ-002": _res("BQ-002", text="At most 3 call attempts in 7 days.",
                       sources=[{"kind": "policy", "detail": "x", "ids": ["POL-COLL-001 §5.2"], "sql": None}]),
        "BQ-004": _res("BQ-004", text="By queue:", table={"columns": ["queue", "avg_dpd"], "rows": [["early", 14.5], ["mid", None]]},
                       sources=[{"kind": "governed_metric", "detail": "m", "ids": [], "sql": "SELECT 1\n  FROM l2.x"}]),
        "BQ-018": _res("BQ-018", status="refused", text="", refusal={"rule": "out_of_scope_data", "reason": "Weather is out of scope."}),
    }
    tpl = tmp_path / "template.csv"
    tpl.write_text("question_id,answer,sql_or_sources,refused\nBQ-018,,,\nBQ-002,,,\nBQ-004,,,\nBQ-099,,,\n")
    info = submission.write_csv(results, tmp_path / "out.csv", tpl)
    rows = read(info["path"])
    assert [r["question_id"] for r in rows] == ["BQ-018", "BQ-002", "BQ-004", "BQ-099"]     # template order kept
    assert list(rows[0]) == submission.COLUMNS and info["missing"] == ["BQ-099"]
    assert rows[0]["refused"] == "true" and rows[0]["answer"] == "Refused: Weather is out of scope."
    assert rows[1]["sql_or_sources"] == "POL-COLL-001 §5.2" and rows[1]["refused"] == "false"
    assert rows[2]["answer"] == "By queue:\nqueue,avg_dpd\nearly,14.5\nmid," and rows[2]["sql_or_sources"] == "SELECT 1 FROM l2.x"
    assert rows[3]["answer"] == "" and rows[3]["refused"] == "false"


def test_later_runs_win_and_extras_are_appended(tmp_path):
    merged = submission.merge_results([[_res("BQ-001", text="old"), _res("BQ-010")],
                                       [_res("BQ-001", text="new"), _res("BQ-036"), {"id": "BQ-037", "answer": None}]])
    assert merged["BQ-001"]["answer"]["answer_text"] == "new" and "BQ-037" not in merged
    tpl = tmp_path / "t.csv"
    tpl.write_text("question_id,answer,sql_or_sources,refused\nBQ-001,,,\nBQ-010,,,\n")
    rows = read(submission.write_csv(merged, tmp_path / "o.csv", tpl)["path"])
    assert [r["question_id"] for r in rows] == ["BQ-001", "BQ-010", "BQ-036"]


def test_questions_file_is_loaded_like_the_benchmark(tmp_path):
    f = tmp_path / "extra.csv"
    f.write_text("question_id,question_text,tolerance,split\nBQ-036,How many open cases are there?,exact,extra\n")
    ans = tmp_path / "answers.csv"
    ans.write_text("question_id,gold_answer\nBQ-036,\"open_cases\n12\"\n")
    qs = loaders.load_questions_csv(f, answers_path=ans)
    assert qs[0]["id"] == "BQ-036" and qs[0]["question"] == "How many open cases are there?"
    assert qs[0]["expected"]["kind"] == "scalar" and qs[0]["expected"]["value"] == 12.0
    assert "split:extra" in qs[0]["tags"]


def test_resume_reuses_answers_and_reasks_model_errors(ctx, tmp_path):
    first = harness.run_set("refusal", fake=True, ctx=ctx, out_dir=tmp_path, quiet=True, limit=3)
    ids = [r["id"] for r in first["results"]]
    reuse = {r["id"]: r for r in first["results"]}
    broken = dict(reuse[ids[1]], warnings=["model error: ClientError (HTTP 429)"])
    reuse[ids[1]] = broken
    second = harness.run_set("refusal", fake=True, ctx=ctx, out_dir=tmp_path, quiet=True, limit=3, reuse=reuse)
    details = {r["id"]: r["detail"] for r in second["results"]}
    assert details[ids[0]].endswith("(reused)") and details[ids[2]].endswith("(reused)")
    assert not details[ids[1]].endswith("(reused)")                     # the model-error one was asked again
    only = harness.run_set("refusal", fake=True, ctx=ctx, out_dir=tmp_path, quiet=True, only=[ids[2]])
    assert [r["id"] for r in only["results"]] == [ids[2]]
    assert all("answer" in r for r in first["results"])
