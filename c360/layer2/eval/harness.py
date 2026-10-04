"""Evaluation harness: run a question set through the agent and score it.

Report per question: pass/fail, expected vs got, tools used, governed or custom, SQL, warnings,
latency. Summary: accuracy per set and per tag, refusal accuracy, average tool calls, average
latency, trap-rule hits, and any answer that still contains personal data.
"""
import datetime as dt
import json
import time
from pathlib import Path

from c360.layer2 import config, guards
from c360.layer2.agent import Agent
from c360.layer2.eval import compare as cmp
from c360.layer2.eval import loaders
from c360.layer2.llm.fake import PlanLLM
from c360.layer2.tools.registry import ToolContext


def _got(answer) -> str:
    text = answer.answer_text
    if answer.table and answer.table.rows:
        text += f" [table: {len(answer.table.rows)} rows]"
    return text[:400]


def run_question(q, ctx, llm=None, fake=False) -> dict:
    base = {"id": q["id"], "question": q["question"], "tags": q.get("tags", []), "group": q.get("group"),
            "expected": q["expected"]}
    if q.get("skip"):
        return {**base, "passed": None, "skipped": q["skip"], "detail": q["skip"]}
    if fake:
        plan = q.get("fake_plan")
        if not plan and q["expected"].get("kind") == "refusal":
            plan = [{"tool": "refuse", "args": {"reason": "Outside allowed use.", "rule": "out_of_scope_data"}}]
        if not plan:
            return {**base, "passed": None, "skipped": "no fake plan", "detail": "no scripted plan for the fake LLM"}
        llm = PlanLLM(plan)
    t0 = time.time()
    result = Agent(llm, ctx, write_audit=not fake).run(q["question"])
    answer, trace = result.answer, result.trace
    passed, detail = cmp.compare(q["expected"], answer)
    leaks = guards.contains_personal_data(answer)
    if leaks:
        passed, detail = False, f"personal data in the answer: {leaks[:3]}"
    rules = [r for s in trace.steps for r in s.summary()["validator_rules"]]
    return {**base, "passed": passed, "detail": detail, "status": answer.status, "got": _got(answer),
            "tools": [s.tool for s in trace.steps if s.executed],
            "governed": [s.result.governed for s in trace.steps if s.executed and s.result and s.result.governed is not None],
            "sql": [r.sql for r in trace.results if r.sql], "warnings": answer.warnings,
            "refusal_rule": answer.refusal.rule if answer.refusal else None,
            "tool_calls": trace.tool_calls, "latency_s": round(time.time() - t0, 2),
            "trap_hits": [r for r in rules if r.startswith("T")], "validator_rules": rules,
            "personal_data": leaks, "stopped": trace.stopped,
            "answer": answer.model_dump(mode="json")}           # full answer, for the submission CSV


def summarise(results) -> dict:
    scored = [r for r in results if r["passed"] is not None]
    ran = [r for r in results if "status" in r]

    def acc(rows):
        rows = [r for r in rows if r["passed"] is not None]
        return {"passed": sum(1 for r in rows if r["passed"]), "scored": len(rows),
                "accuracy": round(sum(1 for r in rows if r["passed"]) / len(rows), 4) if rows else None}

    tags = sorted({t for r in results for t in r["tags"]})
    refusals = [r for r in scored if r["expected"].get("kind") == "refusal"]
    groups: dict = {}
    for r in scored:
        if r.get("group"):
            groups.setdefault(r["group"], []).append(r["passed"])
    consistent = [all(v) for v in groups.values()]
    return {
        "questions": len(results), **acc(results),
        "manual": sum(1 for r in results if r["passed"] is None and not r.get("skipped")),
        "skipped": sum(1 for r in results if r.get("skipped")),
        "by_tag": {t: acc([r for r in results if t in r["tags"]]) for t in tags},
        "refusal_accuracy": acc(refusals)["accuracy"],
        "paraphrase_groups": len(groups),
        "paraphrase_groups_consistent": round(sum(consistent) / len(consistent), 4) if consistent else None,
        "avg_tool_calls": round(sum(r["tool_calls"] for r in ran) / len(ran), 2) if ran else None,
        "avg_latency_s": round(sum(r["latency_s"] for r in ran) / len(ran), 2) if ran else None,
        "trap_rule_hits": sum(len(r["trap_hits"]) for r in ran),
        "answers_with_personal_data": sum(1 for r in ran if r["personal_data"]),
        "governed_answers": sum(1 for r in ran if any(r["governed"])),
        "custom_sql_answers": sum(1 for r in ran if r["governed"] and not any(r["governed"])),
    }


def to_markdown(set_name, results, summary, model, fake, build) -> str:
    out = [f"# Layer 2 evaluation: {set_name}", "",
           f"Run {dt.datetime.now():%Y-%m-%d %H:%M} · model `{model}` · serving build {build.get('build_ts')} · "
           f"data as of {build.get('snapshot_date')}", ""]
    if fake:
        out += ["> **Fake LLM run.** The tool calls were scripted, so this proves the pipeline (tools, validator, guards, "
                "scoring) end to end. It says nothing about how well a real model answers.", ""]
    acc = "n/a" if summary["accuracy"] is None else f"{summary['accuracy']:.1%}"
    out += ["## Summary", "",
            f"- Accuracy: **{acc}** ({summary['passed']} of {summary['scored']} auto-scored; {summary['manual']} manual, {summary['skipped']} skipped)",
            f"- Refusal accuracy: {summary['refusal_accuracy'] if summary['refusal_accuracy'] is not None else 'n/a'}",
            f"- Paraphrase groups answered consistently: {summary['paraphrase_groups_consistent'] if summary['paraphrase_groups'] else 'n/a'}"
            + (f" ({summary['paraphrase_groups']} groups)" if summary["paraphrase_groups"] else ""),
            f"- Average tool calls: {summary['avg_tool_calls']} · average latency: {summary['avg_latency_s']} s",
            f"- Trap-rule hits (validator caught a mistake): {summary['trap_rule_hits']}",
            f"- Answers containing a phone number, email or deny-listed field: {summary['answers_with_personal_data']}",
            f"- Governed-metric answers: {summary['governed_answers']} · custom-SQL answers: {summary['custom_sql_answers']}", "",
            "### By tag", "", "| Tag | Passed | Scored | Accuracy |", "|---|---:|---:|---:|"]
    out += [f"| {t} | {a['passed']} | {a['scored']} | {'n/a' if a['accuracy'] is None else format(a['accuracy'], '.0%')} |"
            for t, a in summary["by_tag"].items()]
    out += ["", "## Questions", ""]
    for r in results:
        mark = "SKIP" if r.get("skipped") else "MANUAL" if r["passed"] is None else "PASS" if r["passed"] else "FAIL"
        out += [f"### {r['id']} — {mark}", "", f"**Q:** {r['question']}", ""]
        if r.get("skipped"):
            out += [f"Skipped: {r['skipped']}", ""]
            continue
        exp = r["expected"]
        shown = exp.get("value", exp.get("rows", exp.get("note", "")))
        out += [f"- Expected ({exp.get('kind')}): `{json.dumps(shown, default=str)[:300]}`",
                f"- Got ({r['status']}): {r['got']}", f"- Check: {r['detail']}",
                f"- Tools: {', '.join(r['tools']) or 'none'} · " + ("governed" if any(r["governed"]) else "custom" if r["governed"] else "no query")
                + f" · {r['tool_calls']} call(s) · {r['latency_s']} s"
                + (f" · validator rules hit: {', '.join(r['validator_rules'])}" if r["validator_rules"] else "")]
        out += [f"- SQL: `{' '.join(s.split())[:500]}`" for s in r["sql"]]
        if r["warnings"]:
            out.append("- Warnings: " + "; ".join(r["warnings"]))
        out.append("")
    return "\n".join(out)


def _model_failed(r) -> bool:
    """A result to run again on resume: never ran, or the model call itself failed (quota, outage)."""
    return "status" not in r or any("model error" in str(w) for w in r.get("warnings") or [])


def run_set(name, limit=None, fake=False, delay=0.0, questions_dir=None, synthetic_path=None, out_dir=None, ctx=None, llm=None,
            quiet=False, questions=None, reuse=None, only=None) -> dict:
    """Run one question set. Returns {"summary", "results", "report_md", "report_json"}.

    `questions` overrides the set's questions (e.g. loaded from a CSV); `reuse` is {question_id: earlier result}: those
    questions are not asked again unless the earlier run hit a model error; `only` restricts to these question ids."""
    ctx = ctx or ToolContext.build()
    if questions is None:
        questions = loaders.load_benchmark() if name == "benchmark" else loaders.load_set(name, questions_dir, synthetic_path)
    if only:
        wanted = {str(x).strip() for x in only}
        questions = [q for q in questions if q["id"] in wanted]
    if limit:
        questions = questions[:limit]
    reuse = reuse or {}
    if not fake and llm is None:
        from c360.layer2.llm import get_llm
        llm = get_llm()
    model = "fake-plan" if fake else getattr(llm, "label", None) or getattr(llm, "name", "unknown")
    out_dir = Path(out_dir) if out_dir else config.settings().out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    md, js = out_dir / f"eval_{name}_{stamp}.md", out_dir / f"eval_{name}_{stamp}.json"

    def checkpoint(partial):
        # written after every question, so an interrupted (paid) run can continue with --resume-from <this file>
        js.write_text(json.dumps({"set": name, "model": model, "fake": fake, "build": ctx.serving.build, "partial": partial,
                                  "summary": summarise(results), "results": results}, indent=2, default=str))
    results = []
    asked = 0
    for q in questions:
        prior = reuse.get(q["id"])
        if prior is not None and not _model_failed(prior):
            r = {**prior, "expected": q["expected"], "tags": q.get("tags", prior.get("tags", []))}
            if r.get("answer") and "status" in r:          # re-score against today's expected answer
                from c360.layer2.answer import Answer
                passed, detail = cmp.compare(q["expected"], Answer.model_validate(r["answer"]))
                r["passed"], r["detail"] = passed, detail + " (reused)"
            results.append(r)
            if not quiet:
                print(f"  [{name}] {r['id']:<6} reused", flush=True)
            continue
        if delay and asked and not fake:
            time.sleep(delay)
        asked += 1
        r = run_question(q, ctx, llm, fake)
        results.append(r)
        if not fake:
            checkpoint(partial=True)
        if not quiet:
            mark = "skip" if r.get("skipped") else "manual" if r["passed"] is None else "pass" if r["passed"] else "FAIL"
            print(f"  [{name}] {r['id']:<6} {mark:<6} {r['detail'][:100]}", flush=True)
    summary = summarise(results)
    md.write_text(to_markdown(name, results, summary, model, fake, ctx.serving.build))
    checkpoint(partial=False)
    answers_csv = None
    if name.startswith("benchmark") and not fake:
        from c360.layer2.eval import submission
        answers_csv = out_dir / f"benchmark_answers_{name}_{stamp}.csv"
        submission.write_csv(submission.merge_results([results]), answers_csv,
                             config.settings().data_root / "labels" / "benchmark_answers_template.csv" if name == "benchmark" else None)
        if not quiet:
            print(f"[eval] submission rows written -> {answers_csv}", flush=True)
    if not quiet:
        acc = "n/a" if summary["accuracy"] is None else f"{summary['accuracy']:.1%}"
        print(f"[eval] {name}: {acc} ({summary['passed']}/{summary['scored']}), {summary['manual']} manual, "
              f"{summary['skipped']} skipped, avg tool calls {summary['avg_tool_calls']} -> {md}", flush=True)
    return {"summary": summary, "results": results, "report_md": md, "report_json": js, "answers_csv": answers_csv}
