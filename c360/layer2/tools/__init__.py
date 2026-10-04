"""Layer 2 tools. Every tool returns a ToolResult; what the model sees is ToolResult.to_llm()."""
import datetime as dt
import decimal
import json
from typing import Any

from pydantic import BaseModel, Field


def jsonable(v):
    """Plain JSON values: dates as ISO text, floats rounded to 6 decimals."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return None if v != v else round(v, 6)
    if isinstance(v, decimal.Decimal):
        return round(float(v), 6)
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d") if (v.hour, v.minute, v.second) == (0, 0, 0) else v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, (dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [jsonable(x) for x in v]
    if hasattr(v, "item"):
        return jsonable(v.item())
    return str(v)


class ToolResult(BaseModel):
    tool: str
    ok: bool = True
    data: Any = None                    # rows (list of dicts), a document, or text snippets
    columns: list[str] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    sql: str | None = None              # runnable SQL as executed (literals inlined), when the tool ran SQL
    source_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    governed: bool | None = None        # True: governed metric; False: custom SQL; None: not a query
    detail: str | None = None           # e.g. "Promise-kept rate (placeholder definition)"
    meta: dict = Field(default_factory=dict)

    def payload(self) -> dict:
        """The compact body sent to the model."""
        if not self.ok:
            return {"ok": False, "error": self.error}
        out: dict = {"ok": True}
        if self.detail:
            out["what"] = self.detail
        out.update({k: v for k, v in self.meta.items()
                    if k in ("period", "unit", "group_by", "filters", "search_mode", "date_meaning") and v not in (None, [], "")})
        if isinstance(self.data, list) and self.columns:
            out["columns"] = self.columns
            out["rows"] = [[jsonable(r.get(c)) for c in self.columns] for r in self.data]
            out["row_count"] = self.row_count
        elif self.data is not None:
            out["data"] = jsonable(self.data)
        if self.truncated:
            out["truncated"] = True
        if self.warnings:
            out["warnings"] = self.warnings
        return out

    def to_llm(self) -> str:
        """Serialised result, wrapped and marked as data. `<` is escaped so text inside a note or a
        transcript can never close the wrapper or open a fake one."""
        body = json.dumps(self.payload(), ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
        return f'<tool_result tool="{self.tool}" trust="data">\n{body}\n</tool_result>'


def failure(tool: str, message: str, **kw) -> ToolResult:
    return ToolResult(tool=tool, ok=False, error=message, **kw)
