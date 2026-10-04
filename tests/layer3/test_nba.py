from c360.layer3 import nba
from tests.layer3.conftest import case_of, needs_release

pytestmark = needs_release()


def test_routine_mid_stage_is_an_agent_call_at_the_best_hour(doc):
    rec = nba.recommend(doc)
    assert rec["action"]["code"] == "AGENT_CALL"
    assert rec["channel"] == "call"
    assert rec["timing"]["window_local"].startswith("18:00")
    assert rec["conflicts"] == []


def test_insolvency_stops_all_contact(doc):
    doc["servicing_constraints"]["consumer_proposal"] = True
    rec = nba.recommend(doc)
    assert rec["action"]["code"] == "HOLD_INSOLVENCY"
    assert {b["channel"] for b in rec["blocked_channels"]} == {"call", "sms", "email"}
    assert any("§7.3" in (r["cite"] or "") for r in rec["reasons"])


def test_cease_communication_means_letters_only(doc):
    doc["servicing_constraints"]["cease_communication"] = True
    rec = nba.recommend(doc)
    assert rec["action"]["code"] == "LETTERS_ONLY" and rec["channel"] == "letter"


def test_hardship_comes_before_any_payment_request(doc):
    case_of(doc)["hardship_flag"] = True
    case_of(doc)["hardship_reason"] = "job_loss"
    rec = nba.recommend(doc)
    assert rec["action"]["code"] == "OFFER_HARDSHIP"
    assert rec["program"]                                      # an eligible programme is named
    # editing it back into a payment request is a conflict, so Approve is blocked
    asked = dict(rec, action={"code": "AGENT_CALL"})
    assert any(c["rule"] == "hardship_first" for c in nba.check(asked, doc))


def test_call_cap_of_three_in_seven_days_blocks_calls(doc):
    doc["collections"]["contacts"]["outbound_calls_7d"] = 3
    rec = nba.recommend(doc)
    assert rec["channel"] != "call"
    assert any(b["channel"] == "call" and "§5.2" in b["cite"] for b in rec["blocked_channels"])


def test_editing_to_a_channel_without_consent_is_a_conflict(doc):
    doc["servicing_constraints"]["consent_sms"] = False
    rec = nba.recommend(doc)
    edited = dict(rec, channel="sms")
    assert [c["rule"] for c in nba.check(edited, doc)] == ["channel_blocked"]


def test_statutory_holiday_is_skipped_and_flagged_when_forced(doc):
    rec = nba.recommend(doc)
    assert rec["timing"]["date"] != "2026-09-30"               # National Day for Truth and Reconciliation
    forced = dict(rec, timing=dict(rec["timing"], date="2026-09-30"))
    assert any(c["rule"] == "stat_holiday" for c in nba.check(forced, doc))


def test_vulnerability_is_a_safeguard_not_a_penalty(doc):
    base = nba.recommend(doc)
    case_of(doc)["vulnerable_customer_flag"] = True
    rec = nba.recommend(doc)
    assert rec["action"]["code"] == base["action"]["code"]     # same help, same priority
    assert rec["script"] == "SCR-VUL-EN"
    assert any("§4.5" in g["cite"] for g in rec["safeguards"])


def test_national_dncl_does_not_block_collection_calls(doc):
    doc["servicing_constraints"]["national_dncl_telemarketing_only"] = True
    rec = nba.recommend(doc)
    assert "call" in rec["allowed_channels"]
    assert any("§5.3" in g["cite"] for g in rec["safeguards"])


def test_protected_attributes_cannot_change_the_recommendation(doc):
    base = nba.recommend(doc)
    doc["profile"].update(gender_code="F", marital_status="single", citizenship_status="newcomer")
    assert nba.recommend(doc)["action"] == base["action"]
