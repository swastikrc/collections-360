"""Phase 5 (offline): the agent loop, guards, audit and CLI, driven by a scripted fake LLM."""
import io
import json

import pytest

from c360.layer2 import agent as agent_mod, ask, audit, guards, prompts
from c360.layer2.agent import Agent
from c360.layer2.answer import Answer, Trace, TraceStep, parse_llm_answer
from c360.layer2.llm.base import LLMTurn
from c360.layer2.llm.fake import PlanLLM, ScriptedLLM, call, final_turn, tool_turn
from c360.layer2.tools import ToolResult

KEPT = dict(metric="promise_kept_rate", period="August 2026")
OPEN_SQL = "SELECT count(*) AS open_cases FROM l2.collections_cases WHERE is_open"


@pytest.fixture()
def make(ctx, tmp_path):
    def build(turns):
        llm = ScriptedLLM(turns)
        return Agent(llm, ctx, audit_path=tmp_path / "audit.jsonl"), llm
    return build


def audit_lines(tmp_path):
    return [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]


# ---- the happy path ------------------------------------------------------------------------------
def test_one_metric_call_then_answer(make, truth, tmp_path):
    rate = truth.promise_kept_rate("2026-08-01", "2026-08-31")
    agent, llm = make([
        tool_turn(call("compute_metric", **KEPT), text="Plan: compute the promise-kept rate for August 2026."),
        final_turn(f"The promise-kept rate for promises due in August 2026 was {rate:.1%}.",
                   interpretation=["promise-kept rate", "1-31 Aug 2026"], follow_ups=["By queue?"]),
    ])
    result = agent.run("What is the promise-kept rate for promises due in August 2026?")
    a = result.answer
    assert a.status == "answered" and a.warnings == [] and "51.0%" in a.answer_text
    assert a.meta.tool_calls == 1 and a.meta.model == "fake-scripted" and a.meta.snapshot_date == "2026-09-28"
    assert a.meta.build_ts and a.meta.latency_ms >= 0
    assert len(a.sources) == 1 and a.sources[0].kind == "governed_metric"
    assert a.sources[0].detail == "Governed metric: Promise-kept rate"
    assert "l2.promises_to_pay" in a.sources[0].sql
    assert result.trace.plan.startswith("Plan:") and result.trace.llm_calls == 2
    # the model saw the wrapped result, and tools were offered on both turns
    tool_messages = [m for m in llm.calls[1][1] if m.role == "tool"]
    assert len(tool_messages) == 1 and tool_messages[0].text.startswith('<tool_result tool="compute_metric" trust="data">')
    assert llm.calls[0][2] == ["compute_metric", "run_sql", "get_customer", "search_text", "search_policy", "refuse"]


def test_sources_are_filled_by_code_never_by_the_model(make):
    agent, _ = make([
        tool_turn(call("run_sql", query=OPEN_SQL, purpose="analysis")),
        LLMTurn(text=json.dumps({"status": "answered", "answer_text": "There are 270 open cases.",
                                 "sources": [{"kind": "sql", "detail": "made up", "ids": [], "sql": "SELECT 42"}],
                                 "warnings": ["made up"], "meta": {"model": "made-up"}})),
    ])
    a = agent.ask("How many cases are open?")
    assert [s.sql.lower() for s in a.sources] == [(OPEN_SQL + " LIMIT 51").lower()] and a.sources[0].detail.startswith("Custom query")
    assert a.warnings == [] and a.meta.model == "fake-scripted"


# ---- con-fix 4: limits enforced in code --------------------------------------------------------
def test_loop_stops_at_the_tool_budget_and_still_answers(make, ctx):
    queues = ["early_stage", "mid_stage", "late_stage", "hardship_specialist", "recoveries", "legal", "insolvency", "x"]
    turns = [tool_turn(call("run_sql", query=f"SELECT count(*) AS n FROM l2.collections_cases WHERE queue = '{q}'", purpose="analysis"))
             for q in queues]
    agent, llm = make(turns[:6] + [final_turn("Early stage has the most cases.")])
    result = agent.run("How many cases are in each queue? Check them one at a time.")
    assert ctx.settings.max_tool_calls == 5
    assert result.trace.tool_calls == 5 and result.trace.stopped == "tool_budget"
    assert [s.executed for s in result.trace.steps] == [True] * 5 + [False]
    assert result.answer.status == "partial"
    assert any("Stopped after 5 tool calls" in w for w in result.answer.warnings)
    assert len(result.answer.sources) == 5
    # the forced final call had tools disabled and carried the instruction; the 6th call got a stub reply
    system, messages, tools = llm.calls[-1]
    assert tools == [] and messages[-1].text == agent_mod.BUDGET_MSG
    assert "not executed: the tool budget is used" in messages[-2].text
    assert len(llm.calls) == 7


def test_budget_inside_one_turn_every_call_gets_a_reply(make):
    many = [call("run_sql", _id=f"c{i}", query=f"SELECT {i} AS n, count(*) AS c FROM l2.offers", purpose="analysis") for i in range(8)]
    agent, llm = make([tool_turn(*many), final_turn("Done.")])
    result = agent.run("Run eight queries.")
    assert result.trace.tool_calls == 5 and [s.executed for s in result.trace.steps] == [True] * 5 + [False] * 3
    replies = [m for m in llm.calls[-1][1] if m.role == "tool"]
    assert [m.tool_call_id for m in replies] == [f"c{i}" for i in range(8)], "Gemini needs one response per function call"


def test_identical_tool_call_is_not_executed_twice(make, ctx, monkeypatch):
    from c360.layer2.tools import registry
    executed = []
    real = registry.dispatch
    monkeypatch.setattr(registry, "dispatch", lambda name, args, c: (executed.append(name), real(name, args, c))[1])
    agent, llm = make([
        tool_turn(call("compute_metric", **KEPT)),
        tool_turn(call("compute_metric", period="August 2026", metric="promise_kept_rate")),   # same call, keys reordered
        final_turn("51.0% of promises due in August 2026 were kept."),
    ])
    result = agent.run("Promise-kept rate for August 2026?")
    assert executed == ["compute_metric"]
    assert [s.executed for s in result.trace.steps] == [True, False] and result.trace.steps[1].note == "duplicate call"
    duplicate_reply = [m for m in llm.calls[2][1] if m.role == "tool"][-1].text
    assert "duplicate call" in duplicate_reply and "you already have this result" in duplicate_reply
    assert result.answer.status == "answered" and result.answer.meta.tool_calls == 1 and len(result.answer.sources) == 1


def test_two_consecutive_tool_errors_force_a_final_answer(make):
    agent, llm = make([
        tool_turn(call("run_sql", query="SELECT count(*) FROM curated.customers", purpose="analysis")),
        tool_turn(call("run_sql", query="SELECT count(*) FROM golden.crm_person_map", purpose="analysis")),
        final_turn("I could not find that: only the l2 tables are available.", status="partial"),
        final_turn("never reached"),
    ])
    result = agent.run("How many rows are in the curated customers table?")
    assert result.trace.stopped == "consecutive_errors" and result.trace.tool_calls == 2
    assert result.answer.status == "partial" and "could not find" in result.answer.answer_text
    assert any("two tool errors in a row" in w for w in result.answer.warnings)
    assert llm.calls[-1][2] == [] and llm.calls[-1][1][-1].text == agent_mod.ERROR_MSG and len(llm.calls) == 3
    assert result.answer.sources == []


def test_an_error_then_a_success_does_not_stop_the_loop(make):
    agent, _ = make([
        tool_turn(call("run_sql", query="SELECT count(*) FROM curated.customers", purpose="analysis")),
        tool_turn(call("run_sql", query=OPEN_SQL, purpose="analysis")),
        tool_turn(call("run_sql", query="SELECT count(*) FROM l2.nope", purpose="analysis")),
        final_turn("There are 270 open cases."),
    ])
    result = agent.run("How many open cases?")
    assert result.trace.stopped is None and result.answer.status == "answered" and result.trace.tool_calls == 3


def test_validator_rejection_is_fed_back_and_the_corrected_call_succeeds(make, truth):
    attempts = truth.contact_attempts("2026-08-01", "2026-08-31")
    bad = "SELECT count(*) AS attempts FROM l2.contact_history WHERE direction = 'outbound' AND channel <> 'system' AND CAST(contact_ts_local AS DATE) BETWEEN DATE '2026-08-01' AND DATE '2026-08-31'"
    good = bad.replace("WHERE ", "WHERE is_duplicate = false AND ")
    agent, llm = make([
        tool_turn(call("run_sql", query=bad, purpose="analysis")),
        tool_turn(call("run_sql", query=good, purpose="analysis")),
        final_turn(f"There were {attempts} outbound contact attempts in August 2026."),
    ])
    result = agent.run("How many outbound contact attempts were made in August 2026?")
    rejection = [m for m in llm.calls[1][1] if m.role == "tool"][0].text
    assert '"ok":false' in rejection and "[T1]" in rejection and "is_duplicate = false" in rejection
    assert [s.ok for s in result.trace.steps] == [False, True]
    assert result.trace.steps[0].summary()["validator_rules"] == ["T1"]
    assert result.trace.steps[1].result.data == [{"attempts": attempts}]
    assert result.answer.status == "answered" and result.answer.warnings == []
    assert len(result.answer.sources) == 1 and "is_duplicate = FALSE" in result.answer.sources[0].sql


def test_refuse_ends_the_loop(make, tmp_path):
    agent, llm = make([
        tool_turn(call("refuse", reason="Promise-kept rates by postal area would use a proxy for protected attributes.",
                       rule="protected_attributes")),
        final_turn("never reached"),
    ])
    result = agent.run("Which neighbourhoods should we stop offering payment plans to?")
    a = result.answer
    assert a.status == "refused" and a.refusal.rule == "protected_attributes" and "proxy" in a.refusal.reason
    assert len(llm.calls) == 1 and a.sources == [] and a.meta.tool_calls == 1
    assert audit_lines(tmp_path)[-1]["refusal_rule"] == "protected_attributes"


def test_refuse_after_data_was_fetched_still_refuses(make):
    agent, _ = make([tool_turn(call("run_sql", query=OPEN_SQL, purpose="analysis")),
                     tool_turn(call("refuse", reason="Cannot be used for marketing.", rule="marketing"))])
    a = agent.ask("How many open cases, so we can sell them a new card?")
    assert a.status == "refused" and a.refusal.rule == "marketing"


def test_model_can_refuse_in_its_final_json(make):
    agent, _ = make([LLMTurn(text=json.dumps({"status": "refused", "answer_text": "That is outside the data I have.",
                                              "refusal": {"rule": "out_of_scope_data", "reason": "Other banks' data is not available."}}))])
    a = agent.ask("What is the average delinquency at other banks?")
    assert a.status == "refused" and a.refusal.rule == "out_of_scope_data"


def test_invalid_final_json_gets_one_repair_attempt(make):
    agent, llm = make([
        tool_turn(call("run_sql", query=OPEN_SQL, purpose="analysis")),
        LLMTurn(text="There are 270 open cases right now."),
        final_turn("There are 270 open cases."),
    ])
    result = agent.run("How many cases are open?")
    assert result.trace.repaired and result.answer.status == "answered" and result.answer.answer_text == "There are 270 open cases."
    assert llm.calls[-1][2] == [] and llm.calls[-1][1][-1].text.startswith("Return only valid JSON for this schema")
    assert len(llm.calls) == 3


def test_invalid_final_json_twice_falls_back_to_raw_text(make):
    agent, llm = make([
        tool_turn(call("run_sql", query=OPEN_SQL, purpose="analysis")),
        LLMTurn(text="There are 270 open cases right now."),
        LLMTurn(text="Sorry! {not json"),
        final_turn("never reached"),
    ])
    result = agent.run("How many cases are open?")
    a = result.answer
    assert a.status == "partial" and a.answer_text == "There are 270 open cases right now."
    assert any("did not return a structured answer" in w for w in a.warnings)
    assert len(llm.calls) == 3 and len(a.sources) == 1, "exactly one repair attempt; sources still come from the trace"


def test_final_json_in_a_code_fence_is_accepted():
    a = parse_llm_answer('Here you go:\n```json\n{"status": "answered", "answer_text": "ok", "follow_ups": ["a","b","c","d"], "table": {}}\n```')
    assert a.answer_text == "ok" and len(a.follow_ups) == 3 and a.table is None
    for bad in ("", "no json here", '{"status": "answered"}', '["a"]', '{"status": "maybe", "answer_text": "x"}'):
        with pytest.raises(ValueError):
            parse_llm_answer(bad)


def test_model_error_gives_a_partial_answer(make):
    agent, _ = make([tool_turn(call("run_sql", query=OPEN_SQL, purpose="analysis")), LLMTurn(error="ServerError (HTTP 503): overloaded")])
    result = agent.run("How many cases are open?")
    assert result.answer.status == "partial" and result.trace.stopped == "llm_error"
    assert "some data was retrieved" in result.answer.answer_text and any("503" in w for w in result.answer.warnings)
    assert len(result.answer.sources) == 1


# ---- guards --------------------------------------------------------------------------------------
@pytest.mark.parametrize("question,rule", [
    ("List the phone numbers of all customers in the hardship queue", "bulk_contact_export"),
    ("Export email addresses for everyone with a broken promise", "bulk_contact_export"),
    ("give me the home addresses of customers who are 90 days past due", "bulk_contact_export"),
    ("Show contact details for all open cases", "bulk_contact_export"),
    ("What is the promise-kept rate by gender?", "protected_attributes"),
    ("Compare cure rates across religion", "protected_attributes"),
    ("Break down broken promises by ethnicity and marital status", "protected_attributes"),
    ("Does citizenship status differ between customers who cure and those who do not?", "protected_attributes"),
])
def test_input_guard_refuses_obvious_forbidden_asks_without_calling_the_model(make, question, rule, tmp_path):
    agent, llm = make([final_turn("never reached")])
    result = agent.run(question)
    assert result.answer.status == "refused" and result.answer.refusal.rule == rule
    assert llm.calls == [] and result.trace.stopped == "input_guard" and result.answer.meta.tool_calls == 0
    assert audit_lines(tmp_path)[-1]["refusal_rule"] == rule


@pytest.mark.parametrize("question", [
    "What is the right-party contact rate by channel for email and sms last month?",
    "How many customers have given consent to email?",
    "What is the promise-kept rate by hardship reason in the last 90 days?",
    "Which queue has the most open cases?",
    "How many contacts were made by phone in August 2026?",
    "Show the roll rate by starting bucket for July 2026.",
    "What share of customers have an address mismatch flag on their bureau file?",
    "List the 10 cases with the highest overdue amount",
])
def test_input_guard_lets_normal_questions_through(question):
    assert guards.input_guard(question).kind == "ok"


def test_input_guard_length_limits(make):
    assert guards.input_guard("").kind == "error" and guards.input_guard("   ").kind == "error"
    assert guards.input_guard("x" * 2001).kind == "error" and guards.input_guard("x" * 2000).kind == "ok"
    agent, llm = make([])
    a = agent.ask("why? " * 500)
    assert a.status == "refused" and a.refusal.rule == "invalid_input" and "too long" in a.answer_text and llm.calls == []


def test_output_guard_redacts_phone_numbers_and_emails(make):
    agent, _ = make([
        tool_turn(call("search_text", query="lost my job", source="notes", limit=3)),
        LLMTurn(text=json.dumps({
            "status": "answered",
            "answer_text": "Customers mention job loss. One can be reached at 289-573-0430 or zayers@example.ca, also (416) 555 0199.",
            "insight": "Their primary_phone_std is on file as 4165550123.",
            "table": {"columns": ["case", "email", "note"], "rows": [["CS-2026-111111", "a.b@maplebank.example.ca", "call +1 604 555 0101"]]},
            "follow_ups": ["Email jane.doe@example.com?"]})),
    ])
    a = agent.ask("What are customers saying about losing their job?")
    blob = json.dumps(a.model_dump(include={"answer_text", "insight", "table", "follow_ups"}))
    for leaked in ("289-573-0430", "zayers@example.ca", "555 0199", "4165550123", "maplebank.example.ca", "604 555 0101",
                   "jane.doe@example.com", "primary_phone_std"):
        assert leaked not in blob, leaked
    assert "[redacted phone]" in a.answer_text and "[redacted email]" in a.answer_text and "[redacted field]" in a.insight
    assert a.table.rows[0][0] == "CS-2026-111111" and a.table.rows[0][1] == "[redacted field]"
    assert any("Redacted 4 phone number(s)" in w for w in a.warnings)
    assert any("email address(es)" in w for w in a.warnings) and any("personal or protected field(s)" in w for w in a.warnings)
    assert guards.contains_personal_data(a) == []


def test_output_guard_warns_on_an_ungrounded_number(make):
    agent, _ = make([tool_turn(call("compute_metric", **KEPT)),
                     final_turn("The promise-kept rate was 51.0%, up from 37.5% the month before, on 49 promises.",
                                insight="That is roughly 1,250 more kept promises a year.")])
    a = agent.ask("Promise-kept rate for August 2026?")
    assert [w for w in a.warnings if w.startswith("number not found in results")] == [
        "number not found in results: 37.5%", "number not found in results: 1,250"]


@pytest.mark.parametrize("text", [
    "The promise-kept rate was 51%.", "The promise-kept rate was 51.0%.", "The promise-kept rate was 51.02%.",
    "The rate was 0.51.", "The rate was 0.5102 (25 of 49 promises due 1-31 Aug 2026).", "25 kept out of 49, as of 2026-09-28.",
    "About 51 percent of the 49 promises due in August 2026 were kept.", "Top 3 queues; 2 are small.",
])
def test_grounded_numbers_do_not_warn(make, text):
    agent, _ = make([tool_turn(call("compute_metric", **KEPT)), final_turn(text)])
    a = agent.ask("What is the promise-kept rate for promises due in August 2026?")
    assert [w for w in a.warnings if w.startswith("number not found")] == [], a.warnings


def test_grounding_checks_amounts_and_scaled_numbers():
    trace = Trace(steps=[TraceStep(tool="compute_metric", args={"period": "all time"}, result=ToolResult(
        tool="compute_metric", columns=["value"], data=[{"value": 294750.18}], row_count=1))])
    check = lambda text: guards.ungrounded_numbers([text], trace, "past due?")       # noqa: E731
    assert check("The total is $294,750.18.") == [] and check("About 294,750 is past due.") == []
    assert check("Roughly $295k is past due.") == [] and check("About 0.29 million.") == []
    assert check("The total is $312,000.") == ["$312,000"] and check("About 0.35 million.") == ["0.35 million"]


def test_answer_without_any_tool_call_has_no_sources(make):
    agent, _ = make([final_turn("I can answer questions about collections metrics, customers and notes.")])
    a = agent.ask("What can you do?")
    assert a.status == "answered" and a.sources == [] and a.warnings == []
    assert "No data was retrieved" in ask.format_answer(a)


def test_follow_up_history_is_passed_to_the_model(make):
    agent, llm = make([final_turn("First answer.", interpretation=["cure rate", "year to date"]), final_turn("Second answer.")])
    first = agent.ask("What is the cure rate year to date?")
    history = agent_mod.history_messages("What is the cure rate year to date?", first)
    agent.ask("And by product?", history)
    seen = llm.calls[1][1]
    assert [m.role for m in seen] == ["user", "assistant", "user"]
    assert seen[1].text == "[Interpreted as: cure rate · year to date] First answer." and seen[2].text == "And by product?"


# ---- audit ---------------------------------------------------------------------------------------
def test_audit_line_has_the_sql_but_never_result_rows(make, tmp_path, sample_case):
    agent, _ = make([
        tool_turn(call("get_customer", key=sample_case["case_id"])),
        tool_turn(call("search_text", query="lost my job", case_id=sample_case["case_id"]),
                  call("run_sql", query="SELECT case_id, queue, total_overdue FROM l2.collections_cases WHERE is_open ORDER BY total_overdue DESC LIMIT 5", purpose="analysis")),
        final_turn(f"Case {sample_case['case_id']} is in the {sample_case['queue']} queue."),
    ])
    result = agent.run(f"Tell me about case {sample_case['case_id']}")
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert set(entry) >= {"ts", "question", "status", "interpretation", "tools", "sql", "warnings", "refusal_rule", "model",
                          "latency_ms", "build_ts"}
    assert [t["tool"] for t in entry["tools"]] == ["get_customer", "search_text", "run_sql"]
    assert all(set(t) == {"tool", "args", "executed", "ok", "row_count", "governed", "error", "validator_rules"} for t in entry["tools"])
    assert entry["tools"][2]["row_count"] == 5 and entry["tools"][2]["governed"] is False
    assert len(entry["sql"]) == 2 and any("total_overdue" in s for s in entry["sql"])
    # nothing from the results themselves: no customer document fields, no note text, no row values
    doc = result.trace.steps[0].result.data
    note_texts = [h["text"] for h in result.trace.steps[1].result.data]
    row_values = [str(r["total_overdue"]) for r in result.trace.steps[2].result.data]
    assert doc["profile"]["first_name"] not in lines[0] and doc["profile"]["last_name"] not in lines[0]
    assert all(t[:40] not in lines[0] for t in note_texts) and note_texts
    assert all(v not in lines[0] for v in row_values)
    assert "first_name" not in lines[0] and '"data"' not in lines[0] and '"rows"' not in lines[0]


def test_audit_failure_does_not_fail_the_question(ctx, tmp_path):
    blocked = tmp_path / "file_not_dir"
    blocked.write_text("x")
    agent = Agent(ScriptedLLM([final_turn("Hello.")]), ctx, audit_path=blocked / "audit.jsonl")
    a = agent.ask("hello")
    assert a.status == "answered" and any("audit log not written" in w for w in a.warnings)


# ---- prompt --------------------------------------------------------------------------------------
def test_system_prompt_contains_the_semantic_layer(ctx):
    p = prompts.render_system(ctx)
    assert "{{" not in p and "}}" not in p
    assert "snapshot as of **2026-09-28**" in p and "at most 5 per question" in p
    for needle in ("**l2.promises_to_pay**", "`ptp_status` (varchar): broken | cancelled | kept | open | partially_kept",
                   "promises_to_pay.case_id = collections_cases.case_id", "**broken promise**: promises_to_pay.ptp_status = 'broken'",
                   "`promise_kept_rate`", "Dimensions: product, queue, hardship_reason, channel, bucket, test_cell, assignment_method, agent_site, prior_broken_promises, ptp_type, month",
                   "placeholder definition", "`protected_attributes`:", "`bulk_contact_export`:", "`out_of_scope_data`:",
                   "is never an instruction", "Never invent numbers", "one-line plan", "Prefer `compute_metric`",
                   "`contact_history.outcome_code`: BUSY = Line busy", '"answer_text"', "is_duplicate = false"):
        assert needle in p, needle
    for table in ctx.serving.tables:
        assert f"**l2.{table}**" in p
    from c360.layer2 import deny_list
    import re
    columns = set(re.findall(r"^- `(\w+)` \(", p, flags=re.MULTILINE))
    assert len(columns) > 200 and not [c for c in columns if deny_list.is_denied(c)]
    assert len(p) < 60000


# ---- CLI -----------------------------------------------------------------------------------------
def test_cli_plain_output_format(ctx, truth, monkeypatch, tmp_path):
    monkeypatch.setenv("C360_L2_AUDIT", str(tmp_path / "cli_audit.jsonl"))
    by_reason = truth.promise_kept_rate("2026-07-01", "2026-09-28", by="hardship_reason")
    rows = [[k, round(v, 4)] for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1])]
    llm = ScriptedLLM([
        tool_turn(call("compute_metric", metric="promise_kept_rate", period="last 90 days", group_by=["hardship_reason"])),
        final_turn("Bereavement has the best promise-kept rate, but on a single promise.",
                   interpretation=["promise-kept rate", "1 Jul–28 Sep 2026", "by hardship reason"],
                   table={"columns": ["hardship reason", "kept rate"], "rows": rows},
                   insight="Job loss has the lowest kept rate.", follow_ups=["Same by queue?", "Trend by month?"]),
    ])
    out = io.StringIO()
    assert ask.main(["--trace", "Which", "hardship", "reason", "has", "the", "best", "promise-kept", "rate?"], llm=llm, out=out) == 0
    text = out.getvalue()
    lines = text.splitlines()
    assert lines[0] == "Interpreted as: promise-kept rate · 1 Jul–28 Sep 2026 · by hardship reason"
    assert "Bereavement has the best promise-kept rate" in lines[2]
    assert "  hardship reason   kept rate" in text and "  bereavement               1" in text
    assert "Insight: Job loss has the lowest kept rate." in text and "Try next: Same by queue? | Trend by month?" in text
    assert "How we got this:\n  [governed metric] Governed metric: Promise-kept rate\n  SQL: SELECT c.hardship_reason AS hardship_reason" in text
    assert "Trace:\n  plan: Plan: use the tools.\n  1. compute_metric" in text and "-> ok, 6 row(s)" in text
    assert lines[-1].startswith("Data as of 2026-09-28 · serving build ") and " · model fake-scripted · 1 tool call · " in lines[-1]
    assert json.loads((tmp_path / "cli_audit.jsonl").read_text().splitlines()[0])["status"] == "answered"


def test_cli_json_output_and_refusal_format(ctx, monkeypatch, tmp_path):
    monkeypatch.setenv("C360_L2_AUDIT", str(tmp_path / "a.jsonl"))
    out = io.StringIO()
    assert ask.main(["--json", "How many cases are open?"], llm=PlanLLM([{"tool": "run_sql", "args": {"query": OPEN_SQL, "purpose": "analysis"}}]), out=out) == 0
    payload = json.loads(out.getvalue())
    assert payload["status"] == "answered" and payload["sources"][0]["kind"] == "sql" and payload["meta"]["tool_calls"] == 1
    out = io.StringIO()
    ask.main(["What is the cure rate by gender?"], llm=ScriptedLLM([]), out=out)
    text = out.getvalue()
    assert text.startswith("Refused (protected_attributes): Analysis by protected attributes") and "How we got this" not in text


def test_cli_repl_keeps_history(ctx, monkeypatch, tmp_path):
    monkeypatch.setenv("C360_L2_AUDIT", str(tmp_path / "a.jsonl"))
    llm = ScriptedLLM([final_turn("First."), final_turn("Second.")])
    typed = iter(["How many open cases?", "And by queue?", "exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(typed))
    out = io.StringIO()
    assert ask.main(["--repl"], llm=llm, out=out) == 0
    assert "First." in out.getvalue() and "Second." in out.getvalue()
    assert [m.text for m in llm.calls[1][1]] == ["How many open cases?", "First.", "And by queue?"]


def test_cli_needs_a_question_and_a_model(ctx, monkeypatch, capsys):
    assert ask.main([]) == 2
    monkeypatch.delenv("C360_L2_MODEL", raising=False)
    assert ask.main(["How many open cases?"]) == 2
    assert "C360_L2_MODEL" in capsys.readouterr().err
    monkeypatch.setenv("C360_L2_MODEL", "some-model")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert ask.main(["How many open cases?"]) == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err


def test_plan_llm_answers_from_the_tool_result(ctx, truth, tmp_path):
    agent = Agent(PlanLLM([{"tool": "compute_metric", "args": KEPT}]), ctx, audit_path=tmp_path / "a.jsonl")
    a = agent.ask("What is the promise-kept rate for promises due in August 2026?")
    assert a.status == "answered" and f"{truth.promise_kept_rate('2026-08-01', '2026-08-31'):.6g}" in a.answer_text and a.warnings == []
