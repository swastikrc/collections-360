"""compute_metric: governed metrics. The model picks a metric and parameters; this code builds
the SQL from the tested templates in semantic/metrics.yaml. Values are bound as parameters,
never interpolated into the SQL that runs."""
import datetime as dt
import re

from c360.layer2 import periods, sanity, serving as serving_mod
from c360.layer2.tools import ToolResult, failure, run_sql as run_sql_mod, sql_guard

OPS = ("=", "!=", "in", ">=", "<=")
MAX_GROUP_BY = 2


def _norm(name) -> str:
    return re.sub(r"[\s\-]+", "_", str(name or "").strip().lower())


def find_metric(metrics, name):
    key = _norm(name)
    if key in metrics:
        return key, metrics[key]
    for k, spec in metrics.items():
        if key in {_norm(a) for a in (spec.get("aliases") or [])} or key == _norm(spec.get("label")):
            return k, spec
    return None, None


def dimension(spec, name):
    """(expression, type) of a metric's dimension, or None."""
    d = (spec.get("dimensions") or {}).get(_norm(name))
    if d is None:
        return None
    return (d["expr"], d.get("type", "string")) if isinstance(d, dict) else (str(d), "string")


def _coerce(value, typ):
    if typ == "bool":
        if isinstance(value, bool):
            return value
        if str(value).strip().lower() in ("true", "t", "yes", "1"):
            return True
        if str(value).strip().lower() in ("false", "f", "no", "0"):
            return False
        raise ValueError(f"'{value}' is not true/false")
    if typ == "int":
        return int(float(value))
    if typ == "float":
        return float(value)
    return value if isinstance(value, str) else str(value)


def literal(v) -> str:
    """A SQL literal for display. The executed statement uses bound parameters instead."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return f"DATE '{v.isoformat()[:10]}'"
    return "'" + str(v).replace("'", "''") + "'"


def inline(sql, params) -> str:
    return re.sub(r"\$(\w+)", lambda m: literal(params[m.group(1)]) if m.group(1) in params else m.group(0), sql)


def render(spec, dims, conditions, max_rows):
    """SQL (with $start/$end/$f.. parameters) for a metric, its group-by dimensions and filter conditions."""
    dims_sql = "".join(f"{expr} AS {name}, " for name, expr in dims)
    conds = list(spec.get("default_filters") or []) + list(conditions)
    filters_sql = "".join(f"\n  AND {c}" for c in conds)
    group_by = "GROUP BY ALL" if dims else ""
    if spec.get("sql"):
        body = (spec["sql"].strip().replace("{dims}", dims_sql).replace("{filters}", filters_sql)
                .replace("{group_by}", group_by))
    else:
        if spec.get("unit") == "ratio":
            num, den = spec["numerator"], spec["denominator"]
            value = f"{num} AS numerator, {den} AS denominator, CAST({num} AS DOUBLE) / nullif({den}, 0) AS value"
        else:
            value = f"{spec['value']} AS value"
        body = (f"SELECT {dims_sql}{value}\nFROM {spec['from']}\n"
                f"WHERE CAST({spec['date_column']} AS DATE) BETWEEN $start AND $end{filters_sql}\n{group_by}")
    names = [n for n, _ in dims]
    if names:
        time_dims = [n for n in names if n in ("month", "week", "quarter", "year", "hour")]
        order = "\nORDER BY " + ", ".join(time_dims + (["value DESC NULLS LAST"] if not time_dims else [])
                                           + [n for n in names if n not in time_dims])
    else:
        order = ""
    return f"{body.rstrip()}{order}\nLIMIT {max_rows + 1}"


def compute_metric(ctx, metric="", period=None, group_by=None, filters=None) -> ToolResult:
    tool = "compute_metric"
    key, spec = find_metric(ctx.metrics, metric)
    if spec is None:
        return failure(tool, f"Unknown metric '{metric}'. Valid metrics: {', '.join(sorted(ctx.metrics))}. "
                             "If none fits the question, use run_sql.")
    valid_dims = ", ".join(spec.get("dimensions") or {}) or "(none)"

    if isinstance(group_by, str):
        group_by = [g for g in re.split(r"[,\s]+", group_by) if g]
    group_by = list(group_by or [])
    if len(group_by) > MAX_GROUP_BY:
        return failure(tool, f"group_by accepts at most {MAX_GROUP_BY} dimensions.")
    dims = []
    for g in group_by:
        d = dimension(spec, g)
        if d is None:
            return failure(tool, f"'{g}' is not a dimension of {key}. Valid dimensions: {valid_dims}.")
        if _norm(g) not in [n for n, _ in dims]:
            dims.append((_norm(g), d[0]))

    period_text = period if (period and str(period).strip()) else spec.get("default_period")
    if not period_text:
        return failure(tool, f"{key} needs a period (it filters on the {spec.get('date_meaning', 'date')}). "
                             f"Accepted forms: {periods.ACCEPTED}")
    try:
        start, end, label = ctx.resolve_period(period_text)
    except periods.PeriodError as e:
        return failure(tool, str(e))

    params = {"start": start, "end": end}
    conditions, shown_filters = [], []
    if isinstance(filters, dict):
        filters = [filters]
    for i, f in enumerate(filters or []):
        if not isinstance(f, dict) or "dimension" not in f or "value" not in f:
            return failure(tool, "Each filter must be an object {dimension, op, value}.")
        d = dimension(spec, f["dimension"])
        if d is None:
            return failure(tool, f"'{f['dimension']}' is not a dimension of {key}. Valid dimensions: {valid_dims}.")
        expr, typ = d
        op = str(f.get("op") or "=").strip().lower()
        op = {"==": "=", "<>": "!=", "eq": "=", "ne": "!="}.get(op, op)
        if op not in OPS:
            return failure(tool, f"Filter operator '{f.get('op')}' is not supported. Use one of: {', '.join(OPS)}.")
        raw = f["value"]
        try:
            if op == "in":
                values = raw if isinstance(raw, (list, tuple)) else [v.strip() for v in str(raw).split(",") if v.strip()]
                if not values:
                    return failure(tool, "An 'in' filter needs at least one value (comma-separated).")
                names = []
                for j, v in enumerate(values):
                    params[f"f{i}_{j}"] = _coerce(v, typ)
                    names.append(f"$f{i}_{j}")
                conditions.append(f"{expr} IN ({', '.join(names)})")
            else:
                params[f"f{i}"] = _coerce(raw, typ)
                conditions.append(f"{expr} {op} $f{i}")
        except (TypeError, ValueError) as e:
            return failure(tool, f"Filter value for '{f['dimension']}' is not valid: {e}.")
        shown_filters.append(f"{_norm(f['dimension'])} {op} {raw}")

    sql = render(spec, dims, conditions, ctx.settings.max_rows)
    shown = inline(sql, params)
    verdict = sql_guard.validate(shown, ctx.guard, "analysis")       # defence in depth
    if not verdict.ok:
        return failure(tool, f"The definition of {key} did not pass the SQL validator ({sql_guard.error_text(verdict.issues)}). "
                             "This is a configuration problem in metrics.yaml, not a problem with the question.", sql=shown)
    try:
        columns, rows = ctx.serving.execute(sql, params)
    except serving_mod.QueryTimeout as e:
        return failure(tool, str(e), sql=shown)
    except Exception as e:      # noqa: BLE001
        return failure(tool, f"The metric query failed: {str(e).splitlines()[0][:400]}", sql=shown)

    status = spec.get("status", "placeholder")
    label_text = spec.get("label", key)
    detail = f"Governed metric: {label_text}" + ("" if status == "verified" else " (placeholder definition)")
    result = run_sql_mod.rows_to_result(
        tool, columns, rows, ctx.settings.max_rows, sql=shown, governed=True, detail=detail,
        # T5 (count over a join) is a hint for hand-written SQL; governed definitions are tested against it.
        warnings=[w for w in sql_guard.warning_texts(verdict.issues) if not w.startswith("[T5]")],
        meta={"metric": key, "label": label_text, "status": status, "unit": spec.get("unit"), "period": label,
              "period_start": start.isoformat(), "period_end": end.isoformat(), "date_meaning": spec.get("date_meaning"),
              "group_by": [n for n, _ in dims], "filters": shown_filters})
    return sanity.check(result, {"row_counts": ctx.serving.row_counts, "tables": run_sql_mod.referenced_tables(shown),
                                 "unit": spec.get("unit"), "max_rows": ctx.settings.max_rows})
