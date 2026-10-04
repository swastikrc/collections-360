"""The structured answer, and the trace of how it was produced.

The model fills only the fields of `LLMAnswer`. `sources`, `warnings` and `meta` are always filled
by code from the trace and are never trusted from the model.
"""
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from c360.layer2.tools import ToolResult

Interpretation = list[str]      # short tokens, e.g. ["promise-kept rate", "1 Jul-28 Sep 2026", "by hardship reason"]
MAX_FOLLOW_UPS = 3


class Table(BaseModel):
    columns: list[str]
    rows: list[list[Any]] = Field(default_factory=list)


class Refusal(BaseModel):
    rule: str
    reason: str


class SourceRef(BaseModel):
    kind: Literal["governed_metric", "sql", "customer", "notes", "transcripts", "policy"]
    detail: str
    ids: list[str] = Field(default_factory=list)
    sql: str | None = None


class Meta(BaseModel):
    snapshot_date: str | None = None
    build_ts: str | None = None
    model: str | None = None
    tool_calls: int = 0
    latency_ms: int = 0


class LLMAnswer(BaseModel):
    """What the model must return as its final message (and nothing else)."""
    status: Literal["answered", "refused", "partial"] = "answered"
    interpretation: Interpretation = Field(default_factory=list)
    answer_text: str
    table: Table | None = None
    insight: str | None = None
    follow_ups: list[str] = Field(default_factory=list)
    refusal: Refusal | None = None

    @field_validator("interpretation", "follow_ups", mode="before")
    @classmethod
    def _strings(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return [str(x) for x in v if x not in (None, "")]

    @field_validator("follow_ups")
    @classmethod
    def _cap(cls, v):
        return v[:MAX_FOLLOW_UPS]

    @field_validator("table", mode="before")
    @classmethod
    def _table(cls, v):
        if isinstance(v, Table):
            return v
        if not v or not isinstance(v, dict) or not v.get("columns"):
            return None
        return v

    @field_validator("insight", mode="before")
    @classmethod
    def _insight(cls, v):
        return str(v) if v not in (None, "") else None

    @field_validator("refusal", mode="before")
    @classmethod
    def _refusal(cls, v):
        if isinstance(v, Refusal):
            return v
        return v if isinstance(v, dict) and v.get("reason") else None


class Answer(LLMAnswer):
    sources: list[SourceRef] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    meta: Meta = Field(default_factory=Meta)


ANSWER_SCHEMA_TEXT = """{
  "status": "answered" | "refused" | "partial",
  "interpretation": ["short tokens saying how you read the question, e.g. the metric, the period, the breakdown"],
  "answer_text": "1-4 sentences that answer the question",
  "table": {"columns": ["..."], "rows": [["...", 0.0]]} or null,
  "insight": "one line grounded in the results, or null",
  "follow_ups": ["0-3 useful next questions"],
  "refusal": {"rule": "...", "reason": "..."} or null
}"""


def extract_json(text: str) -> dict:
    """The JSON object in a model reply (tolerates code fences and stray prose around it)."""
    if not text or not text.strip():
        raise ValueError("empty reply")
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, flags=re.DOTALL)
    if fence:
        t = fence.group(1)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    obj = json.loads(t[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("the reply is not a JSON object")
    return obj


def parse_llm_answer(text: str) -> LLMAnswer:
    """Raises ValueError when the reply is not valid JSON for the Answer schema."""
    try:
        return LLMAnswer.model_validate(extract_json(text))
    except ValueError:
        raise
    except Exception as e:      # noqa: BLE001 - pydantic ValidationError and friends
        raise ValueError(str(e)) from e


# ---- trace ---------------------------------------------------------------------------------------
class TraceStep(BaseModel):
    tool: str
    args: dict = Field(default_factory=dict)
    executed: bool = True               # False: duplicate call or over budget (the tool did not run)
    note: str | None = None             # why it did not run
    result: ToolResult | None = None

    @property
    def ok(self) -> bool:
        return bool(self.result and self.result.ok)

    def summary(self) -> dict:
        """What the audit log keeps: never the result rows."""
        r = self.result
        return {"tool": self.tool, "args": self.args, "executed": self.executed, "ok": self.ok,
                "row_count": r.row_count if r else 0, "governed": r.governed if r else None,
                "error": (r.error if r else self.note), "validator_rules": (r.meta.get("rules") if r else None) or []}


class Trace(BaseModel):
    question: str = ""
    plan: str | None = None
    steps: list[TraceStep] = Field(default_factory=list)
    llm_calls: int = 0
    stopped: str | None = None          # None | "tool_budget" | "consecutive_errors" | "llm_error" | "input_guard"
    repaired: bool = False
    raw_final: str | None = None

    @property
    def results(self) -> list[ToolResult]:
        return [s.result for s in self.steps if s.executed and s.result is not None]

    @property
    def tool_calls(self) -> int:
        return sum(1 for s in self.steps if s.executed)
