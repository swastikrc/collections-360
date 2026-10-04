from c360.layer3 import hardship
from c360.layer3.policy import PolicyLibrary
from tests.layer3.conftest import case_of, needs_release

pytestmark = needs_release()


def test_only_programmes_for_the_product_and_in_force(doc):
    ev = hardship.evaluate(doc, case_of(doc), stated_reason="job_loss")
    ids = {o["program_id"] for o in ev["options"]}
    assert "HP-CARD-MIN" in ids and "HP-LOAN-RED3" not in ids
    red = next(o for o in ev["options"] if o["program_id"] == "HP-CARD-MIN")
    assert red["eligible"] and red["estimate"]["amount"] == 100.0      # 50% of the $200 minimum


def test_insolvency_and_dpd_limits_exclude_with_reasons(doc):
    doc["servicing_constraints"]["bankruptcy"] = True
    ev = hardship.evaluate(doc, case_of(doc), stated_reason="illness")
    assert not any(o["eligible"] for o in ev["options"] if o["program_id"] != "HP-DMP")
    assert all(o["why_not"] for o in ev["options"] if not o["eligible"])


def test_a_plan_already_active_is_not_offered_again(doc):
    case_of(doc)["offers"] = [{"program_id": "HP-CARD-MIN", "customer_response": "accepted", "plan_status": "active",
                               "offer_ts": "2026-09-01 10:00:00"}]
    ev = hardship.evaluate(doc, case_of(doc), stated_reason="illness")
    o = next(o for o in ev["options"] if o["program_id"] == "HP-CARD-MIN")
    assert not o["eligible"] and "Already on this plan." in o["why_not"]


def test_current_policy_beats_a_conflicting_faq():
    lib = PolicyLibrary()
    hits = lib.search("How long does the 3-month reduced payment programme last, and what share of the payment is reduced?")
    assert hits[0].cite == "POL-COLL-004 §4.2"          # 3 months at 50%; the FAQ still says 6 months


def test_superseded_documents_are_never_returned():
    lib = PolicyLibrary()
    assert "POL-COLL-004-V4.1" in lib.superseded and "SCR-MID-STD-EN-V1.0" in lib.superseded
    assert not any(s.doc_id in lib.superseded for s in lib.sections)
    assert lib.search("How many call attempts can we make on a mid-stage account in a week?")[0].cite == "POL-COLL-001 §5.2"
