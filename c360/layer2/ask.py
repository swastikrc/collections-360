"""Ask the Collections 360 a question in plain English.

    python -m c360.layer2.ask "Which hardship reason has the best promise-kept rate in the last 90 days?"
    python -m c360.layer2.ask --json "..."      print the Answer as JSON
    python -m c360.layer2.ask --trace "..."     also print each tool call and validator message
    python -m c360.layer2.ask --repl            interactive; keeps the conversation for follow-ups

Every answer shows how it was produced: the governed metric or SQL that ran, or the ids of the
records, notes and transcripts it came from. Questions outside allowed use are refused.
"""
import argparse
import json
import sys

from c360.layer2.agent import Agent, history_messages
from c360.layer2.answer import Answer, Trace

MAX_TABLE_ROWS = 20
KIND_LABEL = {"governed_metric": "governed metric", "sql": "custom SQL", "customer": "customer record",
              "notes": "agent notes", "transcripts": "call transcripts", "policy": "policy documents"}
SEP = " · "


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:,.2f}"
    return str(v)


def format_table(table, max_rows=MAX_TABLE_ROWS) -> str:
    rows = [[_cell(v) for v in row] for row in table.rows[:max_rows]]
    cols = [str(c) for c in table.columns]
    rows = [r + [""] * (len(cols) - len(r)) for r in rows]
    widths = [max(len(cols[i]), *(len(r[i]) for r in rows)) if rows else len(cols[i]) for i in range(len(cols))]
    numeric = [bool(rows) and all(_is_number(r[i]) for r in rows if r[i] != "") for i in range(len(cols))]

    def line(values):
        return "  " + "  ".join(v.rjust(w) if numeric[i] else v.ljust(w) for i, (v, w) in enumerate(zip(values, widths))).rstrip()

    out = [line(cols), "  " + "  ".join("-" * w for w in widths)] + [line(r) for r in rows]
    if len(table.rows) > max_rows:
        out.append(f"  ... {len(table.rows) - max_rows} more rows")
    return "\n".join(out)


def _is_number(text) -> bool:
    try:
        float(text.replace(",", "").rstrip("%"))
        return True
    except ValueError:
        return False


def format_trace(trace: Trace) -> str:
    out = ["Trace:"]
    if trace.plan:
        out.append(f"  plan: {trace.plan}")
    for i, step in enumerate(trace.steps, 1):
        args = json.dumps(step.args, ensure_ascii=False, default=str)
        if not step.executed:
            out.append(f"  {i}. {step.tool} {args}\n     -> not executed ({step.note})")
        elif step.ok:
            r = step.result
            out.append(f"  {i}. {step.tool} {args}\n     -> ok, {r.row_count} row(s)" + (", truncated" if r.truncated else ""))
            out.extend(f"     validator/sanity: {w}" for w in r.warnings)
        else:
            out.append(f"  {i}. {step.tool} {args}\n     -> error: {step.result.error}")
    out.append(f"  model calls: {trace.llm_calls}" + (f", stopped: {trace.stopped}" if trace.stopped else "")
               + (", final answer needed a repair" if trace.repaired else ""))
    return "\n".join(out)


def format_answer(answer: Answer, trace: Trace | None = None, show_trace=False) -> str:
    out = []
    if answer.interpretation:
        out += ["Interpreted as: " + SEP.join(answer.interpretation), ""]
    if answer.status == "refused":
        rule = answer.refusal.rule if answer.refusal else "refused"
        out += [f"Refused ({rule}): {answer.refusal.reason if answer.refusal else answer.answer_text}", ""]
    else:
        out += [("[Partial answer] " if answer.status == "partial" else "") + answer.answer_text, ""]
        if answer.table and answer.table.rows:
            out += [format_table(answer.table), ""]
        if answer.insight:
            out.append(f"Insight: {answer.insight}")
        if answer.follow_ups:
            out.append("Try next: " + " | ".join(answer.follow_ups))
        if answer.insight or answer.follow_ups:
            out.append("")
    if answer.sources:
        out.append("How we got this:")
        for s in answer.sources:
            out.append(f"  [{KIND_LABEL.get(s.kind, s.kind)}] {s.detail}")
            if s.sql:
                out.append("  SQL: " + s.sql.strip().replace("\n", "\n       "))
            elif s.ids:
                shown = ", ".join(s.ids[:12]) + (f", ... ({len(s.ids)} ids)" if len(s.ids) > 12 else "")
                out.append(f"  Source ids: {shown}")
    elif answer.status != "refused":
        out.append("How we got this:\n  No data was retrieved for this answer.")
    if answer.warnings:
        out.append("Warnings: " + "\n          ".join(answer.warnings))
    if show_trace and trace is not None:
        out += ["", format_trace(trace)]
    m = answer.meta
    out.append(f"Data as of {m.snapshot_date}{SEP}serving build {m.build_ts}{SEP}model {m.model}{SEP}"
               f"{m.tool_calls} tool call{'' if m.tool_calls == 1 else 's'}{SEP}{m.latency_ms / 1000:.1f}s")
    return "\n".join(out)


def build_agent(llm=None) -> Agent:
    from c360.layer2.llm import get_llm
    from c360.layer2.tools.registry import ToolContext
    ctx = ToolContext.build()
    return Agent(llm or get_llm(), ctx)


def main(argv=None, llm=None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(prog="python -m c360.layer2.ask", description="Ask the Collections 360 in plain English.")
    ap.add_argument("question", nargs="*", help="the question (quote it)")
    ap.add_argument("--json", action="store_true", help="print the Answer as JSON")
    ap.add_argument("--trace", action="store_true", help="also print each tool call and validator message")
    ap.add_argument("--repl", action="store_true", help="interactive loop; keeps the conversation for follow-ups")
    a = ap.parse_args(argv)
    question = " ".join(a.question).strip()
    if not question and not a.repl:
        ap.print_usage(file=sys.stderr)
        return 2

    from c360.layer2.llm.gemini import LLMConfigError
    from c360.layer2.serving import ServingError
    try:
        agent = build_agent(llm)
    except (LLMConfigError, ServingError, FileNotFoundError) as e:
        print(f"Cannot start: {e}", file=sys.stderr)
        return 2

    def answer_one(q, history):
        result = agent.run(q, history)
        if a.json:
            payload = result.answer.model_dump()
            if a.trace:
                payload["trace"] = {"plan": result.trace.plan, "steps": [s.summary() for s in result.trace.steps],
                                    "llm_calls": result.trace.llm_calls, "stopped": result.trace.stopped}
            print(json.dumps(payload, indent=2, ensure_ascii=False, default=str), file=out)
        else:
            print(format_answer(result.answer, result.trace, a.trace), file=out)
        return result

    if not a.repl:
        answer_one(question, [])
        return 0

    history = []
    print("Collections 360 — ask a question (blank line or 'exit' to leave).", file=out)
    if question:
        history += history_messages(question, answer_one(question, history).answer)
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=out)
            break
        if not q or q.lower() in ("exit", "quit", "q"):
            break
        history += history_messages(q, answer_one(q, history).answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
