from c360.layer3 import postcall
from tests.layer3.conftest import needs_release

pytestmark = needs_release()


def call(*turns, ts="2026-09-27 16:59:55"):
    return {"transcript_id": "T1", "case_id": "CS-2026-1", "call_start_ts": ts,
            "turns": [{"speaker": s, "text": t, "start_sec": float(i * 5), "sentiment_score": sc}
                      for i, (s, t, sc) in enumerate(turns)]}


GOOD_OPEN = [("agent", "Hi, this is Alex calling from Maple Bank. This call may be recorded for quality and training purposes.", 0.3),
             ("customer", "Speaking.", 0.1),
             ("agent", "Before we continue, can you please confirm your date of birth?", 0.2),
             ("customer", "Sure, it's [PII].", 0.1),
             ("agent", "Thank you. I'm calling about your account with Maple Bank, which is past due.", 0.3),
             ("agent", "The amount past due today is $164.77.", 0.2)]
GOOD_CLOSE = [("agent", "Thank you for your time today. Have a good evening.", 0.4)]


def test_hardship_promise_and_summary():
    a = postcall.analyse(call(*GOOD_OPEN,
                              ("customer", "I'm not able to pay that right now. I've been off work sick since Jul.", -0.6),
                              ("agent", "I'm sorry to hear that. We have hardship options, like a reduced payment for three months.", 0.3),
                              ("customer", "I can pay $95 Oct 14.", -0.5),
                              ("agent", "Just to confirm, a payment of $95.00 on October 14. I'll send you a payment link.", 0.3),
                              *GOOD_CLOSE), overdue_at_call=164.77)
    f = a["fields"]
    assert (f["hardship"], f["hardship_reason"], f["ptp_amount"], f["ptp_date"]) == ("clear", "illness", 95.0, "2026-10-14")
    assert a["summary"] == "Customer reports illness; promised $95 on 14 Oct; next step: hardship plan review."
    assert a["qa"]["score"] == 100 and not a["qa"]["coaching_required"]


def test_frequency_complaint_is_not_a_cease_request_but_please_stop_is():
    angry = postcall.analyse(call(*GOOD_OPEN, ("customer", "Stop calling me every day! This is harassment.", -0.9), *GOOD_CLOSE))
    assert not angry["fields"]["cease_request"] and angry["fields"]["sentiment"] == "hostile"
    cease = postcall.analyse(call(*GOOD_OPEN, ("customer", "Please stop calling me.", -0.6), *GOOD_CLOSE))
    assert cease["fields"]["cease_request"] and cease["fields"]["next_step"] == "stop_contact"


def test_qa_score_is_100_minus_failed_weights_and_critical_fail_needs_coaching():
    a = postcall.analyse(call(("agent", "The amount past due today is $50.00.", 0.2),            # disclosed before ID check
                              ("agent", "Before we continue, can you please confirm your date of birth?", 0.2),
                              ("customer", "Sure.", 0.1),
                              ("agent", "If you don't pay we will sue and garnish your wages.", -0.2)))
    failed = {i["code"]: i["weight"] for i in a["qa"]["items"] if i["verdict"] == "fail"}
    assert a["qa"]["score"] == 100 - sum(failed.values())
    assert "identity_verified_before_disclosure" in failed and "no_threatening_language" in failed
    assert a["qa"]["coaching_required"] and a["fields"]["legal_threat_by_agent"]


def test_french_calls():
    a = postcall.analyse(call(("agent", "Bonjour, ici Luc de la Banque Maple. Cet appel peut être enregistré à des fins de qualité.", 0.3),
                              ("customer", "C'est moi.", 0.1),
                              ("agent", "Avant de continuer, pouvez-vous confirmer votre date de naissance?", 0.2),
                              ("agent", "Merci. J'appelle au sujet de votre compte à la Banque Maple, qui est en retard.", 0.2),
                              ("customer", "Je ne peux pas tout payer. J'ai été mis à pied en juin.", -0.6),
                              ("customer", "Je vais essayer de payer 405$ le 3.", 0.2),
                              ("agent", "Pour confirmer, un paiement de 405.00 $ le 3 juil..", 0.2),
                              ("agent", "Merci pour votre temps. Bonne soirée.", 0.3), ts="2026-06-20 10:00:00"))
    f = a["fields"]
    assert (f["hardship_reason"], f["ptp_amount"], f["ptp_date"]) == ("job_loss", 405.0, "2026-07-03")
    assert {i["code"]: i["verdict"] for i in a["qa"]["items"]}["recording_notice_given"] == "pass"
