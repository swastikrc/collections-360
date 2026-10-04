import copy

import pytest

from c360.layer3 import config


def needs_release():
    return pytest.mark.skipif(not (config.maple_data() / "data" / "hardship_programs.csv").exists(),
                              reason="Maple release not extracted (set MAPLE_DATA)")


def needs_store():
    return pytest.mark.skipif(not config.l3_db().exists(), reason="Layer 3 store not built (python -m c360.layer3.data.build_local)")


BASE_DOC = {
    "_id": "C360-TST-0001",
    "profile": {"first_name": "Test", "last_name": "Person"},
    "contact": {"time_zone": "America/Toronto", "preferred_language": "EN", "permitted_call_window_local": ["07:00", "21:00"],
                "preferred_contact_window": ["09:00", "20:00"], "sunday_contact_permitted": False},
    "servicing_constraints": {"consent_call": True, "consent_sms": True, "consent_email": True},
    "accounts": {"cards": [{"account_id": "C-1", "minimum_payment_due": 200.0, "months_on_book": 36, "dpd": 40,
                            "dpd_bucket": "31-60", "past_due_amount": 300.0}], "loans": [], "deposits": []},
    "collections": {"cases": [{"case_id": "CS-2026-1", "primary_account_id": "C-1", "primary_product": "card",
                               "current_dpd": 40, "current_bucket": "31-60", "total_overdue": 300.0, "total_exposure": 3000.0,
                               "queue": "mid_stage", "outcome": None, "offers": [],
                               "promises": {"total": 0, "kept": 0, "broken": 0, "open": 0, "latest": None}}],
                    "contacts": {"outbound_calls_7d": 0, "most_common_rpc_hour": 18}},
    "hardship_indicators": {"financial_hardship_flag": False, "reasons": []},
    "model_scores": [],
}


@pytest.fixture
def doc():
    return copy.deepcopy(BASE_DOC)


def case_of(d):
    return d["collections"]["cases"][0]
