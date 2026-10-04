"""Log a conversation (c360/layer3/conversation.py + its endpoints). Spec: SPEC_L3_CONVERSATION_EXTRACT.md §9."""
import copy
import re
from pathlib import Path

import pytest

from c360.layer3 import actions, conversation as cv
from tests.layer3.conftest import needs_store

CASE = {"case_id": "CS-2026-1", "total_overdue": 412.5, "total_exposure": 3000.0}
BASE = {"channel": "call", "direction": "outbound", "started_at": "2026-09-28T10:00:00", "language": "auto"}
TRANSCRIPT = """[00:00] Agent: Hi, this is Priya calling from Maple Bank. This call may be recorded. Can you confirm your date of birth?
[00:12] Customer: Sure, it's on file. Look, I was laid off three weeks ago
and things are really tight.
[00:25] Agent: I'm sorry to hear that. The amount past due today is $412.50. We do have hardship options like a reduced payment.
[00:41] Customer: I can pay $150 on the 14th. And please don't call me at work.
[00:50] Spouse: He really can't take calls there.
[00:55] Agent: Just to confirm, a payment of $150.00 on October 14. Thank you for your time."""
NOTES = "Spoke w/ cust. Lost job last month. Promised $200 on Oct 14. Asked us to stop calling."


def draft(text, mode="transcript", doc=None, **kw):
    return cv.extract(cv.clean_input(dict(BASE, mode=mode, text=text, **kw)), doc or {"_id": "C360-TST-0001", "profile": {}}, CASE)


def test_parse_labelled_transcript():
    turns, warnings, mode = cv.parse({"mode": "transcript", "text": TRANSCRIPT})
    assert mode == "transcript" and warnings == []
    assert [t["speaker"] for t in turns] == ["agent", "customer", "agent", "customer", "third_party", "agent"]
    assert turns[1]["start_sec"] == 12 and turns[1]["text"].endswith("things are really tight.")     # continuation line appended
    d = draft(TRANSCRIPT)
    assert d["fields"]["third_party_present"] is True and d["qa"]["score"] is not None


def test_unlabelled_text_becomes_notes():
    turns, warnings, mode = cv.parse({"mode": "transcript", "text": "Called the customer. She will pay $50 on Oct 9."})
    assert mode == "notes" and "No speaker labels found; treated as notes." in warnings and len(turns) == 2


def test_notes_english_and_french():
    f = draft(NOTES, "notes")["fields"]
    assert (f["hardship"], f["hardship_reason"], f["ptp_amount"], f["ptp_date"], f["cease_request"], f["next_step"]) == \
        ("clear", "job_loss", 200.0, "2026-10-14", True, "stop_contact")
    assert f["legal_threat_by_agent"] is None                                    # not assessed from notes
    f = draft("Client a perdu son emploi. Va payer 150 $ le 10 octobre.", "notes")["fields"]
    assert (f["hardship_reason"], f["ptp_amount"], f["ptp_date"]) == ("job_loss", 150.0, "2026-10-10")
    d = draft("Promised $80 on 10/14.", "notes")
    assert d["fields"]["ptp_date"] is None and "Promise date not understood; enter it." in d["warnings"]


def test_masking_keeps_amounts_dates_and_ids():
    m = cv.mask("card 4520 1234 5678 9012, SIN 123-456-789, call 416-555-0199 or zoe@example.ca, acct 0012345")
    assert "[card ••••9012]" in m and "[SIN]" in m and "[phone]" in m and "[email]" in m and "[account]" in m
    assert not re.search(r"4520|123-456-789|555-0199|example", m)
    keep = "$1,250.00 on 2026-10-14, Oct 14 at 14:30, CS-2026-614370 and C360-ABC-1234"
    assert cv.mask(keep) == keep
    d = draft("Customer: my number is 416-555-0199 and I can pay $150 on the 14th.\nAgent: Thank you.")
    assert "[phone]" in d["turns"][0]["text"] and d["fields"]["ptp_amount"] == 150.0    # extraction ran before masking


def test_promise_checks_match_the_customer_app_rules():
    rules = lambda a, d: [c["rule"] for c in cv.validate_promise(a, d, CASE)]
    assert rules(150, "2026-10-14") == []
    assert rules(150, "2026-11-30") == ["promise_too_far"] and cv.validate_promise(150, "2026-11-30", CASE)[0]["cite"] == "PRC-COLL-010 §3.1"
    assert rules(150, "2026-09-30") == ["promise_holiday"] and rules(150, "2026-09-20") == ["promise_date_past"]
    assert rules(99999, "2026-10-14") == ["promise_over_balance"]


# ---------------------------------------------------------------- endpoints (real store)

pytestmark_store = needs_store()
AGENT = {"X-C360-User": "t.agent", "X-C360-Role": "agent"}
LEAD = {"X-C360-User": "t.lead", "X-C360-Role": "team_lead"}
KEY = "CS-2026-614370"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("C360_L3_OUT", str(tmp_path))
    from fastapi.testclient import TestClient
    from c360.layer3 import api
    actions._store = None
    yield TestClient(api.app)
    actions._store = None


def body(text=NOTES, mode="notes", **kw):
    return dict(BASE, mode=mode, text=text, **kw)


@needs_store()
def test_extract_writes_nothing_and_overlay_is_a_no_op_without_logs(client):
    before_doc = client.get(f"/api/l3/customers/{KEY}").json()
    before_nba = client.get(f"/api/l3/cases/{KEY}/nba").json()
    counts = actions.store().counts()
    d = client.post(f"/api/l3/cases/{KEY}/conversations/extract", json=body(), headers=AGENT)
    assert d.status_code == 200 and d.json()["fields"]["cease_request"] is True
    assert actions.store().counts() == counts
    assert client.get(f"/api/l3/customers/{KEY}").json() == before_doc and "_overlay" not in before_doc
    assert client.get(f"/api/l3/cases/{KEY}/nba").json() == before_nba
    assert client.post(f"/api/l3/cases/{KEY}/conversations/extract", json=body(text=""), headers=AGENT).status_code == 400


@needs_store()
def test_save_rules_corrections_mandatory_and_duplicates(client):
    d = client.post(f"/api/l3/cases/{KEY}/conversations/extract", json=body(), headers=AGENT).json()
    ids = [u["id"] for u in d["proposed_updates"]]
    cease = next(u["id"] for u in d["proposed_updates"] if u["kind"] == "stop_contact")
    save = lambda **kw: client.post(f"/api/l3/cases/{KEY}/conversations", json={**body(), "selected_updates": ids, "confirm": True, **kw}, headers=AGENT)
    assert client.post(f"/api/l3/cases/{KEY}/conversations", json=dict(body(), selected_updates=ids), headers=AGENT).status_code == 400   # no confirm
    assert save(selected_updates=[i for i in ids if i != cease]).status_code == 400                    # mandatory update unticked
    assert save(corrections={"cease_request": False}).status_code == 400                               # protective change needs a reason
    r = save(corrections={"ptp_date": "2026-11-30"})                                                   # more than 21 days out
    assert r.status_code == 409 and any(c["cite"] == "PRC-COLL-010 §3.1" for c in r.json()["detail"]["checks"])
    ok = save(corrections={"ptp_amount": 180}, fields={"cease_request": False}, agent_note="call me at 416-555-0199")
    out = ok.json()
    p = out["conversation"]["payload"]
    assert ok.status_code == 200 and p["fields_final"]["cease_request"] is True                        # client 'fields' ignored
    assert p["corrections"] == [{"field": "ptp_amount", "from": 200.0, "to": 180.0, "reason": None}] and "[phone]" in p["agent_note"]
    assert {i["type"] for i in out["created"]} >= {"cease_request", "promise", "hardship_request"}
    assert save(corrections={"ptp_amount": 180}).status_code == 409                                    # same conversation again


@needs_store()
def test_logged_stop_contact_and_hardship_take_effect_and_follow_approvals(client):
    before = client.get(f"/api/l3/cases/{KEY}/nba").json()["action"]["code"]
    d = client.post(f"/api/l3/cases/{KEY}/conversations/extract", json=body(), headers=AGENT).json()
    out = client.post(f"/api/l3/cases/{KEY}/conversations", json=dict(body(), selected_updates=[u["id"] for u in d["proposed_updates"]], confirm=True), headers=AGENT).json()
    cease = next(i for i in out["created"] if i["type"] == "cease_request")
    assert cease["status"] == "pending" and out["nba"]["action"]["code"] == "LETTERS_ONLY"             # protective: applies straight away
    doc = client.get(f"/api/l3/customers/{KEY}").json()
    assert any(e["field"] == "cease_contact_flag" for e in doc["_overlay"])
    case = next(c for c in doc["collections"]["cases"] if c["case_id"] == KEY)
    assert case["promises"]["logged"][0]["amount"] == 200.0
    r = client.post(f"/api/l3/cases/{KEY}/decision", json={"decision": "edit", "reason": "try a call", "edits": {"channel": "call"}}, headers=LEAD)
    assert r.status_code == 409
    pending = {i["type"] for i in client.get("/api/l3/approvals").json()["items"]}
    assert {"cease_request", "hardship_request"} <= pending
    assert client.post(f"/api/l3/approvals/{cease['id']}", json={"status": "approved"}, headers=AGENT).status_code == 403
    assert client.post(f"/api/l3/approvals/{cease['id']}", json={"status": "rejected", "reason": "customer withdrew it"}, headers=LEAD).status_code == 200
    for i in out["created"]:
        if i["type"] == "hardship_request":
            client.post(f"/api/l3/approvals/{i['id']}", json={"status": "rejected", "reason": "test"}, headers=LEAD)
    assert client.get(f"/api/l3/cases/{KEY}/nba").json()["action"]["code"] == before                    # rejected: effect removed
    lst = client.get(f"/api/l3/cases/{KEY}/conversations").json()
    cid = lst["items"][0]["payload"]["conversation_id"]
    assert "turns_masked" not in lst["items"][0]["payload"] and lst["followups"][cid]
    assert client.get(f"/api/l3/conversations/{cid}").json()["conversation"]["payload"]["turns_masked"]
    assert client.get("/api/l3/conversations/CNV-nope").status_code == 404


def test_overlay_without_logs_returns_the_same_document(tmp_path):
    store = actions.ActionStore(tmp_path / "a.sqlite")
    doc = {"_id": "C360-X", "collections": {"cases": [dict(CASE)]}}
    assert cv.overlay(doc, store) is doc


def test_mongo_mirror_leaves_out_the_conversation_text(tmp_path, monkeypatch):
    sent = []
    import c360.store
    monkeypatch.setenv("C360_L3_WRITEBACK", "mongo")
    monkeypatch.setattr(c360.store.C360Store, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(c360.store.C360Store, "append_interaction", lambda self, cid, type_, payload, **k: sent.append((type_, payload)))
    store = actions.ActionStore(tmp_path / "a.sqlite")
    doc = {"_id": "C360-X", "profile": {}, "collections": {"cases": [dict(CASE)]}}
    d = draft(NOTES, "notes", doc=doc)
    cv.save(dict(BASE, mode="notes", text=NOTES, selected_updates=[u["id"] for u in d["proposed_updates"]], confirm=True),
            doc, CASE, {"id": "t.agent", "role": "agent"}, store)
    log = next(p for t, p in sent if t == "conversation_log")
    assert "text_masked" not in log and "turns_masked" not in log and log["summary"]


def test_terminal_has_no_html_injection_dialogs_or_stored_conversation_text():
    js = (Path(cv.__file__).parent / "web" / "terminal" / "terminal.js").read_text()
    for banned in (".innerHTML", "insertAdjacentHTML(", "alert(", "confirm(", "prompt("):
        assert banned not in js, banned
    assert not re.search(r"(localStorage|sessionStorage)\.setItem\([^)]*(conv|draft|transcript)", js, re.I)
