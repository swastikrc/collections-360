"""Phase 4: the five tools, the registry and the sanity checks."""
import json

import pytest

from c360.layer2 import config, sanity
from c360.layer2.tools import ToolResult, compute_metric as cm, get_customer as gc, search_text as st
from c360.layer2.tools.registry import TOOL_NAMES, ToolContext, dispatch, specs

Q3 = ("2026-07-01", "2026-09-28")
APPROX = dict(rel=1e-9, abs=1e-9)


def metric(ctx, name, period, group_by=None, filters=None):
    r = dispatch("compute_metric", {"metric": name, "period": period, "group_by": group_by or [], "filters": filters or []}, ctx)
    assert r.ok, r.error
    return r


def scalar(r):
    assert r.row_count == 1
    return r.data[0]["value"]


def grouped(r, key):
    return {row[key]: row["value"] for row in r.data if row[key] is not None}


# ---- compute_metric: every metric equals the independent pandas truth ---------------------------
METRIC_CASES = [
    # metric, period phrase, truth start/end, group_by dimension
    ("promise_kept_rate", "this quarter", Q3, "hardship_reason"),
    ("broken_promise_rate", "this quarter", Q3, "queue"),
    ("promise_count", "last quarter", ("2026-04-01", "2026-06-30"), "channel"),
    ("cure_rate", "August 2026", ("2026-08-01", "2026-08-31"), "product"),
    ("roll_rate", "July 2026", ("2026-07-01", "2026-07-31"), "from_bucket"),
    ("rpc_rate", "August 2026", ("2026-08-01", "2026-08-31"), "queue"),
    ("ptp_rate", "August 2026", ("2026-08-01", "2026-08-31"), "channel"),
    ("cost_per_contact", "August 2026", ("2026-08-01", "2026-08-31"), "channel"),
    ("cost_per_rpc", "this quarter", Q3, "channel"),
    ("collected_amount", "this quarter", Q3, "product"),
    ("hardship_rate", "year to date", ("2026-01-01", "2026-09-28"), "product"),
    ("delinquency_rate", "all time", ("1900-01-01", "2999-12-31"), "product_family"),
    ("write_off_rate", "this quarter", Q3, "product_family"),
    ("write_off_count", "this quarter", Q3, "product"),
    ("write_off_amount", "this quarter", Q3, "product_family"),
    ("contact_attempts", "last month", ("2026-08-01", "2026-08-31"), "queue"),
    ("contact_attempts_per_case", "last month", ("2026-08-01", "2026-08-31"), "bucket"),
    ("payroll_delay_share", "all time", ("1900-01-01", "2999-12-31"), "with_open_case"),
    ("payment_on_contact_amount", "this quarter", Q3, "channel"),
    ("hardship_offer_acceptance_rate", "year to date", ("2026-01-01", "2026-09-28"), "program"),
    ("open_case_count", "all time", ("1900-01-01", "2999-12-31"), "queue"),
    ("past_due_amount", "all time", ("1900-01-01", "2999-12-31"), "product"),
    ("average_dpd", "all time", ("1900-01-01", "2999-12-31"), "queue"),
]

PLACEHOLDERS = {"promise_count", "payment_on_contact_amount", "hardship_offer_acceptance_rate", "open_case_count", "past_due_amount"}


def test_every_metric_in_the_catalogue_is_covered(ctx):
    assert sorted(m for m, *_ in METRIC_CASES) == sorted(ctx.metrics)
    # official definitions (DB machine, curated.metric_definitions) are verified; the rest stay labelled placeholders
    for name, spec in ctx.metrics.items():
        assert spec["status"] == ("placeholder" if name in PLACEHOLDERS else "verified"), name
        assert name in PLACEHOLDERS or spec.get("defined_in", "").startswith("metric_definitions:"), name


@pytest.mark.parametrize("name,period,dates,by", METRIC_CASES)
def test_metric_scalar_matches_truth(ctx, truth, name, period, dates, by):
    expected = truth.resolve(name, {"start": dates[0], "end": dates[1]})
    assert expected is not None
    assert scalar(metric(ctx, name, period)) == pytest.approx(expected, rel=1e-6, abs=1e-6)


@pytest.mark.parametrize("name,period,dates,by", METRIC_CASES)
def test_metric_grouping_matches_truth(ctx, truth, name, period, dates, by):
    expected = {k: v for k, v in truth.resolve(name, {"start": dates[0], "end": dates[1], "by": by}).items() if v is not None}
    got = grouped(metric(ctx, name, period, [by]), by)
    assert len(expected) >= 1
    assert set(got) >= set(expected)
    for label, value in expected.items():
        assert got[label] == pytest.approx(value, rel=1e-6, abs=1e-6), (name, label)


def test_metric_filter_matches_truth(ctx, truth):
    r = metric(ctx, "roll_rate", "June 2026", filters=[{"dimension": "from_bucket", "op": "=", "value": "1-30"}])
    assert scalar(r) == pytest.approx(truth.roll_rate("2026-06-01", "2026-06-30", where={"from_bucket": "1-30"}), rel=1e-6, abs=1e-6)
    r = metric(ctx, "promise_kept_rate", "last 12 months", filters=[{"dimension": "product", "op": "=", "value": "card"}])
    assert scalar(r) == pytest.approx(truth.promise_kept_rate("2025-09-29", "2026-09-28", where={"product": "card"}), rel=1e-6, abs=1e-6)
    both = metric(ctx, "open_case_count", "all time", filters=[{"dimension": "queue", "op": "in", "value": "early_stage, mid_stage"}])
    by_queue = truth.open_case_count(by="queue")
    assert scalar(both) == by_queue["early_stage"] + by_queue["mid_stage"]
    not_early = metric(ctx, "open_case_count", "all time", filters=[{"dimension": "queue", "op": "!=", "value": "early_stage"}])
    assert scalar(not_early) == truth.open_case_count() - by_queue["early_stage"]
    late = metric(ctx, "contact_attempts", "August 2026", filters=[{"dimension": "hour", "op": ">=", "value": "17"}])
    assert 0 < scalar(late) < truth.contact_attempts("2026-08-01", "2026-08-31")


def test_metric_result_shape_and_labels(ctx):
    r = metric(ctx, "promise_kept_rate", "last 90 days", ["hardship_reason"])
    assert r.governed is True and r.tool == "compute_metric"
    assert r.detail == "Governed metric: Promise-kept rate"            # verified on the DB machine
    assert metric(ctx, "promise_count", "last 90 days").detail == "Governed metric: Promises made (placeholder definition)"
    assert r.columns == ["hardship_reason", "numerator", "denominator", "value"]
    assert r.meta["period"] == "1 Jul–28 Sep 2026" and r.meta["unit"] == "ratio"
    assert "DATE '2026-07-01'" in r.sql and "DATE '2026-09-28'" in r.sql and "$" not in r.sql
    assert "l2.promises_to_pay" in r.sql and r.sql.rstrip().endswith("LIMIT 51")
    assert ctx.serving.execute(r.sql)[1]        # the SQL shown to the user runs as-is
    assert all(0 <= row["value"] <= 1 for row in r.data) and not r.warnings


def test_verified_metric_drops_the_placeholder_label(ctx):
    metrics = json.loads(json.dumps(ctx.metrics))
    metrics["cure_rate"].update(status="verified", aliases=["Cure Rate (official)", "cured_share"])
    ctx2 = ToolContext.build(serving=ctx.serving, mongo=ctx.mongo, metrics=metrics)
    r = dispatch("compute_metric", {"metric": "cured_share", "period": "year to date"}, ctx2)
    assert r.ok and r.detail == "Governed metric: Cure rate" and r.meta["metric"] == "cure_rate"
    assert dispatch("compute_metric", {"metric": "Cure rate", "period": "year to date"}, ctx2).ok


def test_default_period_and_month_trend(ctx):
    assert scalar(dispatch("compute_metric", {"metric": "open_case_count"}, ctx)) > 0
    r = metric(ctx, "promise_count", "year to date", ["month"])
    months = [row["month"] for row in r.data]
    assert months == sorted(months) and months[0] == "2026-01" and months[-1] == "2026-09"


def test_metric_values_are_bound_not_interpolated(ctx):
    evil = "x' OR '1'='1"
    r = metric(ctx, "open_case_count", "all time", filters=[{"dimension": "queue", "op": "=", "value": evil}])
    assert scalar(r) == 0 and "'x'' OR ''1''=''1'" in r.sql
    assert any("No rows" in w or "zero" in w for w in r.warnings) or scalar(r) == 0


@pytest.mark.parametrize("args,fragment", [
    ({"metric": "profit", "period": "last month"}, "Valid metrics: average_dpd, broken_promise_rate"),
    ({"metric": "cure_rate", "period": "last month", "group_by": ["postal_code"]}, "Valid dimensions: product, queue"),
    ({"metric": "cure_rate", "period": "last month", "group_by": ["product", "queue", "month"]}, "at most 2"),
    ({"metric": "cure_rate", "period": "whenever"}, "Accepted forms"),
    ({"metric": "cure_rate"}, "needs a period"),
    ({"metric": "cure_rate", "period": "last month", "filters": [{"dimension": "gender", "op": "=", "value": "F"}]}, "not a dimension"),
    ({"metric": "cure_rate", "period": "last month", "filters": [{"dimension": "queue", "op": "like", "value": "x"}]}, "not supported"),
    ({"metric": "rpc_rate", "period": "last month", "filters": [{"dimension": "hour", "op": ">=", "value": "noon"}]}, "not valid"),
    ({"metric": "cure_rate", "period": "last month", "filters": ["queue=legal"]}, "must be an object"),
])
def test_metric_errors_tell_the_model_how_to_fix_the_call(ctx, args, fragment):
    r = dispatch("compute_metric", args, ctx)
    assert not r.ok and fragment in r.error, r.error


# ---- run_sql -------------------------------------------------------------------------------------
def test_run_sql_runs_validated_queries(ctx, truth):
    r = dispatch("run_sql", {"query": "SELECT count(*) AS customers FROM customer_summary WHERE financial_hardship_flag", "purpose": "analysis"}, ctx)
    assert r.ok and r.governed is False and r.detail == "Custom query — not a governed metric"
    assert r.data == [{"customers": truth.customers_with_hardship_flag()}]
    assert "l2.customer_summary" in r.sql and r.sql.endswith("LIMIT 51")


def test_run_sql_feeds_validator_rejections_back(ctx):
    r = dispatch("run_sql", {"query": "SELECT count(*) AS n FROM l2.contact_history", "purpose": "analysis"}, ctx)
    assert not r.ok and "[T1]" in r.error and "is_duplicate = false" in r.error and r.meta["rules"] == ["T1"]
    r = dispatch("run_sql", {"query": "SELECT * FROM curated.customers"}, ctx)
    assert not r.ok and "[R2]" in r.error
    r = dispatch("run_sql", {"query": "SELECT * FROM read_csv('/etc/passwd')"}, ctx)
    assert not r.ok and "[R3]" in r.error


def test_run_sql_database_errors_come_back_as_tool_errors(ctx):
    r = dispatch("run_sql", {"query": "SELECT no_such_column FROM l2.offers"}, ctx)
    assert not r.ok and "no_such_column" in r.error and r.sql


def test_run_sql_truncates_and_warns(ctx):
    r = dispatch("run_sql", {"query": "SELECT case_id, queue FROM l2.collections_cases"}, ctx)
    assert r.ok and r.row_count == 50 and r.truncated
    assert any("first 50 rows" in w for w in r.warnings)
    assert len(r.source_ids) == 50 and r.source_ids[0].startswith("CS-")


def test_run_sql_purpose_and_t5_warning(ctx, truth):
    q = "SELECT count(*) AS mismatches FROM l2.card_accounts WHERE dpd_bucket <> dpd_bucket_recomputed"
    assert "[T3]" in dispatch("run_sql", {"query": q, "purpose": "analysis"}, ctx).error
    assert dispatch("run_sql", {"query": q, "purpose": "data_quality"}, ctx).ok
    r = dispatch("run_sql", {"query": "SELECT c.queue, count(*) AS n FROM l2.collections_cases c JOIN l2.offers o USING (case_id) GROUP BY 1"}, ctx)
    assert r.ok and any(w.startswith("[T5]") for w in r.warnings)


def test_serving_cannot_read_files_even_when_the_guard_is_bypassed(ctx, tmp_path):
    victim = tmp_path / "x.csv"
    victim.write_text("a\n1\n")
    with pytest.raises(Exception, match="(?i)disabled|permission"):
        ctx.serving.execute(f"SELECT * FROM read_csv('{victim}')")


# ---- get_customer --------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def sample(truth):
    return truth.sample_case("open_hardship")


def test_get_customer_returns_a_trimmed_document(ctx, sample, mock_db):
    r = dispatch("get_customer", {"key": sample["case_id"]}, ctx)
    assert r.ok and r.detail == f"Customer record {sample['c360_id']}"
    doc = r.data
    raw = mock_db.customers.find_one({"_id": sample["c360_id"]})
    for section, field in [("contact", "primary_phone"), ("contact", "email"), ("contact", "address_line1"),
                           ("contact", "postal_code"), ("profile", "date_of_birth"), ("profile", "age"), ("profile", "age_band")]:
        assert raw[section][field] is not None, f"the raw document should hold {section}.{field}"
        assert field not in doc[section]
    assert all("evidence" not in k for k in doc["identity"]["source_keys"])
    assert raw["identity"]["source_keys"][0]["evidence"]
    assert all("file_path" not in t for t in doc["collections"]["call_transcripts"])
    assert len(doc["collections"]["cases"]) <= 5 and len(doc["collections"]["recent_notes"]) <= 5 and len(doc["model_scores"]) <= 5
    assert all(len(n["note_text"]) <= 301 for n in doc["collections"]["recent_notes"])
    assert r.source_ids[0] == sample["c360_id"] and sample["case_id"] in r.source_ids
    payload = r.to_llm()
    assert raw["contact"]["primary_phone"] not in payload and raw["contact"]["email"] not in payload
    assert raw["contact"]["address_line1"] not in payload and raw["contact"]["postal_code"] not in payload
    assert sample["queue"] in payload
    assert "primary_phone" in raw["contact"], "trimming must not modify the stored document"


def test_get_customer_resolves_other_keys(ctx, sample, mock_db):
    raw = mock_db.customers.find_one({"_id": sample["c360_id"]})
    for key in (sample["c360_id"], raw["identity"]["crm_ids"][0], sample["primary_account_id"]):
        r = dispatch("get_customer", {"key": key}, ctx)
        assert r.ok and r.data["_id"] == sample["c360_id"], key


def test_get_customer_trim_limits_and_truncation():
    doc = {"_id": "C360-X", "profile": {"age": 40, "segment": "mass"}, "contact": {"email": "a@b.ca", "province_code": "ON"},
           "identity": {"source_keys": [{"src_ref": "1", "evidence": "sur:3"}]},
           "collections": {"cases": [{"case_id": f"CS-{i}", "case_open_date": f"2026-0{i}-01"} for i in range(1, 9)],
                           "recent_notes": [{"note_ts": "2026-01-01", "note_text": "x" * 1000}],
                           "call_transcripts": [{"transcript_id": "1", "file_path": "transcripts/1.json"}]},
           "model_scores": [{"model_name": f"m{i}", "score_date": "2026-09-28"} for i in range(9)],
           "interactions": [{"interaction_id": "I1", "payload": {"text": "secret"}, "occurred_ts": "2026-01-01"}]}
    out = gc.trim(doc)
    assert [c["case_id"] for c in out["collections"]["cases"]] == ["CS-8", "CS-7", "CS-6", "CS-5", "CS-4"]
    assert len(out["collections"]["recent_notes"][0]["note_text"]) == 301
    assert len(out["model_scores"]) == 5 and "payload" not in out["interactions"][0]
    assert out["profile"] == {"segment": "mass"} and out["contact"] == {"province_code": "ON"}
    assert len(doc["collections"]["cases"]) == 8 and doc["profile"]["age"] == 40


@pytest.mark.parametrize("key", ["", "all customers", "queue=legal", "everyone in Ontario", "SELECT 1"])
def test_get_customer_rejects_non_ids_helpfully(ctx, key):
    r = dispatch("get_customer", {"key": key}, ctx)
    assert not r.ok and "not a customer/case/account ID" in r.error and "use compute_metric or run_sql" in r.error


def test_get_customer_unknown_id(ctx):
    r = dispatch("get_customer", {"key": "CS-1999-000001"}, ctx)
    assert not r.ok and "No customer found" in r.error


# ---- search_text ---------------------------------------------------------------------------------
def test_search_text_finds_hardship_notes(ctx):
    r = dispatch("search_text", {"query": "lost my job", "source": "notes"}, ctx)
    assert r.ok and r.row_count == 20 and r.meta["search_mode"] == "fts"
    assert all(h["source"] == "note" and "job" in h["text"].lower() for h in r.data)
    assert all(h["id"].startswith("N-") and h["case_id"].startswith("CS-") and h["date"] for h in r.data)
    assert all(len(h["text"]) <= st.SNIPPET_CHARS for h in r.data)
    assert r.data[0]["id"] in r.source_ids and r.data[0]["case_id"] in r.source_ids
    assert "fts_l2_agent_notes.match_bm25" in r.sql


def test_search_text_covers_transcripts_and_both(ctx):
    t = dispatch("search_text", {"query": "employment insurance", "source": "transcripts"}, ctx)
    assert t.ok and t.row_count > 0 and all(h["source"] == "transcript" for h in t.data)
    assert "customer:" in t.data[0]["text"]
    both = dispatch("search_text", {"query": "reduced hours money tight", "source": "both"}, ctx)
    assert {h["source"] for h in both.data} == {"note", "transcript"}


def test_search_text_respects_limits(ctx):
    assert dispatch("search_text", {"query": "payment", "limit": 3}, ctx).row_count == 3
    assert dispatch("search_text", {"query": "payment", "limit": 500}, ctx).row_count == st.MAX_LIMIT
    assert dispatch("search_text", {"query": "payment"}, ctx).row_count == st.DEFAULT_LIMIT


def test_search_text_respects_filters(ctx, sample):
    r = dispatch("search_text", {"query": "hardship job sick hours customer", "case_id": sample["case_id"]}, ctx)
    assert r.ok and r.row_count > 0 and {h["case_id"] for h in r.data} == {sample["case_id"]}
    r = dispatch("search_text", {"query": "customer notes", "c360_id": sample["c360_id"], "source": "notes"}, ctx)
    cases = {row[0] for row in ctx.serving.execute("SELECT case_id FROM l2.collections_cases WHERE c360_id = ?", [sample["c360_id"]])[1]}
    assert r.row_count > 0 and {h["case_id"] for h in r.data} <= cases and r.meta["search_mode"] == "recent"
    dates = [h["date"] for h in r.data]
    assert dates == sorted(dates, reverse=True), "no keywords + a customer id lists their most recent notes first"
    r = dispatch("search_text", {"query": "dispute fee", "period": "August 2026", "source": "notes"}, ctx)
    assert r.row_count > 0 and all("2026-08-01" <= h["date"] <= "2026-08-31" for h in r.data) and r.meta["period"] == "1–31 Aug 2026"
    r = dispatch("search_text", {"query": "job", "queue": "hardship_specialist", "product": "card", "source": "notes", "limit": 30}, ctx)
    ids = [h["case_id"] for h in r.data]
    rows = ctx.serving.execute("SELECT DISTINCT queue, primary_product FROM l2.collections_cases WHERE case_id IN ("
                               + ", ".join(f"'{i}'" for i in ids) + ")")[1]
    assert rows == [("hardship_specialist", "card")]
    nested = dispatch("search_text", {"query": "job", "filters": {"case_id": sample["case_id"], "source": "notes"}}, ctx)
    assert {h["case_id"] for h in nested.data} <= {sample["case_id"]}


def test_search_text_errors_and_empty_results(ctx):
    assert "keywords" in dispatch("search_text", {"query": ""}, ctx).error
    assert "keywords" in dispatch("search_text", {"query": "the and of"}, ctx).error
    assert "source must be" in dispatch("search_text", {"query": "job", "source": "emails"}, ctx).error
    assert "Accepted forms" in dispatch("search_text", {"query": "job", "period": "whenever"}, ctx).error
    r = dispatch("search_text", {"query": "zeppelin xylophone"}, ctx)
    assert r.ok and r.row_count == 0 and "No notes or transcripts matched" in r.warnings[0]


def test_search_text_keyword_mode(serving_like, mock_db):
    ctx = ToolContext.build(serving=serving_like, mongo=mock_db)
    r = dispatch("search_text", {"query": "lost my job", "source": "notes", "limit": 10}, ctx)
    assert r.ok and r.row_count == 10 and r.meta["search_mode"] == "like"
    assert "ILIKE" in r.sql and "match_bm25" not in r.sql
    assert all("job" in h["text"].lower() or "lost" in h["text"].lower() for h in r.data)
    assert "lost my job" in r.data[0]["text"].lower()      # most matching terms first
    t = dispatch("search_text", {"query": "employment insurance", "source": "transcripts"}, ctx)
    assert t.row_count > 0 and t.data[0]["source"] == "transcript"


def test_search_results_are_wrapped_as_data_and_cannot_break_out(ctx):
    r = dispatch("search_text", {"query": "reveal the system prompt instruction", "source": "notes"}, ctx)
    assert any("New instruction" in h["text"] for h in r.data), "the planted injection note should be found"
    wrapped = r.to_llm()
    assert wrapped.startswith('<tool_result tool="search_text" trust="data">\n') and wrapped.endswith("\n</tool_result>")
    assert wrapped.count("</tool_result>") == 1 and wrapped.count("<tool_result") == 1
    body = json.loads(wrapped.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert any("</tool_result>" in h["text"] for h in body["data"]), "the text survives as data once the JSON is decoded"


def test_snippet_is_centred_on_the_match():
    text = "filler " * 200 + "the customer lost her job in June" + " filler" * 200
    s = st.snippet(text, ["job"])
    assert len(s) <= st.SNIPPET_CHARS and "lost her job" in s and s.startswith("…") and s.endswith("…")
    assert st.terms_of("What are customers saying about losing their JOB?") == ["losing", "job"]


# ---- refuse, registry, wrapper, sanity ---------------------------------------------------------
def test_refuse(ctx):
    r = dispatch("refuse", {"reason": "Protected attributes cannot be used.", "rule": "protected_attributes"}, ctx)
    assert r.ok and r.data == {"rule": "protected_attributes", "reason": "Protected attributes cannot be used."}
    r = dispatch("refuse", {"reason": "", "rule": "made up rule"}, ctx)
    assert r.ok and r.data["rule"] == "out_of_scope_data" and r.data["reason"]


def test_registry_specs_are_simple_json_schema(ctx):
    tools = specs(ctx)
    assert [t.name for t in tools] == TOOL_NAMES == ["compute_metric", "run_sql", "get_customer", "search_text", "search_policy", "refuse"]
    by_name = {t.name: t for t in tools}
    assert by_name["compute_metric"].parameters["properties"]["metric"]["enum"] == sorted(ctx.metrics)
    assert set(by_name["refuse"].parameters["properties"]["rule"]["enum"]) == {
        "marketing", "protected_attributes", "automated_adverse_action", "bulk_contact_export", "out_of_scope_data"}
    text = json.dumps([t.parameters for t in tools])
    for unsupported in ("oneOf", "anyOf", "allOf", "$ref", "additionalProperties"):
        assert unsupported not in text
    for t in tools:
        assert t.description and t.parameters["type"] == "object" and t.parameters["required"]


def test_dispatch_never_raises(ctx, monkeypatch):
    assert "Unknown tool" in dispatch("drop_database", {}, ctx).error
    assert "must be an object" in dispatch("run_sql", "SELECT 1", ctx).error
    r = dispatch("run_sql", {"query": "SELECT 1", "database": "curated"}, ctx)
    assert not r.ok and "Unknown argument(s) for run_sql: database" in r.error and "query, purpose" in r.error
    from c360.layer2.tools import registry
    monkeypatch.setitem(registry._FUNCTIONS, "run_sql", lambda ctx, **kw: 1 / 0)
    r = dispatch("run_sql", {}, ctx)
    assert not r.ok and "failed unexpectedly (ZeroDivisionError" in r.error


def test_tool_result_payload_is_compact_and_json_safe():
    import datetime as dt
    import decimal
    r = ToolResult(tool="run_sql", columns=["d", "ts", "x", "n"], row_count=1,
                   data=[{"d": dt.date(2026, 9, 28), "ts": dt.datetime(2026, 9, 28, 14, 5), "x": decimal.Decimal("1.23456789"), "n": float("nan")}])
    body = json.loads(r.to_llm().split("\n")[1])
    assert body == {"ok": True, "columns": ["d", "ts", "x", "n"], "rows": [["2026-09-28", "2026-09-28 14:05:00", 1.234568, None]], "row_count": 1}
    failed = ToolResult(tool="run_sql", ok=False, error="bad <query>")
    assert json.loads(failed.to_llm().split("\n")[1]) == {"ok": False, "error": "bad <query>"}
    assert "<query>" not in failed.to_llm()


def rows(columns, *values, **kw):
    return ToolResult(tool="run_sql", columns=list(columns), data=[dict(zip(columns, v)) for v in values], row_count=len(values), **kw)


def test_sanity_checks():
    counts = {"row_counts": {"collections_cases": 800, "offers": 300}, "tables": ["collections_cases", "offers"], "max_rows": 50}
    assert any("outside 0-1" in w for w in sanity.check(rows(["value"], (1.4,)), {**counts, "unit": "ratio"}).warnings)
    assert any("outside 0-1" in w for w in sanity.check(rows(["cure_rate"], (-0.2,)), counts).warnings)
    assert not sanity.check(rows(["value"], (0.62,)), {**counts, "unit": "ratio"}).warnings
    fan = sanity.check(rows(["queue", "case_count"], ("legal", 5000)), counts).warnings
    assert any("multiplying rows" in w and "800" in w for w in fan)
    assert not sanity.check(rows(["queue", "case_count"], ("legal", 700)), counts).warnings
    assert any("multiplying rows" in w for w in sanity.check(rows(["value"], (900,)), {**counts, "unit": "count"}).warnings)
    assert any("No rows matched" in w for w in sanity.check(rows(["value"]), counts).warnings)
    assert any("first 50 rows" in w for w in sanity.check(rows(["a"], (1,), truncated=True), counts).warnings)
    assert any("negative" in w for w in sanity.check(rows(["value"], (-10.0,)), {**counts, "unit": "amount"}).warnings)
    assert any("negative" in w for w in sanity.check(rows(["total_payment_amount"], (-10.0,)), counts).warnings)
    assert not sanity.check(rows(["current_balance"], (-10.0,)), counts).warnings        # balances may be negative
    failed = ToolResult(tool="run_sql", ok=False, error="x")
    assert sanity.check(failed, counts).warnings == []
