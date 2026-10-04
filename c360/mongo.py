"""MongoDB: the Collections 360 NoSQL store.

Database `collections360`:
  customers                one JSON document per person, _id = c360_id
  interactions             append-only write-back (transcripts, agent-assist notes); also $pushed onto the customer
  match_review_queue       identity links awaiting a person
  match_review_decisions   reviewer decisions (persist across rebuilds; read back by the pipeline)
  match_llm_verdicts       optional LLM adjudication results (read back by the pipeline)
  dq_results               data quality check results per build
  restricted_protected_attributes   fairness testing only; never joined into customers

Start the server:  podman run -d --name c360-mongo -p 127.0.0.1:27017:27017 \
                     -v $PWD/warehouse/mongo:/data/db:Z docker.io/library/mongo:7
"""
import os

MONGO_URI = os.environ.get("C360_MONGO_URI", "mongodb://127.0.0.1:27017")
MONGO_DB = os.environ.get("C360_MONGO_DB", "collections360")

# Every key from every source system resolves to a customer through one of these indexed paths.
KEY_PATHS = {
    "c360_id": "_id",
    "crm": "identity.crm_ids",
    "cards": "identity.card_customer_refs",
    "loans": "identity.borrower_refs",
    "core": "identity.cifs",
    "card_account": "accounts.cards.account_id",
    "loan_account": "accounts.loans.account_id",
    "deposit_account": "accounts.deposits.account_id",
    "case": "collections.cases.case_id",
}

SECONDARY_INDEXES = [
    "contact.province_code", "exposure.worst_bucket", "exposure.max_dpd",
    "hardship_indicators.financial_hardship_flag", "collections.open_cases",
    "collections.cases.is_open", "collections.cases.queue", "identity.links_pending_review",
]

# Document validator: the data contract's required shape, enforced by the server.
CUSTOMER_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["_id", "master_crm_id", "identity", "profile", "contact", "servicing_constraints",
                     "accounts", "exposure", "collections", "hardship_indicators", "lineage"],
        "properties": {
            "_id": {"bsonType": "string", "pattern": "^C360-"},
            "identity": {"bsonType": "object", "required": ["crm_ids", "min_link_confidence"]},
            "hardship_indicators": {"bsonType": "object", "required": ["financial_hardship_flag", "reasons"]},
        },
    }
}


def db(uri=None, name=None):
    from pymongo import MongoClient
    return MongoClient(uri or MONGO_URI, tz_aware=False)[name or MONGO_DB]


def ensure_customer_indexes(coll):
    from pymongo import ASCENDING
    for path in list(KEY_PATHS.values())[1:] + SECONDARY_INDEXES:
        coll.create_index([(path, ASCENDING)])


def ensure_indexes(d):
    from pymongo import ASCENDING
    d.interactions.create_index([("c360_id", ASCENDING), ("occurred_ts", ASCENDING)])
    d.match_review_queue.create_index([("src_system", ASCENDING), ("src_ref", ASCENDING)])
    d.match_review_decisions.create_index([("src_system", ASCENDING), ("src_ref", ASCENDING), ("decided_ts", ASCENDING)])
    d.match_llm_verdicts.create_index([("src_system", ASCENDING), ("src_ref", ASCENDING)])
    d.restricted_protected_attributes.create_index([("crm_customer_id", ASCENDING)])
