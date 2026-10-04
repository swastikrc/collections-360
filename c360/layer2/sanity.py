"""Sanity checks on a tool result before the model sees it. Warnings, never hard failures."""
import re

_RATIO_NAME = re.compile(r"(^|_)(rate|ratio|share|pct_of|proportion)($|_)", re.IGNORECASE)
_COUNT_NAME = re.compile(r"(^|_)(count|cnt|n|num|number|numerator|denominator|cases|customers|accounts|contacts|promises|attempts)($|_)", re.IGNORECASE)
_AMOUNT_NAME = re.compile(r"(amount|overdue|payment|collected)", re.IGNORECASE)


def _numbers(result, column):
    return [r.get(column) for r in result.data if isinstance(r.get(column), (int, float)) and not isinstance(r.get(column), bool)]


def check(result, context=None):
    """Attach warnings to `result` (a ToolResult holding rows). Returns the same object.

    context: {"row_counts": {table: n}, "tables": [referenced L2 tables], "unit": ratio|count|amount|days|None,
              "max_rows": N}
    """
    context = context or {}
    if not result.ok or not isinstance(result.data, list):
        return result
    warnings = list(result.warnings)
    unit = context.get("unit")
    row_counts = context.get("row_counts") or {}
    largest = max((row_counts.get(t, 0) for t in context.get("tables") or []), default=0)

    if result.row_count == 0:
        warnings.append("No rows matched: check the filters and the date range before concluding the answer is zero.")
    if result.truncated:
        warnings.append(f"Only the first {context.get('max_rows', result.row_count)} rows are shown; the full result is larger. "
                        "Aggregate or filter instead of listing rows.")

    for column in result.columns:
        values = _numbers(result, column)
        if not values:
            continue
        is_value = column == "value"
        if (is_value and unit == "ratio") or (not is_value and _RATIO_NAME.search(column)):
            bad = [v for v in values if v < 0 or v > 1]
            if bad:
                warnings.append(f"`{column}` has values outside 0-1 ({bad[0]:.4g}): a ratio should be between 0 and 1. "
                                "Check the numerator and denominator.")
        countish = (is_value and unit == "count") or column in ("numerator", "denominator") or (not is_value and _COUNT_NAME.search(column))
        if countish and largest and all(float(v).is_integer() for v in values):
            top = max(values)
            if top > largest:
                warnings.append(f"`{column}` is {int(top):,}, larger than the biggest table in the query ({largest:,} rows): "
                                "a join is probably multiplying rows. Count DISTINCT ids.")
        if (is_value and unit == "amount") or (not is_value and _AMOUNT_NAME.search(column)):
            if any(v < 0 for v in values):
                warnings.append(f"`{column}` has negative values where an amount should not be negative.")

    seen = set()
    result.warnings = [w for w in warnings if not (w in seen or seen.add(w))]
    return result
