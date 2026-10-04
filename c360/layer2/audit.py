"""Audit log: one JSON line per question. Records what was asked, what ran and what was decided;
never the result rows, a customer document or note text."""
import datetime as dt
import json
from pathlib import Path

from c360.layer2 import config


def record(question, answer, trace) -> dict:
    return {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "question": question,
        "status": answer.status,
        "interpretation": answer.interpretation,
        "tools": [s.summary() for s in trace.steps],
        "sql": [r.sql for r in trace.results if r.sql],
        "source_ids": sorted({i for s in answer.sources for i in s.ids})[:100],
        "warnings": answer.warnings,
        "refusal_rule": answer.refusal.rule if answer.refusal else None,
        "stopped": trace.stopped,
        "model": answer.meta.model,
        "latency_ms": answer.meta.latency_ms,
        "tool_calls": answer.meta.tool_calls,
        "llm_calls": trace.llm_calls,
        "build_ts": answer.meta.build_ts,
        "snapshot_date": answer.meta.snapshot_date,
    }


def write(question, answer, trace, path=None) -> dict:
    """Append one line. A failure to write the log never fails the question (it adds a warning)."""
    entry = record(question, answer, trace)
    path = Path(path) if path else config.settings().audit_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        answer.warnings.append(f"audit log not written ({type(e).__name__})")
    return entry
