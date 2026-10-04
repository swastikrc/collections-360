"""Render the system prompt from the semantic layer, the metric catalogue and the serving database."""
from c360.layer2 import config
from c360.layer2.answer import ANSWER_SCHEMA_TEXT
from c360.layer2.tools import refuse as rf


def _tables(ctx) -> str:
    spec = ctx.semantic.get("tables") or {}
    out = []
    for table in sorted(ctx.serving.columns, key=lambda t: (list(spec).index(t) if t in spec else 999, t)):
        info = spec.get(table) or {}
        n = ctx.serving.row_counts.get(table)
        out.append(f"**l2.{table}** — {info.get('grain', 'grain unknown')}" + (f" ({n:,} rows)" if n is not None else ""))
        if info.get("description"):
            out.append(info["description"])
        described = info.get("columns") or {}
        for column, meta in ctx.serving.columns[table].items():
            desc = described.get(column) or meta.get("description") or ""
            desc = desc if isinstance(desc, str) else str(desc.get("description", ""))
            out.append(f"- `{column}` ({meta['type'].lower()})" + (f": {desc}" if desc else ""))
        out.append("")
    return "\n".join(out).rstrip()


def _metrics(ctx) -> str:
    out = []
    for key, spec in ctx.metrics.items():
        status = "verified" if spec.get("status") == "verified" else "placeholder definition"
        period = f"Period filters on the {spec.get('date_meaning', 'date')}"
        if spec.get("default_period"):
            period += f" (default: {spec['default_period']})"
        out.append(f"- `{key}` — {spec.get('label', key)} ({spec.get('unit', 'value')}, {status}). {spec.get('description', '')} "
                   f"{period}. Dimensions: {', '.join(spec.get('dimensions') or {}) or 'none'}.")
    return "\n".join(out)


def _codes(ctx) -> str:
    mapping = ctx.semantic.get("code_columns") or {}
    if not mapping or "code_lookups" not in ctx.serving.tables:
        return ""
    cols = ctx.serving.columns["code_lookups"]
    if not {"code_type", "code", "description"} <= set(cols):
        return ""
    lines = []
    for code_type, column in mapping.items():
        try:
            _, rows = ctx.serving.execute("SELECT code, description FROM l2.code_lookups WHERE code_type = ? ORDER BY code LIMIT 40", [code_type])
        except Exception:       # noqa: BLE001 - codes are a convenience; the prompt works without them
            continue
        if rows:
            lines.append(f"- `{column}`: " + "; ".join(f"{code} = {desc}" for code, desc in rows))
    return ("\n### Codes\n\n" + "\n".join(lines) + "\n") if lines else ""


def render_system(ctx) -> str:
    template = config.SYSTEM_PROMPT_MD.read_text()
    rules = rf.rules(ctx.semantic)
    values = {
        "snapshot_date": str(ctx.serving.snapshot_date),
        "max_tool_calls": str(ctx.settings.max_tool_calls),
        "metrics": _metrics(ctx),
        "tables": _tables(ctx),
        "joins": "\n".join(f"- {j}" for j in ctx.semantic.get("joins") or []),
        "terms": "\n".join(f"- **{k}**: {v}" for k, v in (ctx.semantic.get("terms") or {}).items()),
        "rules": "\n".join(f"{i}. {r}" for i, r in enumerate(ctx.semantic.get("rules") or [], 1)),
        "codes": _codes(ctx),
        "not_allowed": "\n".join(f"- `{k}`: {v}" for k, v in rules.items()),
        "answer_schema": ANSWER_SCHEMA_TEXT,
    }
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template
