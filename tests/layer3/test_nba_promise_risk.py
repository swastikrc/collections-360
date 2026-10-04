"""Next best action + promise risk model: advice inside the rules, never instead of them."""
from c360.layer3 import nba
from c360.models import ptp_risk


def _risk(band, p, safeguards=()):
    return {"probability_not_kept": p, "band": band, "summary": f"{p:.0%} chance this promise is not kept in full ({band} risk).",
            "raises_risk": [], "lowers_risk": [], "safeguards": list(safeguards), "model": "test", "advisory": True}


def _with_open_promise(doc):
    case = doc["collections"]["cases"][0]
    case["promises"] = {"total": 1, "kept": 0, "broken": 0, "open": 1,
                        "latest": {"ptp_id": "PTP-T1", "amount": 120.0, "due_date": "2026-10-02", "status": "open", "channel": "call"}}
    return doc


def test_high_risk_turns_the_reminder_into_a_call(doc, monkeypatch):
    monkeypatch.setattr(ptp_risk, "score", lambda ptp_id: _risk("high", 0.82, ["Financial hardship was mentioned: check affordability."]))
    rec = nba.recommend(_with_open_promise(doc))
    assert rec["action"]["code"] == "PTP_REMINDER" and rec["channel"] == "call"
    texts = [r["text"] for r in rec["reasons"]]
    assert any("likely to fail" in t for t in texts) and any(t.startswith("Promise risk model (advice): 82%") for t in texts)
    assert any("hardship" in s["text"] for s in rec["safeguards"])
    assert rec["promise_risk"]["band"] == "high"


def test_low_risk_keeps_the_least_intrusive_reminder(doc, monkeypatch):
    monkeypatch.setattr(ptp_risk, "score", lambda ptp_id: _risk("low", 0.08))
    rec = nba.recommend(_with_open_promise(doc))
    assert rec["action"]["code"] == "PTP_REMINDER" and rec["channel"] == "sms"


def test_holds_ignore_the_score(doc, monkeypatch):
    monkeypatch.setattr(ptp_risk, "score", lambda ptp_id: _risk("high", 0.95))
    d = _with_open_promise(doc)
    d["servicing_constraints"]["bankruptcy"] = True
    rec = nba.recommend(d)
    assert rec["action"]["code"] == "HOLD_INSOLVENCY" and rec["promise_risk"] is None
    assert not any("risk model" in r["text"] for r in rec["reasons"])


def test_model_failure_or_switch_off_leaves_the_rules_unchanged(doc, monkeypatch):
    def boom(ptp_id):
        raise RuntimeError("serving database not reachable")
    monkeypatch.setattr(ptp_risk, "score", boom)
    rec = nba.recommend(_with_open_promise(doc))
    assert rec["action"]["code"] == "PTP_REMINDER" and rec["channel"] == "sms" and rec["promise_risk"] is None
    monkeypatch.setattr(ptp_risk, "score", lambda ptp_id: _risk("high", 0.9))
    monkeypatch.setenv("C360_L3_PROMISE_MODEL", "off")
    assert nba.recommend(_with_open_promise(doc))["promise_risk"] is None
