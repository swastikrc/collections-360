"""Compare an Answer with an expected result: scalar / table / set / refusal / contains / manual.

The comparison reads what the person would read: the answer text and the answer table. A number
that only exists in a tool result but was not reported does not count.
"""
import re

RATIO_TOL = 0.005       # absolute, for ratios
REL_TOL = 0.005         # relative, for counts, amounts and days

_NUM = re.compile(r"(?<![\w.])(-?\$?\d[\d,]*(?:\.\d+)?)\s?(%|percent\b|k\b|m\b|million\b|thousand\b)?", re.IGNORECASE)
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}


def norm(text) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_\-–]+", " ", str(text).lower())).strip()


def numbers_in(text) -> list[float]:
    """Every number in a piece of text, with percentages also offered as ratios."""
    out = []
    for m in _NUM.finditer(str(text or "")):
        raw = m.group(1).replace("$", "").replace(",", "")
        try:
            x = float(raw)
        except ValueError:
            continue
        suffix = (m.group(2) or "").lower()
        if suffix in ("%", "percent"):
            out += [x / 100.0, x]
        else:
            out.append(x * _SCALE.get(suffix, 1.0))
    return out


def cell_numbers(cell) -> list[float]:
    if isinstance(cell, bool) or cell is None:
        return []
    if isinstance(cell, (int, float)):
        return [float(cell)]
    return numbers_in(cell)


def close(expected, candidates, spec) -> bool:
    unit = spec.get("unit")
    if "tolerance" in spec:
        tol = float(spec["tolerance"])
    else:
        tol = abs(expected) * float(spec.get("relative_tolerance", REL_TOL)) if unit != "ratio" else RATIO_TOL
    tol = max(tol, 1e-9)
    pool = list(candidates)
    if unit == "ratio":
        pool += [c / 100.0 for c in candidates]        # "51" or 51.0 in a table for a ratio of 0.51
    return any(abs(c - expected) <= tol for c in pool)


def _answer_text(answer) -> str:
    return " ".join(t for t in (answer.answer_text, answer.insight) if t)


def _table_rows(answer) -> list[list]:
    return list(answer.table.rows) if answer.table else []


def _all_text(answer) -> str:
    cells = " ".join(str(c) for row in _table_rows(answer) for c in row)
    return norm(_answer_text(answer) + " " + cells)


def _row_matches(expected_row, keys, spec, answer) -> bool:
    labels = [norm(expected_row[k]) for k in keys]
    value = float(expected_row["value"])
    for row in _table_rows(answer):
        cells = [norm(c) for c in row]
        if all(any(label == c or label in c.split(" / ") for c in cells) for label in labels):
            nums = [n for c in row for n in cell_numbers(c) if norm(c) not in labels]
            if close(value, nums, spec):
                return True
    text = _answer_text(answer)
    low = norm(text)
    for label in labels:
        if label not in low:
            return False
    # the value must be stated near the label
    raw_low = text.lower()
    for m in re.finditer(re.escape(labels[0]).replace(r"\ ", r"[\s_\-]+"), raw_low):
        window = text[max(0, m.start() - 60): m.end() + 120]
        if close(value, numbers_in(window), spec):
            return True
    return False


def compare(expected: dict, answer) -> tuple[bool | None, str]:
    """(passed, detail). passed is None for `manual` questions."""
    kind = expected.get("kind", "manual")
    if kind == "manual":
        return None, "manual review"
    if kind == "refusal":
        ok = answer.status == "refused"
        return ok, "refused" if ok else f"not refused (status {answer.status})"
    if answer.status == "refused":
        return False, "refused a question that should be answered"
    if kind == "scalar":
        value = float(expected["value"])
        candidates = numbers_in(_answer_text(answer)) + [n for row in _table_rows(answer) for c in row for n in cell_numbers(c)]
        ok = close(value, candidates, expected)
        return ok, f"expected {value:g}" + ("" if ok else f"; numbers in the answer: {[round(c, 4) for c in candidates[:8]]}")
    if kind == "table":
        keys = expected["key"]
        results = [(r, _row_matches(r, keys, expected, answer)) for r in expected["rows"]]
        matched = sum(1 for _, ok in results if ok)
        if expected.get("match") == "any":
            return matched >= 1, f"{matched} of {len(results)} acceptable rows found (any one is enough)"
        missing = [" / ".join(str(r[k]) for k in keys) + f" = {r['value']:g}" for r, ok in results if not ok]
        return matched == len(results), f"{matched} of {len(results)} rows match" + (f"; missing: {missing[:5]}" if missing else "")
    if kind in ("set", "contains"):
        wanted = expected.get("value") or []
        text = _all_text(answer)
        missing = [w for w in wanted if norm(w) not in text]
        return not missing, "all found" if not missing else f"missing: {missing}"
    return None, f"unknown expected kind '{kind}'"
