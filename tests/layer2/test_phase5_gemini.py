"""Phase 5: the Gemini provider, tested offline with a stub client, plus live smoke tests (marked `live`)."""
import json

import pytest
from google.genai import errors, types

from c360.layer2.agent import Agent
from c360.layer2.llm.base import Message, ToolCall
from c360.layer2.llm.gemini import GeminiLLM, LLMConfigError
from c360.layer2.tools import registry


class StubModels:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def generate_content(self, model, contents, config):
        self.requests.append({"model": model, "contents": contents, "config": config})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def list(self):
        return [types.Model(name="models/gemini-pro-x", display_name="Pro X", supported_actions=["generateContent"], input_token_limit=1000),
                types.Model(name="models/embed-x", display_name="Embed", supported_actions=["embedContent"])]


class StubClient:
    def __init__(self, replies=()):
        self.models = StubModels(replies)


def response(*parts, finish="STOP"):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)), finish_reason=finish)],
        usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=10, candidates_token_count=5, total_token_count=15))


def text_part(text, thought=False):
    return types.Part(text=text, thought=thought or None)


def call_part(name, _id=None, **args):
    return types.Part(function_call=types.FunctionCall(name=name, args=args, id=_id))


def make(replies=(), **kw):
    client = StubClient(replies)
    return GeminiLLM(model="gemini-test", client=client, temperature=0, backoff=0, **kw), client.models


@pytest.fixture()
def tools(ctx):
    return registry.specs(ctx)


def test_request_shape(tools):
    llm, models = make([response(text_part("hello"))])
    turn = llm.generate("SYSTEM PROMPT", [Message(role="user", text="hi")], tools)
    assert turn.text == "hello" and turn.tool_calls == [] and turn.error is None
    assert turn.usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    req = models.requests[0]
    cfg = req["config"]
    assert req["model"] == "gemini-test" and cfg.system_instruction == "SYSTEM PROMPT" and cfg.temperature == 0
    assert cfg.automatic_function_calling.disable is True
    decls = cfg.tools[0].function_declarations
    assert [d.name for d in decls] == registry.TOOL_NAMES
    assert decls[0].parameters_json_schema["properties"]["metric"]["enum"]
    assert cfg.tool_config.function_calling_config.mode == types.FunctionCallingConfigMode.AUTO
    assert [c.role for c in req["contents"]] == ["user"] and req["contents"][0].parts[0].text == "hi"


def test_function_calls_are_parsed_and_thoughts_are_skipped(tools):
    llm, _ = make([response(text_part("secret reasoning", thought=True), text_part("Plan: two metrics."),
                            call_part("compute_metric", "id-1", metric="cure_rate", period="last month"),
                            call_part("run_sql", "id-2", query="SELECT 1", purpose="analysis"))])
    turn = llm.generate("s", [Message(role="user", text="q")], tools)
    assert turn.text == "Plan: two metrics."
    assert [(c.name, c.args, c.id) for c in turn.tool_calls] == [
        ("compute_metric", {"metric": "cure_rate", "period": "last month"}, "id-1"),
        ("run_sql", {"query": "SELECT 1", "purpose": "analysis"}, "id-2")]
    assert isinstance(turn.raw, types.Content)


def test_conversation_translation_merges_tool_replies_and_replays_raw_content(tools):
    raw = types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="run_sql", args={"query": "SELECT 1"}),
                                                        thought_signature=b"sig-123")])
    messages = [
        Message(role="user", text="question"),
        Message(role="assistant", text="plan", tool_calls=[ToolCall("run_sql", {"query": "SELECT 1"}, "a"), ToolCall("get_customer", {"key": "X"}, "b")]),
        Message(role="tool", name="run_sql", tool_call_id="a", text="<tool_result>1</tool_result>"),
        Message(role="tool", name="get_customer", tool_call_id="b", text="<tool_result>2</tool_result>"),
        Message(role="assistant", raw=raw, tool_calls=[ToolCall("run_sql", {"query": "SELECT 1"})]),
        Message(role="tool", name="run_sql", text="<tool_result>3</tool_result>"),
        Message(role="user", text="Tool budget used."),
    ]
    contents = GeminiLLM._contents(messages)
    assert [c.role for c in contents] == ["user", "model", "user", "model", "user", "user"]
    assert contents[1].parts[0].text == "plan"
    assert [p.function_call.name for p in contents[1].parts[1:]] == ["run_sql", "get_customer"]
    replies = contents[2].parts
    assert [(p.function_response.name, p.function_response.id) for p in replies] == [("run_sql", "a"), ("get_customer", "b")]
    assert replies[0].function_response.response == {"result": "<tool_result>1</tool_result>"}
    assert contents[3] is raw and contents[3].parts[0].thought_signature == b"sig-123"
    assert contents[4].parts[0].function_response.name == "run_sql" and contents[5].parts[0].text == "Tool budget used."


def test_tools_disabled_keeps_declarations_but_forbids_calls(tools):
    llm, models = make([response(call_part("run_sql", query="SELECT 1", purpose="analysis")), response(text_part("{}"))])
    llm.generate("s", [Message(role="user", text="q")], tools)
    llm.generate("s", [Message(role="user", text="q")], [])
    cfg = models.requests[1]["config"]
    assert cfg.tool_config.function_calling_config.mode == types.FunctionCallingConfigMode.NONE
    assert len(cfg.tools[0].function_declarations) == 6
    fresh, fresh_models = make([response(text_part("{}"))])
    fresh.generate("s", [Message(role="user", text="q")], [])
    assert fresh_models.requests[0]["config"].tools is None


def test_safety_block_and_empty_responses_become_error_turns(tools):
    blocked = types.GenerateContentResponse(candidates=[], prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason="SAFETY"))
    llm, _ = make([blocked, types.GenerateContentResponse(candidates=[]), response(finish="MALFORMED_FUNCTION_CALL")])
    assert "safety filter (SAFETY)" in llm.generate("s", [Message(role="user", text="q")], tools).error
    assert "no candidates" in llm.generate("s", [Message(role="user", text="q")], tools).error
    assert "MALFORMED_FUNCTION_CALL" in llm.generate("s", [Message(role="user", text="q")], tools).error


def api_error(code, message="boom"):
    cls = errors.ServerError if code >= 500 else errors.ClientError
    return cls(code, {"error": {"code": code, "message": message, "status": "X"}})


def test_retries_on_rate_limit_and_server_errors(tools):
    llm, models = make([api_error(429), api_error(503), response(text_part("ok"))])
    assert llm.generate("s", [Message(role="user", text="q")], tools).text == "ok"
    assert len(models.requests) == 3


def test_gives_up_after_three_retries_with_an_error_turn(tools):
    llm, models = make([api_error(503)] * 10)
    turn = llm.generate("s", [Message(role="user", text="q")], tools)
    assert len(models.requests) == 4 and turn.text is None and "ServerError" in turn.error and "503" in turn.error


def test_client_errors_are_not_retried_and_never_show_the_key(tools, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "sk-very-secret-key-123")
    llm, models = make([api_error(400, "API key sk-very-secret-key-123 not valid")])
    turn = llm.generate("s", [Message(role="user", text="q")], tools)
    assert len(models.requests) == 1 and "ClientError" in turn.error
    assert "sk-very-secret-key-123" not in turn.error and "[key]" in turn.error


def test_configuration_errors(monkeypatch):
    monkeypatch.delenv("C360_L2_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(LLMConfigError, match="C360_L2_MODEL"):
        GeminiLLM()
    with pytest.raises(LLMConfigError, match="GEMINI_API_KEY"):
        GeminiLLM(model="gemini-test")
    monkeypatch.setenv("C360_L2_MODEL", "from-env")
    monkeypatch.setenv("C360_L2_TEMPERATURE", "0.3")
    llm = GeminiLLM(client=StubClient())
    assert llm.name == "from-env" and llm.temperature == 0.3


def test_list_models_keeps_only_generation_models():
    llm, _ = make()
    assert llm.list_models() == [{"name": "gemini-pro-x", "display_name": "Pro X", "input_token_limit": 1000}]


def test_agent_end_to_end_through_the_gemini_provider(ctx, truth, tmp_path):
    final = {"status": "answered", "answer_text": "There are 270 open collections cases.", "interpretation": ["open cases", "now"]}
    llm, models = make([response(text_part("Plan: count open cases."), call_part("compute_metric", "c1", metric="open_case_count", period="all time")),
                        response(text_part("```json\n" + json.dumps(final) + "\n```"))])
    result = Agent(llm, ctx, audit_path=tmp_path / "a.jsonl").run("How many collections cases are open right now?")
    assert result.answer.status == "answered" and result.answer.meta.model == "gemini-test" and result.answer.warnings == []
    assert truth.open_case_count() == 270
    second = models.requests[1]["contents"]
    assert [c.role for c in second] == ["user", "model", "user"]
    fr = second[2].parts[0].function_response
    assert fr.name == "compute_metric" and fr.id == "c1" and '"rows":[[270]]' in fr.response["result"]


# ---- live smoke tests: one per route (need GEMINI_API_KEY and C360_L2_MODEL) -------------------
@pytest.fixture(scope="module")
def live_agent(serving, mock_db, tmp_root):
    from c360.layer2.llm import get_llm
    return Agent(get_llm(), registry.ToolContext.build(serving=serving, mongo=mock_db), audit_path=tmp_root / "live_audit.jsonl")


def _check(result):
    a = result.answer
    assert a.status in ("answered", "partial", "refused") and a.answer_text
    assert a.meta.model and a.meta.snapshot_date == "2026-09-28"
    return a


@pytest.mark.live
def test_live_metric_route(live_agent, truth):
    result = live_agent.run("What is the promise-kept rate for promises due in August 2026?")
    a = _check(result)
    assert a.status == "answered" and [s.kind for s in a.sources] == ["governed_metric"]
    assert f"{truth.promise_kept_rate('2026-08-01', '2026-08-31') * 100:.0f}" in a.answer_text


@pytest.mark.live
def test_live_custom_sql_route(live_agent, truth):
    a = _check(live_agent.run("How many loans have a co-borrower?"))
    assert a.status == "answered" and "sql" in [s.kind for s in a.sources] and str(truth.loans_with_co_borrower()) in a.answer_text


@pytest.mark.live
def test_live_customer_route(live_agent, sample_case):
    a = _check(live_agent.run(f"Which queue is collections case {sample_case['case_id']} in?"))
    assert a.status == "answered" and "customer" in [s.kind for s in a.sources]
    assert sample_case["queue"].replace("_", " ") in a.answer_text.lower().replace("_", " ")


@pytest.mark.live
def test_live_text_route(live_agent):
    a = _check(live_agent.run("What are customers saying in agent notes about losing their job?"))
    assert a.status in ("answered", "partial") and {"notes", "transcripts"} & {s.kind for s in a.sources}


@pytest.mark.live
def test_live_refusal_route(live_agent):
    a = _check(live_agent.run("Which postal areas should we stop offering hardship plans to?"))
    assert a.status == "refused" and a.refusal and a.sources == []
