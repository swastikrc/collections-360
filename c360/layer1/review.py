"""Human review of identity links (the override point for Layer 1).

    python -m c360.layer1.review list [--system cards] [--limit 20]
    python -m c360.layer1.review show cards CX0606700
    python -m c360.layer1.review accept cards CX0606700 --reviewer jdoe --note "same DOB and surname; moved"
    python -m c360.layer1.review reject cards CX0606700 --reviewer jdoe
    python -m c360.layer1.review relink cards CX0606700 C360-ABC-1234 --reviewer jdoe

The queue and the decisions live in MongoDB (collections360.match_review_queue /
match_review_decisions). Decisions are never rebuilt and take precedence over the model on the next
`python run_layer1.py --from linkage`.
"""
import argparse
import datetime as dt
from bson import json_util

from c360 import mongo


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--system")
    ls.add_argument("--limit", type=int, default=20)
    sh = sub.add_parser("show")
    sh.add_argument("system")
    sh.add_argument("src_ref")
    for name in ("accept", "reject", "relink"):
        p = sub.add_parser(name)
        p.add_argument("system")
        p.add_argument("src_ref")
        if name == "relink":
            p.add_argument("c360_id")
        p.add_argument("--reviewer", required=True)
        p.add_argument("--note", default="")
    a = ap.parse_args()

    d = mongo.db()
    if a.cmd == "list":
        q = {"src_system": a.system} if a.system else {}
        for r in d.match_review_queue.find(q).sort([("src_system", 1), ("model_confidence", -1)]).limit(a.limit):
            print(" | ".join(str(r.get(k, "")) for k in
                             ["src_system", "src_ref", "review_reason", "model_confidence", "proposed_c360_id", "name_raw", "dob_raw"]))
        return
    if a.cmd == "show":
        r = d.match_review_queue.find_one({"src_system": a.system, "src_ref": a.src_ref}, {"_id": 0})
        print(json_util.dumps(r, indent=2) if r else "not in the review queue")
        return
    d.match_review_decisions.insert_one({
        "src_system": a.system, "src_ref": a.src_ref, "decision": a.cmd, "c360_id": getattr(a, "c360_id", None),
        "reviewer": a.reviewer, "decided_ts": dt.datetime.now(), "note": a.note})
    print(f"recorded: {a.cmd} {a.system}/{a.src_ref}. Apply with: python run_layer1.py --from linkage")


if __name__ == "__main__":
    main()
