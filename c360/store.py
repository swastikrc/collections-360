"""Collections 360 document store (MongoDB): read a customer by any key, write interactions back.

All layers use this one interface, matching the design ("all three layers read from, and write
to, the same record").

    from c360.store import C360Store
    store = C360Store()
    doc = store.get("CS-2026-614370")        # case id, account id, CRM id, card ref, borrower ref, CIF or c360_id
    store.append_interaction(doc["_id"], "call_transcript", {...}, source="layer3.agent_assist")
"""
import json
import re
import uuid
from datetime import datetime

from c360 import mongo

# key pattern -> indexed document path
_KEY_RULES = [
    (r"^C360-", "_id"),
    (r"^CS-\d{4}-\d+$", "collections.cases.case_id"),
    (r"^C-\d+$", "accounts.cards.account_id"),
    (r"^L-\d+$", "accounts.loans.account_id"),
    (r"^(CHQ|SAV|TFS|USD)-\d+$", "accounts.deposits.account_id"),
    (r"^[A-Z]{3}-\d{4}$", "identity.crm_ids"),
    (r"^CX\d+$", "identity.card_customer_refs"),
    (r"^B\d+$", "identity.borrower_refs"),
    (r"^\d{1,10}$", "identity.cifs"),
]


class C360Store:
    def __init__(self, database=None):
        self.db = database if database is not None else mongo.db()
        self.customers = self.db.customers

    def query_for(self, key):
        key = key.strip()
        for pat, path in _KEY_RULES:
            if re.match(pat, key):
                return {path: key.zfill(10) if path == "identity.cifs" else key}
        return None

    def get(self, key, projection=None):
        q = self.query_for(key)
        return self.customers.find_one(q, projection) if q else None

    def resolve(self, key):
        doc = self.get(key, {"_id": 1})
        return doc["_id"] if doc else None

    def append_interaction(self, c360_id, interaction_type, payload, source, case_id=None,
                           occurred_ts=None, written_by="system"):
        """Append-only write-back: kept in `interactions` (survives rebuilds) and pushed onto the customer."""
        item = {"interaction_id": f"INT-{uuid.uuid4().hex[:12]}", "c360_id": c360_id,
                "interaction_type": interaction_type, "source": source,
                "occurred_ts": occurred_ts or datetime.now(), "case_id": case_id, "payload": payload,
                "written_by": written_by, "written_ts": datetime.now()}
        self.db.interactions.insert_one(dict(item))
        item.pop("c360_id")
        item.pop("_id", None)
        self.customers.update_one({"_id": c360_id}, {"$push": {"interactions": {"$each": [item], "$position": 0}}})
        return item["interaction_id"]


def export_jsonl(path=None, limit=None):
    """Dump documents as JSON Lines (mongoexport-style) for sharing or inspection."""
    from bson import json_util
    from c360 import config
    out = path or (config.DOCS_DIR / "customers.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    cur = mongo.db().customers.find({}, limit=limit or 0)
    with open(out, "w") as f:
        for doc in cur:
            f.write(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS) + "\n")
    return out


if __name__ == "__main__":
    import sys
    from bson import json_util
    doc = C360Store().get(sys.argv[1] if len(sys.argv) > 1 else "CS-2026-614370")
    print(json_util.dumps(doc, indent=2, json_options=json_util.RELAXED_JSON_OPTIONS) if doc else "not found")
