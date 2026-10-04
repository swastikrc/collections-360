"""run_sql: the fallback for questions no governed metric covers. Read-only, validated SQL."""
import re

from c360.layer2 import sanity, serving as serving_mod
from c360.layer2.tools import ToolResult, failure, sql_guard

LABEL = "Custom query — not a governed metric"
_ID_COLUMNS = ("c360_id", "case_id", "note_id", "transcript_id", "contact_id", "ptp_id", "offer_id",
               "card_account_id", "loan_id", "deposit_account_id", "account_id")
MAX_SOURCE_IDS = 50


def referenced_tables(sql) -> list[str]:
    return sorted(set(re.findall(r"\bl2\.(\w+)", sql or "", flags=re.IGNORECASE)))


def unique_columns(columns) -> list[str]:
    seen, out = {}, []
    for c in columns:
        seen[c] = seen.get(c, 0) + 1
        out.append(c if seen[c] == 1 else f"{c}_{seen[c]}")
    return out


def rows_to_result(tool, columns, rows, max_rows, **kw) -> ToolResult:
    columns = unique_columns(columns)
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    data = [dict(zip(columns, r)) for r in rows]
    ids = []
    for col in columns:
        if col in _ID_COLUMNS:
            ids.extend(str(r[col]) for r in data if r[col] is not None)
    return ToolResult(tool=tool, data=data, columns=columns, row_count=len(data), truncated=truncated,
                      source_ids=list(dict.fromkeys(ids))[:MAX_SOURCE_IDS], **kw)


def run_sql(ctx, query="", purpose="analysis") -> ToolResult:
    purpose = purpose if purpose in ("analysis", "data_quality") else "analysis"
    verdict = sql_guard.validate(query, ctx.guard, purpose)
    if not verdict.ok:
        return failure("run_sql", "Query rejected by the validator. " + sql_guard.error_text(verdict.issues),
                       meta={"rules": [i.rule for i in verdict.issues if i.severity == "error"], "rejected_sql": str(query)[:2000]})
    try:
        columns, rows = ctx.serving.execute(verdict.sql)
    except serving_mod.QueryTimeout as e:
        return failure("run_sql", str(e), sql=verdict.sql)
    except Exception as e:      # noqa: BLE001 - binder/catalog errors go back to the model so it can fix the query
        return failure("run_sql", "The query failed: " + str(e).split("\n\n")[0].replace("\n", " ")[:600], sql=verdict.sql)
    result = rows_to_result("run_sql", columns, rows, ctx.settings.max_rows, sql=verdict.sql, governed=False, detail=LABEL,
                            warnings=sql_guard.warning_texts(verdict.issues), meta={"purpose": purpose})
    return sanity.check(result, {"row_counts": ctx.serving.row_counts, "tables": referenced_tables(verdict.sql),
                                 "unit": None, "max_rows": ctx.settings.max_rows})
