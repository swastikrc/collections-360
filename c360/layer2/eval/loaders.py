"""Load question sets (YAML) and the bank's benchmark_questions table.

Question files live in eval/questions/. An entry's `expected` may hold a literal answer, or a
`truth: {fn, args}` reference that is resolved against the synthetic world (synth/truth.py).
Paraphrases point at a synthetic question with `of:` and inherit its expected answer.
The live agent only ever receives `question`.
"""
import json
import re
from pathlib import Path

import yaml

from c360.layer2 import config

QUESTION_COLUMNS = ("question", "question_text", "prompt", "text")
ANSWER_COLUMNS = ("expected_answer", "answer", "gold_answer", "expected")
SQL_COLUMNS = ("gold_sql", "sql", "expected_sql", "gold_sql_reference")
SOURCE_COLUMNS = ("gold_sources", "sources")
TOLERANCE_COLUMNS = ("tolerance",)
ID_COLUMNS = ("question_id", "id", "qid")
CATEGORY_COLUMNS = ("category", "type", "route", "tag")
SETS = ("synthetic", "heldout", "paraphrase", "refusal", "benchmark")


def _synthetic_truth():
    """Truth for the synthetic world, or None when the warehouse is not synthetic (the real machine)."""
    s = config.settings()
    if not (s.warehouse / "SYNTHETIC").exists() or not s.source_db.exists():
        return None
    from c360.layer2.synth.truth import Truth
    return Truth.from_db(s.source_db)


def _resolve(entry, truth):
    """Fill `expected` from a truth reference; mark the entry skipped when that is not possible."""
    from c360.layer2.synth import questions as q
    expected = dict(entry.get("expected") or {})
    subject = None
    if entry.get("subject"):
        if truth is None:
            entry["skip"] = "needs the synthetic world"
            return entry
        subject = truth.resolve(entry["subject"]["fn"], entry["subject"].get("args"))
        entry["question"] = entry["question"].format(**subject)
        for step in entry.get("fake_plan") or []:
            step["args"] = {k: (v.format(**subject) if isinstance(v, str) else v) for k, v in (step.get("args") or {}).items()}
    if "fields" in expected:
        expected = {"kind": expected.get("kind", "contains"), "value": [str(subject[f]) for f in expected["fields"]]}
    ref = expected.pop("truth", None) or (entry.get("truth") if "value" not in expected and "rows" not in expected else None)
    if ref and "value" not in expected and "rows" not in expected:
        if truth is None:
            entry["skip"] = "needs the synthetic world"
            return entry
        spec = {"kind": expected.get("kind", "scalar"), "truth": ref, "unit": expected.get("unit", "count"),
                "key": (expected.get("key") or [None])[0], "top": expected.get("top")}
        resolved = q.expected_for(truth, spec)
        for k in ("tolerance", "relative_tolerance"):
            if k in expected:
                resolved.pop("tolerance", None)
                resolved.pop("relative_tolerance", None)
                resolved[k] = expected[k]
        expected = resolved
    entry["expected"] = expected
    return entry


def load_set(name, questions_dir=None, synthetic_path=None) -> list[dict]:
    """Questions of a YAML set, each with id, question, expected, tags (and optionally fake_plan, skip)."""
    questions_dir = Path(questions_dir) if questions_dir else config.QUESTIONS_DIR
    synthetic_path = Path(synthetic_path) if synthetic_path else config.QUESTIONS_DIR / "synthetic.yaml"
    path = synthetic_path if name == "synthetic" else questions_dir / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"question set '{name}' not found at {path}"
                                + (" (run `python -m c360.layer2.synth` to generate it)" if name == "synthetic" else ""))
    entries = yaml.safe_load(path.read_text()) or []
    needs_truth = any(e.get("subject") or (e.get("expected") or {}).get("truth") or (e.get("expected") or {}).get("fields") for e in entries)
    truth = _synthetic_truth() if needs_truth else None
    base = {}
    if any(e.get("of") for e in entries):
        if synthetic_path.exists():
            base = {e["id"]: e for e in yaml.safe_load(synthetic_path.read_text()) or []}
    out = []
    for e in entries:
        e = dict(e)
        e.setdefault("tags", [])
        if e.get("of"):
            src = base.get(e["of"])
            if src is None:
                e["skip"] = f"base question {e['of']} not found (generate synthetic.yaml first)"
                e.setdefault("expected", {"kind": "manual"})
            else:
                e["expected"] = src["expected"]
                e.setdefault("fake_plan", src.get("fake_plan"))
                e["tags"] = list(dict.fromkeys(list(e["tags"]) + list(src.get("tags", []))))
            e["group"] = e["of"]
        else:
            e = _resolve(e, truth)
        e.setdefault("expected", {"kind": "manual"})
        out.append(e)
    return out


# ---- the bank's benchmark ------------------------------------------------------------------------
def _first(columns, candidates):
    lower = {c.lower(): c for c in columns}
    return next((lower[c] for c in candidates if c in lower), None)


def parse_tolerance(text):
    """'±0.5 pts' -> ('pts', 0.5); '±0.1 days' -> ('abs', 0.1); '±1 CAD' -> ('abs', 1.0); 'exact' -> ('abs', 0.0).
    None when the text gives no number to compare against (e.g. 'direction of effect')."""
    t = str(text or "").strip().lower()
    if not t:
        return None
    if t.startswith("exact") and "±" not in t:
        return ("abs", 0.0)
    m = re.search(r"±\s*(\d+(?:\.\d+)?)\s*(pts?|points?)?", t)
    if not m:
        return None
    return ("pts" if m.group(2) else "abs", float(m.group(1)))


def _num(cell):
    try:
        return float(str(cell).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _apply_tolerance(expected, column, tolerance):
    """Percent columns (`*_pct`) are compared as ratios; the question's tolerance becomes absolute."""
    pct = column.lower().endswith(("_pct", "_percent")) or column.lower() in ("pct", "percent")
    if pct:
        if expected["kind"] == "scalar":
            expected["value"] = expected["value"] / 100.0
        else:
            for r in expected["rows"]:
                r["value"] = r["value"] / 100.0
        expected["unit"] = "ratio"
    if tolerance:
        _, amount = tolerance
        # "±0.5 pts" on a percent column is 0.005 as a ratio; any other tolerance is in the column's own unit
        expected["tolerance"] = max(amount / 100.0 if pct else amount, 1e-9)
    return expected


def _csv_answer(t, tolerance):
    """A gold answer written as a small CSV (header + rows). First column = key, first numeric column after it = value."""
    import csv
    import io
    rows = [r for r in csv.reader(io.StringIO(t)) if r]
    header, body = rows[0], rows[1:]
    if not body:
        return None
    if len(header) == 1:
        v = _num(body[0][0])
        if v is None or len(body) != 1:
            return {"kind": "set", "value": [r[0] for r in body]}
        return _apply_tolerance({"kind": "scalar", "value": v}, header[0], tolerance)
    value_idx = next((i for i in range(1, len(header)) if all(_num(r[i]) is not None for r in body if len(r) > i)), None)
    if value_idx is None:
        return None
    exp = {"kind": "table", "key": [header[0]], "value_column": header[value_idx],
           "rows": [{header[0]: r[0], "value": _num(r[value_idx])} for r in body if len(r) > value_idx]}
    return _apply_tolerance(exp, header[value_idx], tolerance)


def parse_expected_answer(text, tolerance=None):
    """Best effort: a number, a list, a small table or a refusal from free text; otherwise manual scoring."""
    if text is None or str(text).strip() == "":
        return {"kind": "manual"}
    t = str(text).strip()
    if re.fullmatch(r"(?i)(refus\w*|decline\w*|not allowed|must refuse)[.!]?", t) or re.match(r"(?i)refuse\s*:", t):
        return {"kind": "refusal"}
    if "\n" in t and "," in t.splitlines()[0]:
        parsed = _csv_answer(t, tolerance)
        if parsed:
            return parsed
    if "\n" in t and re.fullmatch(r"[A-Za-z_][\w ]*\n\s*-?[\d.,]+\s*", t):
        parsed = _csv_answer(t, tolerance)
        if parsed:
            return parsed
    m = re.fullmatch(r"-?\$?\s?(\d[\d,]*(?:\.\d+)?)\s?(%)?", t)
    if m:
        value = float(m.group(1).replace(",", ""))
        if t.lstrip().startswith("-"):
            value = -value
        if m.group(2):
            return {"kind": "scalar", "value": value / 100.0, "unit": "ratio", "tolerance": 0.005}
        return {"kind": "scalar", "value": value, "unit": "ratio" if 0 < value < 1 and "." in m.group(1) else "count"}
    if t[0] in "[{":
        try:
            data = json.loads(t)
            if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
                return _table_from_rows([list(r.values()) for r in data], list(data[0].keys()))
            if isinstance(data, list):
                return {"kind": "set", "value": [str(v) for v in data]}
        except ValueError:
            pass
    items = [p.strip() for p in t.split(",") if p.strip()]
    if (len(items) > 1 and len(t) < 300 and "\n" not in t and not re.search(r"[.!?;]", t)
            and all(len(i.split()) <= 4 for i in items)):        # a short list of labels, not a sentence
        return {"kind": "set", "value": items}
    return {"kind": "manual", "note": t[:300]}


def _table_from_rows(rows, columns):
    """Expected block from a gold-SQL result: 1x1 scalar, Nx1 set, NxM table (first column key, last value)."""
    if not rows:
        return {"kind": "manual", "note": "gold SQL returned no rows"}
    if len(columns) == 1:
        v = rows[0][0]
        if len(rows) == 1 and isinstance(v, (int, float)) and not isinstance(v, bool):
            return {"kind": "scalar", "value": float(v), "unit": "ratio" if isinstance(v, float) and 0 <= v <= 1 else "count"}
        return {"kind": "set", "value": [str(r[0]) for r in rows]}
    if all(isinstance(r[-1], (int, float)) and not isinstance(r[-1], bool) and r[0] is not None for r in rows) and len(rows) <= 50:
        ratio = all(isinstance(r[-1], float) and 0 <= r[-1] <= 1 for r in rows)
        return {"kind": "table", "key": [columns[0]], "unit": "ratio" if ratio else "count",
                "rows": [{columns[0]: str(r[0]), "value": float(r[-1])} for r in rows]}
    return {"kind": "manual", "note": "gold SQL result is not a simple key/value table"}


def benchmark_answers_path():
    """Expected answers kept OUTSIDE the database (the release's labels file). Harness-only: never shown to
    the agent, never copied into prompts, semantic files or code. Override with C360_L2_BENCHMARK_ANSWERS."""
    import os
    if os.environ.get("C360_L2_BENCHMARK_ANSWERS"):
        return Path(os.environ["C360_L2_BENCHMARK_ANSWERS"])
    return config.settings().data_root / "labels" / "benchmark_dev_answers.csv"


def _answers_file(path):
    import csv
    if not path or not Path(path).exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return {}
    id_col = _first(rows[0].keys(), ID_COLUMNS)
    return {str(r[id_col]).strip(): r for r in rows if id_col and r.get(id_col)}


def load_benchmark(con=None, schema="curated", answers_path=None) -> list[dict]:
    """The bank's benchmark questions from the source database (read-only). Column names are detected.

    Expected answers come from an answer column in the table if there is one, else from the labels file
    (`benchmark_answers_path()`, merged on question id). A stated answer wins; gold SQL is only run (read-only,
    never modified) when no stated answer can be parsed. Nothing here is ever shown to the agent.
    """
    import duckdb
    own = con is None
    if own:
        s = config.settings()
        if not s.source_db.exists():
            raise FileNotFoundError(f"source database not found: {s.source_db}")
        con = duckdb.connect(str(s.source_db), read_only=True)
    try:
        try:
            cur = con.execute(f"SELECT * FROM {schema}.benchmark_questions")
        except Exception as e:      # noqa: BLE001
            raise FileNotFoundError(f"{schema}.benchmark_questions is not available: {str(e).splitlines()[0]}") from e
        columns = [d[0] for d in cur.description]
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        q_col = _first(columns, QUESTION_COLUMNS)
        if q_col is None:
            raise ValueError(f"cannot find the question column in benchmark_questions (columns: {columns})")
        id_col, c_col = _first(columns, ID_COLUMNS), _first(columns, CATEGORY_COLUMNS)
        t_col, split_col = _first(columns, TOLERANCE_COLUMNS), _first(columns, ("split",))
        answers = _answers_file(answers_path if answers_path is not None else benchmark_answers_path())
        out = []
        for i, row in enumerate(rows, 1):
            qid = str(row.get(id_col) or f"B{i:02d}") if id_col else f"B{i:02d}"
            merged = {**(answers.get(qid) or {}), **{k: v for k, v in row.items() if v not in (None, "")}}
            mcols = list(merged)
            a_col, s_col, src_col = _first(mcols, ANSWER_COLUMNS), _first(mcols, SQL_COLUMNS), _first(mcols, SOURCE_COLUMNS)
            category = str(merged.get(c_col) or "").strip().lower() if c_col else ""
            tolerance = parse_tolerance(merged.get(t_col)) if t_col else None
            expected = parse_expected_answer(merged.get(a_col), tolerance) if a_col else {"kind": "manual"}
            if "refus" in category:
                expected = {"kind": "refusal"}
            gold = merged.get(s_col) if s_col else None
            if gold and str(gold).strip() and expected["kind"] == "manual" and not merged.get(a_col):
                try:
                    gcur = con.execute(str(gold))
                    gcols = [d[0] for d in gcur.description]
                    from_sql = _table_from_rows(gcur.fetchmany(51), gcols)
                    if from_sql["kind"] != "manual":
                        expected = from_sql
                except Exception as e:      # noqa: BLE001 - a gold query that does not run leaves manual scoring
                    expected.setdefault("note", f"gold SQL did not run: {str(e).splitlines()[0][:120]}")
            if merged.get(src_col) and expected["kind"] == "manual":
                expected["sources"] = str(merged[src_col])[:300]
            split = str(merged.get(split_col) or "").strip().lower() if split_col else ""
            tags = ["benchmark"] + ([category] if category else []) + ([f"split:{split}"] if split else [])
            out.append({"id": qid, "question": str(row[q_col]), "expected": expected, "tags": tags})
        return out
    finally:
        if own:
            con.close()


def load_questions_csv(path, answers_path=None) -> list[dict]:
    """Benchmark-style questions from a CSV (e.g. the extra questions released later), without rebuilding Layer 1.
    Columns are detected like load_benchmark; expected answers are merged from the labels file by id when present."""
    import duckdb
    con = duckdb.connect()
    try:
        con.execute("CREATE SCHEMA curated")
        con.execute("CREATE TABLE curated.benchmark_questions AS SELECT * FROM read_csv(?, header=true, all_varchar=true)",
                    [str(path)])
        return load_benchmark(con, answers_path=answers_path)
    finally:
        con.close()

