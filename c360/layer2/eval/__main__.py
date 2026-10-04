"""python -m c360.layer2.eval --set synthetic|heldout|paraphrase|refusal|benchmark|all [--limit N] [--fake] [--delay S]
                             [--questions-file extra.csv] [--resume-from earlier.json ...] [--only BQ-001,BQ-002]

A benchmark run also writes the submission CSV (question_id, answer, sql_or_sources, refused); merge several runs
with `python -m c360.layer2.eval.submission --from a.json b.json`.

Any other set name is looked up as eval/questions/<name>.yaml (for example heldout_real).
"""
import argparse
import sys

from c360.layer2.eval import harness, loaders


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m c360.layer2.eval", description=__doc__)
    ap.add_argument("--set", dest="set_name", default="synthetic", help="question set, or 'all'")
    ap.add_argument("--limit", type=int, help="only the first N questions of each set")
    ap.add_argument("--fake", action="store_true", help="offline: scripted fake LLM (proves the pipeline, not the model)")
    ap.add_argument("--delay", type=float, default=0.0, help="seconds to wait between questions (rate limits)")
    ap.add_argument("--questions-file", help="run questions from a CSV (question_id, question_text, ...) as set "
                                            "'benchmark_<file stem>', e.g. the extra questions released later")
    ap.add_argument("--resume-from", nargs="+", help="earlier evaluation JSON file(s): reuse their answers, re-ask only "
                                                     "questions that are new or hit a model error")
    ap.add_argument("--only", help="comma-separated question ids to run")
    a = ap.parse_args(argv)
    reuse = {}
    for p in a.resume_from or []:
        import json
        from pathlib import Path
        for r in json.loads(Path(p).read_text())["results"]:
            reuse[r["id"]] = r
    only = [x for x in (a.only or "").split(",") if x.strip()] or None
    if a.questions_file:
        from pathlib import Path
        name = "benchmark_" + Path(a.questions_file).stem.replace(" ", "_")
        try:
            out = harness.run_set(name, limit=a.limit, fake=a.fake, delay=a.delay, reuse=reuse, only=only,
                                  questions=loaders.load_questions_csv(a.questions_file))
        except Exception as e:      # noqa: BLE001
            print(f"[eval] {name}: cannot run ({type(e).__name__}: {e})", file=sys.stderr)
            return 2
        return 1 if out["summary"]["passed"] < out["summary"]["scored"] else 0
    names = list(loaders.SETS) if a.set_name == "all" else [a.set_name]
    failed = False
    for name in names:
        try:
            out = harness.run_set(name, limit=a.limit, fake=a.fake, delay=a.delay, reuse=reuse, only=only)
        except FileNotFoundError as e:
            print(f"[eval] {name}: skipped ({e})")
            continue
        except Exception as e:      # noqa: BLE001 - configuration problems (no model, no key, no serving DB)
            print(f"[eval] {name}: cannot run ({type(e).__name__}: {e})", file=sys.stderr)
            return 2
        failed = failed or out["summary"]["passed"] < out["summary"]["scored"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
