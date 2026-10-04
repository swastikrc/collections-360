"""C360_L3_SOURCE=mongo: Layer 1's golden record from MongoDB, enriched from the local store.

Offline: a fake store stands in for MongoDB, and a fake local source for the DuckDB store.
"""
import pytest

from c360.layer3.data import source as src


class FakeStore:
    def __init__(self, doc):
        self.doc = doc

    def query_for(self, key):
        return {"any": key} if key.startswith(("CX", "C360-", "CS-")) or key.isdigit() else None

    def get(self, key):
        return self.doc if self.query_for(key) is not None else None


class FakeLocal:
    def __init__(self, docs):
        self.docs = docs

    def customer_doc(self, crm):
        if crm not in self.docs:
            raise src.NotFound(crm)
        return self.docs[crm]

    def portfolio(self):
        return {"from": "local"}


GOLDEN = {
    "_id": "C360-AAA-0001",
    "identity": {"crm_ids": ["AAA-0001", "AAA-0004"], "card_customer_refs": ["CX1"], "cifs": ["0000000001"]},
    "profile": {"first_name": "Ann", "last_name": "Lee"},
    "collections": {"cases": [{"case_id": "CS-2026-1", "current_bucket": "31-60", "link_method": "crm_id_exact"},
                              {"case_id": "CS-2026-2", "current_bucket": "1-30", "link_method": "repaired_id_collision"}],
                    "open_cases": 2},
    "accounts": {"cards": [{"account_id": "C-1", "dpd": 40}], "loans": [], "deposits": []},
}
LOCAL_1 = {
    "_id": "C360-AAA-0001",
    "identity": {"crm_ids": ["AAA-0001"], "link_source": "layer3.local"},
    "profile": {"first_name": "Ann", "last_name": "Lee", "preferred_name": "Annie"},
    "contact": {"sunday_contact_permitted": False},
    "collections": {"cases": [{"case_id": "CS-2026-1", "current_bucket": "1-30",       # stored bucket differs
                               "promises": {"items": [{"ptp_id": "P1"}]}, "assigned_agent": {"site": "Toronto"}}],
                    "recent_contacts": [{"contact_id": "K1"}]},
    "accounts": {"cards": [{"account_id": "C-1", "dpd": 39, "months_on_book": 12}], "loans": [], "deposits": []},
}
LOCAL_4 = {
    "_id": "C360-AAA-0004",
    "collections": {"cases": [{"case_id": "CS-2026-2", "promises": {"items": [{"ptp_id": "P2"}]}}],
                    "recent_contacts": [{"contact_id": "K2"}]},
}


def mongo_source(locals_=None):
    return src.MongoSource(local=FakeLocal(locals_ if locals_ is not None else {"AAA-0001": LOCAL_1, "AAA-0004": LOCAL_4}),
                           store=FakeStore(GOLDEN))


def test_golden_record_wins_and_local_detail_is_added():
    d = mongo_source().customer_doc("CS-2026-1")
    assert d["_id"] == "C360-AAA-0001"
    assert d["identity"]["crm_ids"] == ["AAA-0001", "AAA-0004"]          # Layer 1 identity, not the local one
    assert d["identity"]["link_source"] == "layer3.local"                # extra local field kept
    cases = {c["case_id"]: c for c in d["collections"]["cases"]}
    assert cases["CS-2026-1"]["current_bucket"] == "31-60"               # golden (recomputed) bucket wins
    assert cases["CS-2026-1"]["promises"]["items"] == [{"ptp_id": "P1"}]  # enrichment from the local store
    assert cases["CS-2026-1"]["assigned_agent"] == {"site": "Toronto"}
    assert cases["CS-2026-2"]["link_method"] == "repaired_id_collision"
    card = d["accounts"]["cards"][0]
    assert card["dpd"] == 40 and card["months_on_book"] == 12
    assert d["profile"]["preferred_name"] == "Annie" and d["contact"]["sunday_contact_permitted"] is False
    assert "mongo" in d["lineage"]["layer3_source"]


def test_both_crm_records_of_a_merged_person_contribute():
    d = mongo_source().customer_doc("C360-AAA-0001")
    cases = {c["case_id"]: c for c in d["collections"]["cases"]}
    assert cases["CS-2026-2"]["promises"]["items"] == [{"ptp_id": "P2"}]   # from the second CRM record
    assert {c["contact_id"] for c in d["collections"]["recent_contacts"]} == {"K1", "K2"}


def test_customer_outside_collections_still_works_without_enrichment():
    d = mongo_source(locals_={}).customer_doc("CS-2026-1")
    assert d["collections"]["cases"][0]["current_bucket"] == "31-60" and "recent_contacts" not in d["collections"]


def test_layer1_key_formats_are_accepted_and_unknown_keys_rejected():
    s = mongo_source()
    assert s.customer_doc("CX1")["_id"] == "C360-AAA-0001"              # card-system ref: Layer 1 resolves it
    assert s.customer_doc("0000000001")["_id"] == "C360-AAA-0001"       # CIF
    with pytest.raises(src.BadKey):
        s.customer_doc("NOT-A-KEY")


def test_portfolio_and_worklist_come_from_the_local_store():
    assert mongo_source().portfolio() == {"from": "local"}


def test_mongo_source_needs_the_local_store():
    with pytest.raises(FileNotFoundError):
        src.MongoSource(local=None, store=FakeStore(GOLDEN))
