"""Benchmark answers CSV in the release template format: question_id, answer, sql_or_sources, refused.

The rows are exactly what the system answered (from the evaluation run's JSON); nothing here edits an
answer. Several runs can be merged (the released benchmark plus the extra questions released later);
a later run wins for a question id that appears in both.

    python -m c360.layer2.eval.submission --from outputs/layer2/eval_benchmark_<ts>.json [more.json ...] \\
        [--template <release>/labels/benchmark_answers_template.csv] [--out outputs/layer2/benchmark_answers.csv]
"""
import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

COLUMNS = ["question_id", "answer", "sql_or_sources", "refused"]


def _table_csv(table) -> str:
    if not table or not table.get("columns"):
        return ""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(table["columns"])
    for row in table.get("rows") or []:
        w.writerow(["" if v is None else v for v in row])
    return buf.getvalue().rstrip("\n")


def answer_cell(answer: dict) -> str:
    """The answer as a person would read it: the text, then any table as a small CSV."""
    if not answer:
        return ""
    if answer.get("status") == "refused":
        reason = (answer.get("refusal") or {}).get("reason") or answer.get("answer_text") or ""
        return f"Refused: {reason}".strip()
    parts = [str(answer.get("answer_text") or "").strip()]
    table = _table_csv(answer.get("table"))
    if table:
        parts.append(table)
    return "\n".join(p for p in parts if p)


def sources_cell(answer: dict) -> str:
    """The SQL that ran (governed metric or custom query), or the documents / records used."""
    if not answer:
        return ""
    out = []
    for s in answer.get("sources") or []:
        if s.get("sql"):
            out.append(" ".join(str(s["sql"]).split()))
        elif s.get("kind") == "policy":
            out.extend(s.get("ids") or [])
        elif s.get("kind") in ("notes", "transcripts", "customer"):
            label = {"notes": "agent notes", "transcripts": "transcripts", "customer": "customer record"}[s["kind"]]
            ids = s.get("ids") or []
            out.append(f"{label}: {', '.join(ids[:20])}" if ids else label)
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return "\n".join(uniq)


def row_for(result: dict) -> dict:
    a = result.get("answer") or {}
    return {"question_id": result["id"], "answer": answer_cell(a), "sql_or_sources": sources_cell(a),
            "refused": "true" if a.get("status") == "refused" else "false"}


def _qid_key(qid):
    m = re.search(r"(\d+)$", str(qid))
    return (re.sub(r"\d+$", "", str(qid)), int(m.group(1)) if m else 0, str(qid))


def merge_results(runs: list[list[dict]]) -> dict:
    """{question_id: result}; later runs win. Results without an answer (skipped) are ignored."""
    merged = {}
    for results in runs:
        for r in results:
            if r.get("answer") is not None:
                merged[r["id"]] = r
    return merged


def template_ids(path) -> list[str]:
    if not path or not Path(path).exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [r["question_id"] for r in csv.DictReader(f) if r.get("question_id")]


def write_csv(results_by_id: dict, path, template=None) -> dict:
    """Write the submission CSV. Template ids come first (in template order), then any extra ids in id order.
    Returns {"rows", "missing" (template ids with no answer), "path"}."""
    ids = template_ids(template)
    extra = sorted((q for q in results_by_id if q not in ids), key=_qid_key)
    order = ids + extra
    missing = [q for q in ids if q not in results_by_id]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for q in order:
            if q in results_by_id:
                w.writerow(row_for(results_by_id[q]))
            else:
                w.writerow({"question_id": q, "answer": "", "sql_or_sources": "", "refused": "false"})
    return {"rows": len(order), "missing": missing, "path": path}


def load_run(path) -> list[dict]:
    return json.loads(Path(path).read_text())["results"]


def main(argv=None) -> int:
    from c360.layer2 import config
    ap = argparse.ArgumentParser(prog="python -m c360.layer2.eval.submission", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="runs", nargs="+", required=True, help="evaluation JSON file(s); later files win")
    ap.add_argument("--template", default=str(config.settings().data_root / "labels" / "benchmark_answers_template.csv"))
    ap.add_argument("--out", default=str(config.settings().out_dir / "benchmark_answers.csv"))
    a = ap.parse_args(argv)
    merged = merge_results([load_run(p) for p in a.runs])
    info = write_csv(merged, a.out, a.template)
    print(f"[submission] {info['rows']} rows -> {info['path']}"
          + (f"; {len(info['missing'])} template question(s) without an answer: {', '.join(info['missing'])}" if info["missing"] else ""))
    return 1 if info["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())
