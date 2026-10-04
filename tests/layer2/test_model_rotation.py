"""Model rotation (llm/rotation.py): offline, with a stub client and a fake clock."""
from google.genai import errors, types

from c360.layer2.llm import get_llm
from c360.layer2.llm.base import Message, ToolCall
from c360.layer2.llm.rotation import SKIP_SIGNATURE, RotatingGeminiLLM, classify, parse_pool


class Models:
    """Replies per model name: a list of responses or exceptions, consumed in order."""
    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.requests = []

    def generate_content(self, model, contents, config):
        self.requests.append({"model": model, "contents": contents})
        reply = self.script[model].pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class Client:
    def __init__(self, script):
        self.models = Models(script)


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def ok(text="fine", signature=None):
    part = types.Part(function_call=types.FunctionCall(name="run_sql", args={"query": "SELECT 1"}), thought_signature=signature) \
        if signature else types.Part(text=text)
    return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[part]), finish_reason="STOP")])


def limit(per="Minute", delay="30s"):
    body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota.",
                      "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                   "violations": [{"quotaId": f"GenerateRequestsPer{per}PerProjectPerModel-FreeTier"}]},
                                  {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}}
    return errors.ClientError(429, body)


def overloaded():
    return errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "The model is overloaded."}})


def rot(script, pool="a,b,c", max_wait=75.0):
    clock, logs = Clock(), []
    llm = RotatingGeminiLLM(pool, client=Client(script), max_wait=max_wait, clock=clock, sleep=clock.sleep, log=logs.append)
    return llm, clock, logs


def ask(llm, text="q"):
    return llm.generate("system", [Message(role="user", text=text)], [])


def test_pool_and_error_classification():
    assert parse_pool(" a, b:10 ,a, c:x ") == [("a", None), ("b", 10), ("c", None)]
    assert classify(limit("Minute", "42s")) == ("minute", 42.0)
    assert classify(limit("Day", "57700s")) == ("day", 57700.0)
    assert classify(overloaded())[0] == "overload"
    assert classify(errors.ClientError(400, {"error": {"code": 400, "message": "bad request"}}))[0] == "fatal"


def test_per_minute_limit_rotates_and_comes_back():
    llm, clock, logs = rot({"a": [ok("a1"), limit("Minute", "30s"), ok("a2")], "b": [ok("b1")], "c": []})
    assert ask(llm).text == "a1" and llm.name == "a"
    t = ask(llm)                                       # a is rate-limited -> b answers
    assert t.text == "b1" and llm.name == "b" and t.usage["model"] == "b" and not t.error
    clock.t += 31                                       # a's cool-down is over, but b is sticky while it works
    llm.current = llm.slots[1]
    assert [s["model"] for s in llm.status()] == ["a", "b", "c"] and llm.status()[0]["free_in_s"] == 0
    assert any("per-minute limit" in m for m in logs) and any("switching a -> b" in m for m in logs)


def test_daily_limit_parks_the_model():
    llm, clock, _ = rot({"a": [limit("Day", "60s")], "b": [ok("b1"), ok("b2")], "c": []})
    assert ask(llm).text == "b1"
    clock.t += 3600                                     # an hour later a is still parked (daily quota)
    assert ask(llm).text == "b2"
    assert llm.status()[0]["reason"] == "daily limit used up" and llm.status()[0]["free_in_s"] > 20 * 3600


def test_waits_when_every_model_is_busy_briefly_and_gives_up_when_long():
    llm, clock, logs = rot({"a": [limit("Minute", "20s"), ok("a-after-wait")], "b": [limit("Minute", "40s")]}, pool="a,b")
    t = ask(llm)
    assert t.text == "a-after-wait" and clock.slept == [20.0] and any("waiting" in m for m in logs)
    llm2, clock2, _ = rot({"a": [limit("Day")], "b": [limit("Day")]}, pool="a,b")
    t2 = ask(llm2)
    assert t2.error and "rate-limited (HTTP 429)" in t2.error and clock2.slept == []


def test_overload_rotates_and_fatal_errors_stop():
    llm, _, _ = rot({"a": [overloaded()], "b": [ok("b1")], "c": []})
    assert ask(llm).text == "b1"
    llm2, _, _ = rot({"a": [errors.ClientError(400, {"error": {"code": 400, "message": "invalid argument"}})], "b": [ok()]})
    t = ask(llm2)
    assert t.error and "HTTP 400" in t.error and "on a" in t.error


def test_pacing_switches_before_the_limit():
    llm, clock, _ = rot({"a": [ok("a1"), ok("a2")], "b": [ok("b1")]}, pool="a:2,b:5")
    assert [ask(llm).text for _ in range(3)] == ["a1", "a2", "b1"]     # a used its 2 requests this minute -> b


def test_history_from_another_model_is_replayed_safely():
    # a starts a tool-calling turn (with its own thought signature), then hits the limit; b continues the conversation
    llm, _, _ = rot({"a": [ok(signature=b"sig-from-a"), limit("Minute")], "b": [ok("final answer")]}, pool="a,b")
    first = llm.generate("system", [Message(role="user", text="q")], [])
    history = [Message(role="user", text="q"),
               Message(role="assistant", tool_calls=first.tool_calls, raw=first.raw),
               Message(role="tool", text="[[1]]", tool_call_id=None, name="run_sql")]
    second = llm.generate("system", history, [])
    assert second.text == "final answer" and llm.name == "b"
    sent = llm.slots[0].llm._client.models.requests[-1]
    assert sent["model"] == "b"
    model_turn = sent["contents"][1]
    fc_parts = [p for p in model_turn.parts if p.function_call]
    assert fc_parts and fc_parts[0].thought_signature == SKIP_SIGNATURE         # a's signature is not sent to b
    # the same history replayed to the model that wrote it keeps the original signature
    assert llm._portable(history, "a")[1].raw is first.raw


def test_get_llm_uses_the_rotation_when_a_pool_is_set(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("C360_L2_MODELS", "gemini-x, gemini-y:10")
    llm = get_llm()
    assert isinstance(llm, RotatingGeminiLLM) and llm.label == "rotation: gemini-x, gemini-y" and llm.name == "gemini-x"
    monkeypatch.delenv("C360_L2_MODELS")
    monkeypatch.setenv("C360_L2_MODEL", "gemini-z")
    assert not isinstance(get_llm(), RotatingGeminiLLM)
