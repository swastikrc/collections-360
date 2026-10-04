"""Layer 2 HTTP API (c360/api/app.py), offline: synthetic serving DB + scripted model."""
import json

import pytest
from fastapi.testclient import TestClient

from c360.api import app as api
from c360.layer2.llm.base import LLMTurn
from c360.layer2.llm.fake import ScriptedLLM, call, final_turn, tool_turn

U = {"X-C360-User": "t.agent", "X-C360-Role": "agent"}


@pytest.fixture()
def client(ctx):
    def make(turns=()):
        llm = ScriptedLLM(list(turns))
        api.reset_state(ctx=ctx, llm=llm)
        return TestClient(api.app), llm
    yield make
    api.reset_state()


def policy_turns(text="At most 3 call attempts in 7 days (POL-TEST-001 §1.1)."):
    return [tool_turn(call("search_policy", query="call attempts per week")), final_turn(text, interpretation=["contact policy"])]


def test_health_and_request_id(client):
    c, _ = client()
    r = c.get("/health")
    body = r.json()
    assert r.status_code == 200 and body["mode"] == "single" and body["capabilities"]["multi_agent"] is False
    assert body["serving"]["build_ts"] and "X-C360-Request-Id" in r.headers


def test_ask_answers_with_sources_trace_and_follow_up_context(client):
    c, llm = client(policy_turns() + [final_turn("Yes, the same limit applies on weekends.")])
    r = c.post("/ask", json={"question": "How many call attempts can we make in a week?", "include_trace": True}, headers=U)
    body = r.json()
    assert r.status_code == 200 and body["answer"]["status"] == "answered"
    assert any(s["kind"] == "policy" and "POL-TEST-001 §1.1" in s["ids"] for s in body["answer"]["sources"])
    assert body["trace"]["steps"][0]["tool"] == "search_policy" and body["turn_id"].endswith("-1")
    r2 = c.post("/ask", json={"question": "And on weekends?", "conversation_id": body["conversation_id"]}, headers=U)
    assert r2.json()["turn_id"] == body["conversation_id"] + "-2" and "trace" not in r2.json()
    sent = [m.text for m in llm.calls[-1][1]]
    assert "How many call attempts can we make in a week?" in sent          # the earlier turn was passed as context
    hist = c.get("/history?limit=5", headers=U).json()
    assert [h["question"] for h in hist] == ["And on weekends?", "How many call attempts can we make in a week?"]
    assert c.get("/history", headers={"X-C360-User": "someone.else"}).json() == []


def test_refusal_needs_no_model_call(client):
    c, llm = client([])
    a = c.post("/ask", json={"question": "List the phone numbers of customers with a broken promise."}, headers=U).json()["answer"]
    assert a["status"] == "refused" and a["refusal"]["rule"] and llm.calls == []


def test_errors_have_the_standard_shape(client):
    c, _ = client([LLMTurn(error="all models in the rotation are rate-limited (HTTP 429)")])
    r = c.post("/ask", json={"question": "What is the cure rate this year?"}, headers=U)
    assert r.status_code == 502 and r.json()["code"] == "model_unavailable" and r.json()["request_id"]
    r = c.post("/ask", json={"question": ""}, headers=U)
    assert r.status_code == 400 and r.json()["code"] == "invalid_request"
    r = c.post("/ask", json={"question": "x", "options": {"mode": "multi"}}, headers=U)
    assert r.status_code == 400


def test_per_user_limit(client):
    c, _ = client([final_turn("ok")] * 10)
    codes = [c.post("/ask", json={"question": f"Question number {i} about open cases?"}, headers=U).status_code for i in range(7)]
    assert codes[:6] == [200] * 6 and codes[6] == 429
    r = c.post("/ask", json={"question": "one more?"}, headers=U)
    assert r.json()["code"] == "rate_limited" and int(r.headers["Retry-After"]) > 0
    assert c.post("/ask", json={"question": "Another person asks?"}, headers={"X-C360-User": "other"}).status_code == 200


def test_stream_sends_started_then_answer(client):
    c, _ = client(policy_turns())
    with c.stream("POST", "/ask/stream", json={"question": "How many call attempts in a week?"}, headers=U) as r:
        text = "".join(r.iter_text())
    events = [line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("event: ")]
    assert events == ["started", "answer"]
    data = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
    assert data[1]["answer"]["status"] == "answered"


def test_metrics_examples_customers_feedback(client, ctx):
    c, _ = client()
    m = c.get("/metrics").json()
    assert any(x["key"] == "promise_kept_rate" and x["status"] for x in m)
    ex = c.get("/examples").json()
    assert len(ex) >= 10 and {e["route"] for e in ex} >= {"governed metric", "custom SQL", "customer", "text", "policy", "refusal"}
    assert c.get("/customers/hello").status_code == 400
    doc = ctx.mongo_db().customers.find_one({})
    r = c.get(f"/customers/{doc['_id']}")
    assert r.status_code == 200 and r.json()["_id"] == doc["_id"]
    assert c.post("/feedback", json={"turn_id": "x-1", "rating": "up"}, headers=U).json() == {"status": "recorded"}
    assert c.post("/feedback", json={"rating": "meh"}).status_code == 400
