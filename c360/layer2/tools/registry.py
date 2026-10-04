"""Tool declarations (JSON Schema, kept simple for Gemini) and dispatch."""
import inspect
from dataclasses import dataclass, field
from typing import Any

from c360.layer2 import config, periods
from c360.layer2.llm.base import ToolSpec
from c360.layer2.tools import ToolResult, failure, sql_guard
from c360.layer2.tools import compute_metric as cm
from c360.layer2.tools import get_customer as gc
from c360.layer2.tools import refuse as rf
from c360.layer2.tools import run_sql as rs
from c360.layer2.tools import search_text as st
from c360.layer2.tools import search_policy as sp


@dataclass
class ToolContext:
    """Everything a tool needs. Built once per process; holds no per-question state."""
    serving: Any
    settings: Any
    semantic: dict
    metrics: dict
    guard: sql_guard.GuardContext
    mongo: Any = None
    last_n_days: str = "inclusive"
    _mongo_factory: Any = field(default=None, repr=False)

    @classmethod
    def build(cls, serving=None, mongo=None, semantic=None, metrics=None):
        from c360.layer2 import serving as serving_mod
        settings = config.settings()
        serving = serving or serving_mod.open_current()
        semantic = semantic if semantic is not None else config.load_yaml(config.SEMANTIC_YAML)
        metrics = metrics if metrics is not None else config.load_yaml(config.METRICS_YAML)
        return cls(serving=serving, settings=settings, semantic=semantic, metrics=metrics, mongo=mongo,
                   guard=sql_guard.GuardContext.from_serving(serving, semantic, settings.max_rows),
                   last_n_days=(semantic.get("periods") or {}).get("last_n_days", "inclusive"),
                   _mongo_factory=config.mongo_db)

    def mongo_db(self):
        if self.mongo is None:
            self.mongo = self._mongo_factory()
        return self.mongo

    def resolve_period(self, text):
        return periods.resolve(text, self.serving.snapshot_date, self.last_n_days)


_FUNCTIONS = {
    "compute_metric": cm.compute_metric,
    "run_sql": rs.run_sql,
    "get_customer": gc.get_customer,
    "search_text": st.search_text,
    "search_policy": sp.search_policy,
    "refuse": rf.refuse,
}
TOOL_NAMES = list(_FUNCTIONS)


def specs(ctx: ToolContext) -> list[ToolSpec]:
    metric_names = sorted(ctx.metrics)
    refusal_rules = list(rf.rules(ctx.semantic))
    return [
        ToolSpec("compute_metric",
                 "Compute a governed collections metric. The SQL is built by tested code from the metric's official "
                 "template, so prefer this tool whenever a metric fits the question.",
                 {"type": "object",
                  "properties": {
                      "metric": {"type": "string", "enum": metric_names, "description": "Which metric to compute."},
                      "period": {"type": "string", "description":
                                 "The period as a phrase, resolved in code against the snapshot date. Examples: 'last 90 days', "
                                 "'August 2026', 'this quarter', 'last month', 'year to date', '2026-01-01 to 2026-06-30', 'all time'."},
                      "group_by": {"type": "array", "items": {"type": "string"},
                                   "description": "Up to 2 dimension names of this metric to break the result down by."},
                      "filters": {"type": "array", "description": "Optional filters on this metric's dimensions.",
                                  "items": {"type": "object",
                                            "properties": {"dimension": {"type": "string"},
                                                           "op": {"type": "string", "enum": list(cm.OPS)},
                                                           "value": {"type": "string", "description": "For op 'in': comma-separated values."}},
                                            "required": ["dimension", "op", "value"]}}},
                  "required": ["metric", "period"]}),
        ToolSpec("run_sql",
                 "Run one read-only DuckDB SELECT on the l2 tables. Use only when no governed metric fits. The query is "
                 "validated; if it is rejected, read the message, fix the query and call again.",
                 {"type": "object",
                  "properties": {
                      "query": {"type": "string", "description": "A single SELECT statement over schema l2."},
                      "purpose": {"type": "string", "enum": ["analysis", "data_quality"], "description":
                                  "'data_quality' only when the question is about data quality (it unlocks the *_recomputed columns)."}},
                  "required": ["query", "purpose"]}),
        ToolSpec("get_customer",
                 "Fetch one customer's Collections 360 record by any id: C360 id, case id, account id, CRM id, card customer "
                 "ref, borrower ref or CIF. For questions about a specific customer, case or account.",
                 {"type": "object",
                  "properties": {"key": {"type": "string", "description": "The id exactly as given in the question."}},
                  "required": ["key"]}),
        ToolSpec("search_text",
                 "Search agent notes and call transcripts for what customers and agents said. Returns short snippets with "
                 "their ids. Use for why / reasons / themes / complaints questions.",
                 {"type": "object",
                  "properties": {
                      "query": {"type": "string", "description": "Keywords to search for, e.g. 'lost job', 'dispute late fee'."},
                      "source": {"type": "string", "enum": ["notes", "transcripts", "both"]},
                      "period": {"type": "string", "description": "Optional period phrase, e.g. 'last 30 days'."},
                      "case_id": {"type": "string"},
                      "c360_id": {"type": "string"},
                      "queue": {"type": "string", "description": "Optional collections queue to restrict to."},
                      "product": {"type": "string", "description": "Optional product (card | loan) to restrict to."},
                      "limit": {"type": "integer", "description": f"Snippets to return (default {st.DEFAULT_LIMIT}, max {st.MAX_LIMIT})."}},
                  "required": ["query"]}),
        ToolSpec("search_policy",
                 "Search the bank's current policies, procedures, hardship programme terms, regulatory summaries, call "
                 "scripts, FAQ and incident reports. Returns the most relevant sections with their citation (e.g. "
                 "'POL-COLL-001 §5.2'). Superseded versions are never returned, and policy outranks FAQ and scripts. Use "
                 "for rules, limits, what an agent must or may do, programme terms, and what happened in an incident.",
                 {"type": "object",
                  "properties": {
                      "query": {"type": "string", "description": "Keywords for the rule or topic, e.g. 'call attempts per week'."},
                      "limit": {"type": "integer", "description": f"Sections to return (default {sp.DEFAULT_K}, max {sp.MAX_K})."},
                      "language": {"type": "string", "enum": ["EN", "FR"], "description": "Document language (default EN)."}},
                  "required": ["query"]}),
        ToolSpec("refuse",
                 "Decline a question that is outside allowed use or outside the data. Ends the conversation turn.",
                 {"type": "object",
                  "properties": {
                      "reason": {"type": "string", "description": "One or two sentences for the user: what cannot be done and why."},
                      "rule": {"type": "string", "enum": refusal_rules}},
                  "required": ["reason", "rule"]}),
    ]


def dispatch(name, args, ctx: ToolContext) -> ToolResult:
    """Run one tool call. Never raises: every failure comes back as ToolResult(ok=False)."""
    fn = _FUNCTIONS.get(name)
    if fn is None:
        return failure(str(name), f"Unknown tool '{name}'. Available tools: {', '.join(TOOL_NAMES)}.")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return failure(name, "Tool arguments must be an object.")
    accepted = [p for p in inspect.signature(fn).parameters if p != "ctx"]
    unknown = [k for k in args if k not in accepted]
    if unknown:
        return failure(name, f"Unknown argument(s) for {name}: {', '.join(unknown)}. Accepted: {', '.join(accepted)}.")
    try:
        return fn(ctx, **args)
    except Exception as e:      # noqa: BLE001 - a tool bug must not break the agent loop
        return failure(name, f"{name} failed unexpectedly ({type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}).")
