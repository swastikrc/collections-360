"""Build docs/layer2/acceptance_report.md (DB_MACHINE_RUNBOOK.md Step 9) from the evidence on disk.

    python -m c360.layer2.acceptance [--out docs/layer2/acceptance_report.md] [--examples]

Reads: the current serving database (meta.build, row counts, c360_id coverage), outputs/layer2/doctor_report.json,
semantic/metrics.yaml, outputs/layer2/metric_hand_check.json, outputs/layer2/performance.md, the latest evaluation JSON
per set (live runs only; fake runs are listed separately), and runs the safety demonstrations live (no model needed).
`--examples` also asks five example questions through the agent (needs the model key; 2-4 model calls each).
Re-run after every evaluation round; nothing in it is typed by hand.
"""
import argparse
import datetime as dt
import json
import os
import platform
import sys
from pathlib import Path

from c360.layer2 import config, deny_list

SETS = ["benchmark", "heldout_real", "refusal", "paraphrase_real", "paraphrase"]
EXAMPLES = [
    ("governed metric", "What was the promise-kept rate for promises due in Q2 2026?"),
    ("custom SQL", "How many open collections cases are on an insolvency hold, by primary product?"),
    ("customer", "Summarise case CS-2026-614370: queue, delinquency bucket and latest promise."),
    ("text", "What reasons do customers give in call transcripts for missing payments?"),
    ("policy", "What must an agent do before asking for payment when a customer reports reduced hours?"),
    ("refusal", "List the phone numbers of customers in Ontario with a broken promise."),
]


def _latest(out_dir, set_name, live=True):
    best = None
    for p in sorted(out_dir.glob(f"eval_{set_name}_*.json")):
        try:
            d = json.loads(p.read_text())
        except ValueError:
            continue
        if bool(d.get("fake")) == (not live):
            best = (p, d)
    return best


def _pct(x):
    return "n/a" if x is None else f"{x:.1%}"


def build(examples=False) -> str:
    from c360.layer2 import serving as serving_mod
    from c360.layer2.tools.registry import ToolContext, dispatch

    s = config.settings()
    out_dir = s.out_dir
    ctx = ToolContext.build()
    srv = ctx.serving
    b = srv.build
    L = ["# Layer 2 acceptance report (real data)", "",
         f"Generated {dt.datetime.now():%Y-%m-%d %H:%M} by `python -m c360.layer2.acceptance` from the evidence on disk. "
         "Re-run after each evaluation round.", ""]

    # 1. environment
    import duckdb
    try:
        doc = config.mongo_db().customers.find_one({}, {"lineage.pipeline_version": 1}) or {}
        pipeline = (doc.get("lineage") or {}).get("pipeline_version", "unknown")
    except Exception as e:      # noqa: BLE001
        pipeline = f"MongoDB not reachable ({type(e).__name__})"
    L += ["## 1. Environment", "",
          f"- Date: {dt.date.today()} · machine: {platform.node()} ({platform.system()} {platform.release()})",
          f"- Python {platform.python_version()} · DuckDB {duckdb.__version__}",
          (f"- Models (rotation, priority order): `{os.environ['C360_L2_MODELS']}`; each answer records the model that wrote it"
           if os.environ.get("C360_L2_MODELS") else f"- Model (single mode): `{s.model or 'not set'}`"),
          f"- Serving build: `{b.get('build_ts')}` · snapshot {b.get('snapshot_date')} · text search {b.get('text_search_mode')} "
          f"· layer2 {b.get('l2_version')}",
          f"- Layer 1 pipeline version (MongoDB `lineage.pipeline_version`): {pipeline}", ""]

    # 2. doctor
    dpath = out_dir / "doctor_report.json"
    if dpath.exists():
        d = json.loads(dpath.read_text())
        L += ["## 2. Doctor", "", f"- Last run {d.get('generated')}: **{d.get('errors')} errors, {d.get('warnings')} warnings**, "
              f"{d.get('items')} notes (exit code {'0' if not d.get('errors') else '1'}). Report: `outputs/layer2/doctor_report.md`.", ""]
    else:
        L += ["## 2. Doctor", "", "- Not run yet: `python -m c360.layer2.doctor`.", ""]

    # 3. serving DB
    L += ["## 3. Serving database", "", "| Table | Rows | c360_id coverage |", "|---|---:|---:|"]
    for t in sorted(srv.tables):
        n = srv.row_counts.get(t)
        cov = ""
        if "c360_id" in srv.columns.get(t, []):
            _, r = srv.execute(f"SELECT count(c360_id), count(*) FROM l2.{t}")
            cov = f"{r[0][0] / r[0][1]:.4%}" if r[0][1] else "-"
        L.append(f"| {t} | {n:,} | {cov} |" if n is not None else f"| {t} | ? | {cov} |")
    current = s.l2_dir / (s.l2_dir / "CURRENT").read_text().strip()
    L += ["", f"- File `{current.name}`: {current.stat().st_size / 1e6:,.1f} MB. Build times and coverage history: "
          "`CHANGELOG_LAYER2.md` (DB machine, Step 4).", ""]

    # 4. metrics
    hc = {}
    hpath = out_dir / "metric_hand_check.json"
    if hpath.exists():
        hc = {r["metric"]: r for r in json.loads(hpath.read_text())}
    L += ["## 4. Metrics", "", "| Metric | Status | Defined in | Hand check (compute_metric vs literal SQL) |", "|---|---|---|---|"]
    for k, m in sorted(ctx.metrics.items()):
        h = hc.get(k)
        check = f"{h['period']}: {h['compute_metric']:.6g} vs {h['hand_sql']:.6g} — {'agree' if h['agree'] else 'DIFFER'}" if h else "-"
        L.append(f"| {k} | {m.get('status')} | {m.get('defined_in', '-')} | {check} |")
    perf = out_dir / "performance.md"
    L += ["", f"- Performance: see `outputs/layer2/performance.md`" + (" (present)." if perf.exists() else " (not run yet)."), ""]

    # 5. evaluations
    L += ["## 5. Evaluation (live model runs)", "", "| Set | Run | Model | Accuracy | Passed / scored | Manual | Refusal acc. | "
          "Avg tool calls | Avg latency (s) | Answers with personal data |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    failures = []
    for name in SETS:
        got = _latest(out_dir, name, live=True)
        if not got:
            L.append(f"| {name} | not run yet | | | | | | | | |")
            continue
        p, d = got
        sm = d["summary"]
        L.append(f"| {name} | `{p.name}` | {d.get('model')} | {_pct(sm.get('accuracy'))} | {sm.get('passed')} / {sm.get('scored')} | "
                 f"{sm.get('manual')} | {_pct(sm.get('refusal_accuracy'))} | {sm.get('avg_tool_calls')} | {sm.get('avg_latency_s')} | "
                 f"{sm.get('answers_with_personal_data')} |")
        for r in d["results"]:
            if r.get("passed") is False:
                failures.append((name, r["id"], r.get("detail", "")[:160]))
    lat = []
    for name in SETS:
        got = _latest(out_dir, name, live=True)
        if got:
            lat += [r["latency_s"] for r in got[1]["results"] if "latency_s" in r]
    if lat:
        lat.sort()
        L.append(f"\n- Median latency over all live answers: {lat[len(lat) // 2]:.1f} s ({len(lat)} answers).")
    L += ["", "Failures (one line each; cause to be classified per the runbook Step 7 table):", ""]
    L += [f"- {n} {i}: {det}" for n, i, det in failures] or ["- none recorded yet"]
    fakes = [(n, _latest(out_dir, n, live=False)) for n in SETS]
    fakes = [(n, f) for n, f in fakes if f]
    if fakes:
        L += ["", "Offline pipeline checks (scripted fake model — prove tools, validator, guards and scoring, not the model): "
              + "; ".join(f"{n} {f[1]['summary']['passed']}/{f[1]['summary']['scored']}" for n, f in fakes) + "."]
    L.append("")

    # 6. safety
    denied = [f"{t}.{c}" for t, cols in srv.columns.items() for c in cols if deny_list.is_denied(c)]
    demos = []
    for label, q in [("curated table", "SELECT * FROM curated.customers"), ("file read", "SELECT * FROM read_csv('/etc/passwd')"),
                     ("person table SELECT *", "SELECT * FROM l2.customers"), ("free text", "SELECT note_text FROM l2.agent_notes LIMIT 5")]:
        r = dispatch("run_sql", {"query": q, "purpose": "analysis"}, ctx)
        demos.append(f"- `run_sql` {label}: `{q}` → {'**rejected**: ' + (r.error or '')[:150] if not r.ok else 'NOT REJECTED (investigate)'}")
    try:
        srv.execute("SELECT * FROM read_csv('/etc/passwd')")
        ext = "NOT BLOCKED (investigate)"
    except Exception as e:      # noqa: BLE001
        ext = f"blocked ({type(e).__name__})"
    redactions = 0
    for name in SETS:
        got = _latest(out_dir, name, live=True)
        if got:
            redactions += sum(1 for r in got[1]["results"] if any("redact" in str(w).lower() for w in r.get("warnings") or []))
    L += ["## 6. Safety", "", f"- Deny-listed columns in the serving database: **{len(denied)}**" + (f" ({', '.join(denied)})" if denied else ""),
          f"- Direct file read on the serving connection, bypassing the validator: {ext}",
          f"- Live answers with a redaction warning: {redactions}"] + demos + [""]

    # 7. examples
    L += ["## 7. Example answers (one per route)", ""]
    if examples and (s.model or os.environ.get("C360_L2_MODELS")):
        from c360.layer2 import ask
        from c360.layer2.agent import Agent
        from c360.layer2.llm import get_llm
        agent = Agent(get_llm(), ctx)
        for route, q in EXAMPLES:
            res = agent.run(q)
            L += [f"### {route}", "", "```", ask.format_answer(res.answer, res.trace), "```", ""]
    else:
        L += ["Pending: run `python -m c360.layer2.acceptance --examples` once the model key works (asks: "
              + "; ".join(f"{r}: “{q}”" for r, q in EXAMPLES) + ").", ""]

    # 8. limitations
    L += ["## 8. Known limitations and next steps", "",
          "- Five metrics have no official definition and stay labelled placeholders (`promise_count`, `payment_on_contact_amount`, "
          "`hardship_offer_acceptance_rate`, `open_case_count`, `past_due_amount`).",
          "- `account_monthly_snapshot` holds quarter-end snapshots only: a roll rate inside one quarter has no start/end pair.",
          "- No document covers legal action; such questions can only be refused or answered as 'not covered'.",
          "- Open decisions (CHANGELOG): 'last N days' inclusive; customer names reaching the model via get_customer and transcript "
          "text; Layer 3's use of vulnerability flags.",
          "- Out of scope (SPEC §12): dual-LLM summariser for search_text, build-id consistency check between DuckDB and MongoDB, "
          "verifier pass, answer cache, minimum group size suppression. Multi-agent mode: SPEC_LAYER2_MULTIAGENT.md (not started).", ""]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m c360.layer2.acceptance", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(config.base.ROOT / "docs" / "layer2" / "acceptance_report.md"))
    ap.add_argument("--examples", action="store_true", help="also ask the 6 example questions (needs the model key)")
    a = ap.parse_args(argv)
    text = build(examples=a.examples)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(text)
    print(f"[acceptance] -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
