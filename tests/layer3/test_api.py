"""End-to-end API checks on the local store (skipped when it hasn't been built)."""
import re

import pytest

from tests.layer3.conftest import needs_store

pytestmark = needs_store()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("C360_L3_OUT", str(tmp_path))
    from fastapi.testclient import TestClient
    from c360.layer3 import actions, api
    actions._store = None
    yield TestClient(api.app)
    actions._store = None


AGENT = {"X-C360-User": "t.agent", "X-C360-Role": "agent"}


def test_keys_and_errors(client):
    assert client.get("/api/l3/customers/hello").status_code == 400
    assert client.get("/api/l3/customers/CS-2026-000001").status_code == 404
    r = client.get("/api/l3/customers/CS-2026-614370")
    assert r.status_code == 200 and r.json()["_id"] == "C360-ZOA-3450"
    assert "X-C360-Request-Id" in r.headers


def test_approval_rules_are_enforced_server_side(client):
    case = "/api/l3/cases/CS-2026-614370/decision"
    # no SMS consent on this customer: an edit to SMS can't be approved
    r = client.post(case, json={"decision": "edit", "reason": "prefers text", "edits": {"channel": "sms"}}, headers=AGENT)
    assert r.status_code == 409 and r.json()["detail"]["conflicts"][0]["rule"] == "channel_blocked"
    assert client.post(case, json={"decision": "reject"}, headers=AGENT).status_code == 400   # reason required
    r = client.post(case, json={"decision": "approve"}, headers=AGENT)
    assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["decided_by"] == "t.agent"


def test_customer_flow_lands_in_the_employee_queue(client):
    assert client.post("/api/me/login", json={"case_id": "CS-2026-614370", "last_name": "Smith"}).status_code == 401
    tok = client.post("/api/me/login", json={"case_id": "cs-2026-614370", "last_name": "AYERS"}).json()["token"]
    me = {"Authorization": f"Bearer {tok}"}
    assert client.post("/api/me/promise", json={"amount": 50, "date": "2026-10-30"}, headers=me).status_code == 400  # > 21 days
    assert client.post("/api/me/promise", json={"amount": 50, "date": "2026-10-05"}, headers=me).status_code == 200
    r = client.post("/api/me/hardship", json={"program_id": "HP-CARD-MIN", "reason": "illness"}, headers=me)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    queue = client.get("/api/l3/approvals").json()["items"]
    assert any(i["id"] == r.json()["id"] for i in queue)
    assert client.post(f"/api/l3/approvals/{r.json()['id']}", json={"status": "rejected"}, headers=AGENT).status_code == 400
    assert client.post(f"/api/l3/approvals/{r.json()['id']}", json={"status": "approved"}, headers=AGENT).status_code == 200
    statuses = {x["type"]: x["status"] for x in client.get("/api/me", headers=me).json()["requests"]}
    assert statuses == {"hardship_request": "approved", "promise": "recorded"}


def test_customer_cannot_see_internal_fields(client):
    tok = client.post("/api/me/login", json={"case_id": "CS-2026-614370", "last_name": "Ayers"}).json()["token"]
    body = client.get("/api/me", headers={"Authorization": f"Bearer {tok}"}).text
    for internal in ("model_scores", "note_text", "assigned_agent", "hardship_propensity", "C-818030"):
        assert internal not in body


def test_post_call_and_assistant(client):
    a = client.get("/api/l3/calls/5010384").json()["analysis"]
    assert a["summary"] == "Customer reports illness; promised $95 on 14 Oct; next step: hardship plan review."
    tok = client.post("/api/me/login", json={"case_id": "CS-2026-614370", "last_name": "Ayers"}).json()["token"]
    r = client.post("/api/me/chat", json={"message": "I lost my job and can't pay"}, headers={"Authorization": f"Bearer {tok}"}).json()
    assert r["intent"] == "hardship" and "POL-COLL-004 §4.2" in r["citations"]
    # hardship first: the reply offers options and never asks for a payment (POL-COLL-004 §4.2)
    assert not re.search(r"\b(pay|make a payment) (now|today)\b", r["reply"].lower())
    assert [c["type"] for c in r["cards"]] == ["hardship_options"]
