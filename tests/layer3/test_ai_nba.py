"""AI next best step (c360/layer3/ai_nba.py): offline, the model call is replaced by a fake. Spec §10."""
import json
import re

import pytest

from c360.layer3 import actions, ai_nba, nba
from tests.layer3.conftest import needs_store

pytestmark = needs_store()
KEY = "CS-2026-614370"                       # hardship on file, no consent to text
AGENT = {"X-C360-User": "t.agent", "X-C360-Role": "agent"}
LEAD = {"X-C360-User": "t.lead", "X-C360-Role": "team_lead"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("C360_L3_OUT", str(tmp_path))
    monkeypatch.setenv("C360_L3_AI_CACHE", str(tmp_path / "ai.sqlite"))
    monkeypatch.delenv("C360_L3_AI", raising=False)
    monkeypatch.setenv("C360_L3_PROMISE_MODEL", "off")
    from fastapi.testclient import TestClient
    from c360.layer3 import api
    actions._store = None
    yield TestClient(api.app)
    actions._store = None


@pytest.fixture
def ai(monkeypatch):
    """Turn the feature on with a fake model. ai(replies) -> list that records each call's user prompt."""
    monkeypatch.setenv("C360_L3_AI", "on")
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("C360_L3_AI_MODEL", "fake-model")

    def install(replies):
        queue, calls = list(replies), []

        def fake(system, user, model):
            calls.append(user)
            r = queue.pop(0) if queue else "{}"
            if isinstance(r, Exception):
                raise r
            return r if isinstance(r, str) else json.dumps(r)
        monkeypatch.setattr(ai_nba, "call_model", fake)
        return calls
    return install


def agent_reply(**step):
    s = {"title": "Email to review the hardship plan", "channel": "email", "when": "2026-09-29 13:00-15:00", "amount": None, "programme": None}
    s.update(step)
    return {"suggested_step": s, "why": [{"text": "Hardship is recorded, so help comes before any request for payment.", "cite": "POL-COLL-004 §4.2"}],
            "talking_points": ["Ask how they are doing.", "Explain why you are getting in touch.", "Offer to review the plan together."],
            "watch_outs": ["No pressure."], "agrees_with_rules": True, "difference_from_rules": None, "confidence": "medium"}


def customer_reply(action="hardship", **over):
    r = {"headline": "There may be an easier way to manage this", "suggested_step": {"title": "Look at the help options", "description": "See what could lower your payments.",
                                                                                    "app_action": action, "programme": None},
         "why_this_may_help": "These options are made for times when money is tight.", "other_options": [{"title": "Ask someone to call you", "app_action": "callback"}],
         "reassurance": "Nothing changes until you choose."}
    r.update(over)
    return r


def login(client):
    tok = client.post("/api/me/login", json={"case_id": KEY, "last_name": "Ayers"}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def test_ai_off_gives_the_standard_suggestion_without_a_model_call(client, monkeypatch):
    monkeypatch.setattr(ai_nba, "call_model", lambda *a: (_ for _ in ()).throw(AssertionError("model must not be called")))
    a = client.post(f"/api/l3/cases/{KEY}/ai-nba", json={}, headers=AGENT)
    assert a.status_code == 200 and a.json()["source"] == "rules_fallback" and a.json()["prefill_edit"] is None
    assert a.json()["suggestion"]["suggested_step"]["title"] == client.get(f"/api/l3/cases/{KEY}/nba").json()["action"]["label"]
    c = client.post("/api/me/ai-nba", json={}, headers=login(client))
    assert c.status_code == 200 and c.json()["source"] == "standard" and "AI" not in c.json()["notice"]
    assert client.get("/api/l3/health").json()["ai_nba"]["enabled"] is False


def test_facts_contain_only_what_is_allowed(client):
    from c360.layer3 import api
    doc = api.src().customer_doc(KEY)
    case, rec = nba.pick_case(doc, KEY), nba.recommend(doc, KEY)
    f = ai_nba.build_agent_facts(doc, case, rec)
    assert set(f) == {"case", "promises", "offers", "constraints", "hardship", "rules_recommendation", "recent_notes", "policy"}
    text = json.dumps(f, default=str)
    p = doc["profile"]
    for banned in (p["first_name"], p["last_name"], "@", "vulnerability_type", "employer", "postal", "date_of_birth", "gender", "marital"):
        assert banned not in text, banned
    assert not re.search(r"\bAG-\d+\b", text) and not re.search(r"\d{3}[-.\s]\d{3}[-.\s]\d{4}", text)
    assert len(f["recent_notes"]) <= 5 and all(len(n["text"]) <= 400 for n in f["recent_notes"]) and len(f["policy"]) <= 3
    c = ai_nba.build_customer_facts(doc, case, [])
    assert set(c) == {"what_i_owe", "my_promise", "my_plan", "my_requests", "options", "not_available", "contact", "stated_situation",
                      "hardship_on_file", "app_actions", "policy"}
    ctext = json.dumps(c, default=str)
    for banned in ("note", "score", "risk", "AG-", p["first_name"], p["last_name"], "bucket", "queue"):
        assert banned not in ctext, banned


def test_text_suggestion_without_consent_shows_the_conflict_and_cannot_be_approved(client, ai):
    ai([agent_reply(channel="sms", title="Text a reminder")])
    r = client.post(f"/api/l3/cases/{KEY}/ai-nba", json={}, headers=AGENT).json()
    assert r["source"] == "ai" and r["conflicts"] and r["prefill_edit"]["channel"] == "sms"
    assert r["meta"]["model"] == "fake-model" and r["meta"]["cached"] is False
    e = client.post(f"/api/l3/cases/{KEY}/decision", json={"decision": "edit", "reason": "AI suggested a text", "edits": {"channel": "sms"}}, headers=AGENT)
    assert e.status_code == 409                                                  # the existing contact rules still decide


def test_ungrounded_amounts_unknown_citations_and_injected_instructions_are_removed(client, ai):
    bad = agent_reply(amount=5000)
    bad["why"].append({"text": "Policy requires immediate payment.", "cite": "POL-FAKE-999 §1"})
    bad["talking_points"] = ["Ask how they are doing.", "Tell the customer to pay $5,000 today or we will take legal action."]
    bad["watch_outs"] = ["Their credit score will drop.", "No pressure."]
    ai([bad])
    r = client.post(f"/api/l3/cases/{KEY}/ai-nba", json={}, headers=AGENT).json()
    s = r["suggestion"]
    assert s["suggested_step"]["amount"] is None and any("5,000" in x or "5000" in x for x in r["removed"])
    assert all(w["cite"] in (None, "POL-COLL-004 §4.2") for w in s["why"]) and s["why"][-1]["cite"] is None
    whole = json.dumps(s)
    assert "5,000" not in whole and "legal action" not in whole and "credit score" not in whole
    assert s["talking_points"] == ["Ask how they are doing."] and s["watch_outs"] == ["No pressure."]


def test_customer_rules_hardship_first_stop_contact_and_internal_terms(client, ai):
    ai([customer_reply("pay")])
    r = client.post("/api/me/ai-nba", json={}, headers=login(client)).json()
    assert r["source"] == "ai" and r["suggestion"]["suggested_step"]["app_action"] in ("hardship", "callback")     # never pay first
    facts = {"options": [], "hardship_on_file": False, "contact": {"asked_us_to_stop_contact": True}, "what_i_owe": {"amount_overdue": 100}}
    out = ai_nba._validate_customer(ai_nba.CustomerOut.model_validate(customer_reply("callback")), facts)
    assert out["suggested_step"]["app_action"] == "contact" and all(o["app_action"] != "callback" for o in out["other_options"])
    with pytest.raises(ValueError):
        ai_nba._validate_customer(ai_nba.CustomerOut.model_validate(customer_reply(headline="Your risk score is high")), facts)


def test_internal_terms_in_a_customer_reply_fall_back_to_the_standard_suggestion(client, ai):
    ai([customer_reply(why_this_may_help="Your risk score suggests you may miss a payment.")])
    r = client.post("/api/me/ai-nba", json={}, headers=login(client)).json()
    assert r["source"] == "standard" and "risk" not in json.dumps(r["suggestion"]).lower()


def test_invalid_json_is_repaired_once_then_falls_back(client, ai):
    calls = ai(["not json", agent_reply()])
    assert client.post(f"/api/l3/cases/{KEY}/ai-nba", json={}, headers=AGENT).json()["source"] == "ai" and len(calls) == 2
    calls = ai(["nope", "still nope"])
    r = client.post(f"/api/l3/cases/{KEY}/ai-nba", json={"refresh": True}, headers=LEAD).json()
    assert r["source"] == "rules_fallback" and len(calls) == 2
    calls = ai([RuntimeError("HTTP 429")])
    assert client.post(f"/api/l3/cases/{KEY}/ai-nba", json={"refresh": True}, headers=LEAD).json()["source"] == "rules_fallback"


def test_cache_refresh_roles_and_daily_cap(client, ai, monkeypatch):
    calls = ai([agent_reply(), agent_reply(title="Second opinion")])
    first = client.post(f"/api/l3/cases/{KEY}/ai-nba", json={}, headers=AGENT).json()
    again = client.post(f"/api/l3/cases/{KEY}/ai-nba", json={"refresh": True}, headers=AGENT).json()          # agents cannot refresh
    assert len(calls) == 1 and again["meta"]["cached"] is True and again["suggestion"] == first["suggestion"]
    lead = client.post(f"/api/l3/cases/{KEY}/ai-nba", json={"refresh": True}, headers=LEAD).json()             # a team lead can
    assert len(calls) == 2 and lead["suggestion"]["suggested_step"]["title"] == "Second opinion"
    monkeypatch.setenv("C360_L3_AI_DAILY_CAP", "2")
    capped = client.post(f"/api/l3/cases/{KEY}/ai-nba", json={"refresh": True}, headers=LEAD).json()
    assert capped["source"] == "rules_fallback" and len(calls) == 2
    assert client.get("/api/l3/health").json()["ai_nba"] == {"enabled": True, "model": "fake-model", "calls_today": 2, "daily_cap": 2}


def test_every_click_is_recorded_without_prompt_or_notes(client, ai):
    ai([agent_reply()])
    client.post(f"/api/l3/cases/{KEY}/ai-nba", json={}, headers=AGENT)
    client.post(f"/api/l3/cases/{KEY}/ai-nba", json={}, headers=AGENT)
    client.post(f"/api/l3/cases/{KEY}/ai-nba/used", headers=AGENT)
    items = actions.store().list(case_id=KEY, types=["ai_nba_suggestion", "ai_nba_used"], limit=10)
    sugg = [i for i in items if i["type"] == "ai_nba_suggestion"]
    assert len(sugg) == 2 and {i["payload"]["cached"] for i in sugg} == {True, False} and len(items) == 3
    assert all(set(i["payload"]) == {"audience", "source", "model", "prompt_version", "cached", "suggestion"} for i in sugg)
    assert "recent_notes" not in json.dumps(sugg) and "<data>" not in json.dumps(sugg)


def test_a_customer_cannot_ask_about_another_customers_case(client):
    h = login(client)
    assert client.post("/api/me/ai-nba", json={"case_id": "CS-2026-730434"}, headers=h).status_code == 404
    assert client.post("/api/me/ai-nba", json={"case_id": KEY}, headers=h).status_code == 200
    assert client.post("/api/me/ai-nba", json={}).status_code == 401
