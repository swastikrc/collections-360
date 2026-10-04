"""Layer 1 — Data Product Factory. Raw -> curated -> golden Collections 360 in MongoDB, end to end.
Needs the MongoDB server running (see c360/mongo.py).

    python run_layer1.py                 # full build
    python run_layer1.py --from identity # resume from a step
    python run_layer1.py --llm-review    # also let an LLM adjudicate the review queue (needs ANTHROPIC_API_KEY)
    python run_layer1.py --export-json   # also dump every MongoDB document as JSON Lines
    python run_layer1.py --only publish_l2   # rebuild only the Layer 2 serving database
"""
import argparse
import time

STEPS = ["bronze", "curated", "identity", "linkage", "golden", "publish", "dq", "publish_l2"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", choices=STEPS, default="bronze")
    ap.add_argument("--only", choices=STEPS)
    ap.add_argument("--force-bronze", action="store_true", help="reload raw files even if already landed")
    ap.add_argument("--llm-review", action="store_true")
    ap.add_argument("--export-json", nargs="?", const=0, type=int, metavar="N",
                    help="dump MongoDB documents as JSON Lines (optionally only the first N)")
    a = ap.parse_args()
    steps = [a.only] if a.only else STEPS[STEPS.index(a.start):]
    t0 = time.time()
    for step in steps:
        print(f"[layer1] {step}", flush=True)
        if step == "bronze":
            from c360.layer1.bronze import load_bronze
            load_bronze(force=a.force_bronze)
        elif step == "curated":
            from c360.layer1.curated import build_curated
            build_curated()
        elif step == "identity":
            from c360.layer1 import identity
            identity.run()
        elif step == "linkage":
            from c360.layer1 import linkage
            linkage.run()
            if a.llm_review:
                from c360.layer1 import llm_adjudicator
                llm_adjudicator.run()
                linkage.run()  # re-apply decisions with the LLM verdicts
        elif step == "golden":
            from c360.layer1.golden import build_golden
            build_golden()
        elif step == "publish":
            from c360.layer1.load_mongo import publish
            publish()
        elif step == "dq":
            from c360.layer1 import dq
            dq.run()
        elif step == "publish_l2":
            from c360.layer2.publish import publish_l2
            publish_l2()
    if a.export_json is not None:
        from c360.store import export_jsonl
        print(f"[layer1] export -> {export_jsonl(limit=a.export_json or None)}")
    print(f"[layer1] done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
