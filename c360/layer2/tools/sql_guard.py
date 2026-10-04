"""SQL validator for the agent's queries: parse, check, rewrite. Nothing reaches the serving
database without passing through validate().

Hard rules (reject, with a message the model can act on):
  R1  exactly one statement, and it is a SELECT (CTEs and set operations allowed)
  R2  every table is in schema `l2` (unqualified known names are rewritten to l2.<name>)
  R3  no table functions, file functions or system functions
  R4  no SELECT * (or whole-row selection) on row-level tables
  R5  free text (note_text, transcript_text) cannot be selected; it is read through search_text
  R6  no deny-listed (personal or protected) column names

Trap rules (TRAPS): mistakes that produce a plausible but wrong number on this data.
  T1  contact_history without the duplicate filter           reject
  T2  current_date / now() instead of the snapshot date      reject
  T3  *_recomputed columns outside data-quality questions    reject
  T4  model_scores without the superseded-duplicate filter   reject
  T5  count(*) across a join of tables at different grains   warning

Rewrite: LIMIT <max_rows + 1> is added (or lowered) so truncation can be detected.

This is defence in depth, not the only defence: the connection itself is read-only with
external access disabled, and the serving database contains no protected or contact columns.
"""
import re
from dataclasses import dataclass, field
from typing import NamedTuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from c360.layer2 import deny_list

SCHEMA = "l2"
TEXT_COLUMNS = {"note_text", "transcript_text"}

# Statement and clause types that are never allowed anywhere in the tree.
_FORBIDDEN_NODES = tuple(getattr(exp, n) for n in (
    "Insert", "Update", "Delete", "Create", "Drop", "Alter", "Command", "Copy", "Attach", "Detach", "Pragma",
    "Set", "Use", "Describe", "Show", "Into", "Merge", "TruncateTable", "Install", "Summarize", "Pivot",
    "LoadData", "Export", "Transaction", "Commit", "Rollback", "Grant", "Analyze", "Comment", "Cache",
) if hasattr(exp, n))

# Function names (anonymous or typed) that touch files, the catalog, settings or the system.
_DENIED_FUNCTION = re.compile(
    r"^(read_|scan_|sniff_|parquet_|json_execute|from_json_file|glob$|query$|query_table$|pragma_|duckdb_|"
    r"getenv$|getvariable$|current_setting$|current_database$|current_catalog$|current_schema$|current_schemas$|"
    r"current_user$|current_role$|session_user$|user$|version$|current_version$|which_secret|"
    r"copy|export|import|load|install|attach|detach|system|shell|http|sql_auto_complete|checkpoint|force_checkpoint|"
    r"columns$|in_search_path$|pg_|has_|table_info|storage_info|database_size|show|summary$|test_|"
    r"sqlite_|postgres_|mysql_|iceberg_|delta_|icu_|enable_|disable_|set_|add_parquet_key|create_fts_index|drop_fts_index|"
    r"uuid$|gen_random_uuid$|rand$|random$|setseed$|nextval$|currval$|txid_current$|row_to_json$|to_json$|alias$|stats$)",
    re.IGNORECASE)

# Clock functions: a relative date must be anchored at the snapshot date (trap T2).
_CLOCK_CLASSES = tuple(getattr(exp, n) for n in ("CurrentDate", "CurrentTimestamp", "CurrentTime", "CurrentDatetime",
                                                 "Localtimestamp", "Localtime") if hasattr(exp, n))
_CLOCK_NAMES = {"now", "today", "get_current_timestamp", "get_current_time", "current_localtimestamp",
                "current_localtime", "transaction_timestamp", "statement_timestamp", "clock_timestamp",
                "current_date", "current_timestamp", "current_time", "localtimestamp", "localtime", "sysdate", "getdate"}

# Functions sqlglot has no class for. Anything not listed is rejected.
_SAFE_ANONYMOUS = {
    "list_position", "list_indexof", "array_position", "list_contains", "list_has", "list_extract", "list_element",
    "list_distinct", "list_sort", "list_reverse", "list_count", "list_unique", "list_sum", "list_avg", "list_min",
    "list_max", "list_concat", "list_slice", "list_aggregate", "len", "length", "array_length", "cardinality",
    "date_part", "datepart", "date_sub", "datesub", "date_diff", "datediff", "date_add", "dateadd", "date_trunc",
    "datetrunc", "age", "era", "epoch", "epoch_ms", "julian", "dayofyear", "dayofmonth", "isodow", "isoyear",
    "weekday", "yearweek", "hour", "minute", "second", "make_date", "make_timestamp", "to_date", "to_days",
    "to_months", "to_years", "to_weeks", "strptime", "try_strptime", "last_day", "monthname", "dayname",
    "quantile", "quantile_cont", "quantile_disc", "percentile_disc", "percentile_cont", "median", "mad", "mode",
    "histogram", "kurtosis", "skewness", "entropy", "var_samp", "var_pop", "variance", "stddev_samp", "stddev_pop",
    "covar_pop", "covar_samp", "regr_slope", "regr_intercept", "regr_r2", "arg_max", "arg_min", "max_by", "min_by",
    "argmax", "argmin", "bool_and", "bool_or", "bit_and", "bit_or", "string_agg", "listagg", "product", "favg",
    "fsum", "sumkahan", "count_star", "approx_quantile", "reservoir_quantile", "first_value", "last_value",
    "nth_value", "cume_dist", "dense_rank", "rank", "lead", "lag", "bin", "hex", "ascii", "chr", "format",
    "printf", "strlen", "bit_length", "instr", "strpos", "position", "prefix", "suffix", "ends_with", "lpad", "rpad",
    "ltrim", "rtrim", "reverse", "repeat", "translate", "string_split", "str_split", "regexp_extract",
    "regexp_extract_all", "regexp_replace", "regexp_matches", "regexp_full_match", "levenshtein", "jaccard",
    "jaro_winkler_similarity", "strip_accents", "nfc_normalize", "lcase", "ucase", "trunc", "truncate", "cbrt",
    "log2", "log10", "degrees", "radians", "pi", "even", "gcd", "lcm", "fdiv", "fmod", "divide", "multiply",
    "add", "subtract", "isfinite", "isinf", "ifnull", "nvl", "nullif", "least", "greatest", "typeof", "hash",
    "md5", "sha256", "range", "generate_series", "struct_extract", "map_extract", "element_at", "constant_or_null",
    "error", "equi_width_bins", "to_timestamp", "timezone", "date_format",
}


@dataclass(frozen=True)
class Issue:
    rule: str
    severity: str        # "error" | "warning"
    message: str


class GuardResult(NamedTuple):
    ok: bool
    sql: str | None
    issues: list


@dataclass
class GuardContext:
    """What the validator knows about the serving database."""
    tables: set = field(default_factory=set)                 # L2 table names
    columns: set = field(default_factory=set)                # every L2 column name
    row_level: set = field(default_factory=set)              # tables where SELECT * is refused
    keys: dict = field(default_factory=dict)                 # table -> grain key (for T5)
    snapshot_date: str = "2026-09-28"
    max_rows: int = 50

    @classmethod
    def from_serving(cls, serving, semantic=None, max_rows=50):
        semantic = semantic or {}
        spec = semantic.get("tables") or {}
        tables = set(serving.tables)
        return cls(tables=tables,
                   columns={c for cols in serving.columns.values() for c in cols},
                   # a table the semantic layer does not describe is treated as row-level (the safe default)
                   row_level={t for t in tables if (spec.get(t) or {}).get("row_level", True)},
                   keys={t: (spec.get(t) or {}).get("key", t) for t in tables},
                   snapshot_date=str(serving.snapshot_date), max_rows=max_rows)


@dataclass
class _Facts:
    """What a query touches; shared by the trap detectors."""
    tree: exp.Expression
    purpose: str
    ctx: GuardContext
    l2_tables: set
    column_names: set


def _t1(f):
    return "contact_history" in f.l2_tables and not ({"is_duplicate", "dq_duplicate_of"} & f.column_names)


def _t2(f):
    for node in f.tree.find_all(exp.Func):
        if isinstance(node, _CLOCK_CLASSES):
            return True
        if isinstance(node, exp.Anonymous) and node.name.lower() in _CLOCK_NAMES:
            return True
    return any(c.name.lower() in _CLOCK_NAMES and not c.table for c in f.tree.find_all(exp.Column))


def _t3(f):
    return f.purpose != "data_quality" and any(n.endswith("_recomputed") for n in f.column_names)


def _t4(f):
    return "model_scores" in f.l2_tables and "dq_superseded_duplicate" not in f.column_names


def _t5(f):
    for select in f.tree.find_all(exp.Select):
        direct = {t.name.lower() for t in _direct_tables(select) if t.name.lower() in f.ctx.tables}
        if len({f.ctx.keys.get(t, t) for t in direct}) < 2:
            continue
        for count in select.find_all(exp.Count):
            if isinstance(count.this, exp.Star) and count.find_ancestor(exp.Select) is select:
                return True
    return False


@dataclass(frozen=True)
class Trap:
    id: str
    severity: str
    detector: object
    message: str


TRAPS = [
    Trap("T1", "error", _t1,
         "contact_history contains double-loaded contacts: exclude them with `is_duplicate = false` "
         "(only keep them when the question is about duplicates)."),
    Trap("T2", "error", _t2,
         "Do not use current_date, now() or today(): the data is a snapshot. Use the snapshot date "
         "`DATE '{snapshot}'` for relative dates."),
    Trap("T3", "error", _t3,
         "Reported figures use stored buckets (dpd_bucket, current_bucket), not the *_recomputed columns. "
         "Use *_recomputed only for a data-quality question, and then pass purpose = \"data_quality\"."),
    Trap("T4", "error", _t4,
         "model_scores contains superseded duplicate scores: exclude them with `dq_superseded_duplicate = false`."),
    Trap("T5", "warning", _t5,
         "count(*) over a join of tables at different grains can double-count: consider count(DISTINCT <id>)."),
]


def _direct_tables(select):
    """Tables named directly in a SELECT's FROM and JOINs (not inside its subqueries)."""
    out = []
    sources = []
    from_ = select.args.get("from") or select.args.get("from_")
    if from_ is not None:
        sources.append(from_.this)
    for join in select.args.get("joins") or []:
        sources.append(join.this)
    for s in sources:
        if isinstance(s, exp.Table):
            out.append(s)
    return out


def _is_star(e):
    if isinstance(e, exp.Alias):
        e = e.this
    return isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))


def _function_name(node):
    if isinstance(node, exp.Anonymous):
        return node.name.lower()
    try:
        return node.sql_name().lower()
    except Exception:       # noqa: BLE001
        return type(node).__name__.lower()


def _text_column_exposed(col):
    """True unless the text column is used only inside a WHERE / FILTER / JOIN ... ON predicate."""
    node = col.parent
    while node is not None:
        if isinstance(node, (exp.Where, exp.Join)):
            return False
        if isinstance(node, (exp.Select, exp.Order, exp.Group, exp.Having, exp.Qualify, exp.Window)):
            return True
        node = node.parent
    return True


def validate(sql, ctx: GuardContext, purpose="analysis") -> GuardResult:
    """(ok, rewritten_sql, issues). `issues` holds errors (ok = False) and warnings (ok may be True)."""
    issues: list[Issue] = []

    def reject(rule, message):
        issues.append(Issue(rule, "error", message))

    if not isinstance(sql, str) or not sql.strip():
        return GuardResult(False, None, [Issue("R1", "error", "The query is empty.")])
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except (SqlglotError, RecursionError, ValueError) as e:
        return GuardResult(False, None, [Issue("R1", "error", "The query could not be parsed as DuckDB SQL: "
                                               + str(e).splitlines()[0][:200])])
    if len(statements) != 1:
        return GuardResult(False, None, [Issue("R1", "error", "Send exactly one SELECT statement (no semicolon-separated statements).")])
    tree = statements[0]
    if isinstance(tree, exp.Subquery) and not tree.alias:
        tree = tree.unnest()
    if not isinstance(tree, (exp.Select, exp.SetOperation)):
        return GuardResult(False, None, [Issue("R1", "error", f"Only SELECT queries are allowed (got {type(tree).__name__.upper()}). "
                                               "The database is read-only.")])
    forbidden = sorted({type(n).__name__.upper() for n in tree.find_all(*_FORBIDDEN_NODES)})
    if forbidden:
        return GuardResult(False, None, [Issue("R1", "error", "Only plain SELECT queries are allowed; remove: " + ", ".join(forbidden) + ".")])

    known = ", ".join(sorted(ctx.tables))
    cte_names = {c.alias.lower() for c in tree.find_all(exp.CTE) if c.alias}
    for name in sorted(cte_names & ctx.tables):
        reject("R2", f"A CTE cannot be named `{name}` because that is a table name. Pick another name.")

    # R2 / R3: tables
    l2_tables, alias_names = set(), set()
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            fn = _function_name(table.this) if isinstance(table.this, exp.Func) else type(table.this).__name__
            reject("R3", f"Table functions are not allowed (`{fn}`). Query the l2 tables only.")
            continue
        name, db = table.name.lower(), (table.db or "").lower()
        if table.alias:
            alias_names.add(table.alias.lower())
        if table.catalog:
            reject("R2", f"`{table.sql(dialect='duckdb')}`: only tables in schema `l2` can be queried. Available: {known}.")
        elif db:
            if db != SCHEMA:
                reject("R2", f"`{db}.{name}` is not available: only schema `l2` can be queried. Available tables: {known}.")
            elif name not in ctx.tables:
                reject("R2", f"There is no table `l2.{name}`. Available tables: {known}.")
            else:
                l2_tables.add(name)
                alias_names.add(name)
        elif name in cte_names:
            continue
        elif name in ctx.tables:
            table.set("db", exp.to_identifier(SCHEMA))
            l2_tables.add(name)
            alias_names.add(name)
        else:
            reject("R2", f"Unknown table `{name}`. Available tables (schema l2): {known}.")

    # R3: functions
    for node in tree.find_all(exp.Func):
        name = _function_name(node)
        if isinstance(node, _CLOCK_CLASSES) or name in _CLOCK_NAMES:
            continue    # reported by trap T2 with a better message
        if _DENIED_FUNCTION.search(name):
            reject("R3", f"Function `{name}` is not allowed (file, system and catalog functions are blocked).")
        elif isinstance(node, exp.Anonymous) and name not in _SAFE_ANONYMOUS:
            reject("R3", f"Function `{name}` is not on the allowed list. Use standard scalar, aggregate or window functions.")

    # R4: SELECT * / whole-row selection on row-level tables
    for select in tree.find_all(exp.Select):
        if any(_is_star(e) for e in select.expressions):
            hit = sorted(t.name.lower() for t in _direct_tables(select) if t.name.lower() in ctx.row_level and t.name.lower() not in cte_names)
            if hit:
                reject("R4", f"SELECT * is not allowed on {', '.join('l2.' + h for h in hit)}: list the columns you need, or aggregate.")
    column_names = set()
    select_aliases = {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}
    for col in tree.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            continue
        name = col.name.lower()
        column_names.add(name)
        refers_to_output = name in select_aliases and col.find_ancestor(exp.Order, exp.Group, exp.Having, exp.Qualify) is not None
        if not col.table and name in alias_names and name not in ctx.columns and not refers_to_output:
            reject("R4", f"`{name}` is a table, not a column: selecting a whole row is not allowed. List the columns you need.")
        # R5: free text
        if name in TEXT_COLUMNS and _text_column_exposed(col):
            reject("R5", f"`{name}` cannot be selected, grouped or sorted in SQL. Use the search_text tool to read notes and "
                         "transcripts (a WHERE filter on the text is allowed for counting).")
        # R6: deny-list
        if deny_list.is_denied(name):
            reject("R6", f"Column `{name}` is personal or protected data and is not available ({deny_list.reason(name)}).")

    # Trap rules
    facts = _Facts(tree, purpose, ctx, l2_tables, column_names)
    for trap in TRAPS:
        if trap.detector(facts):
            issues.append(Issue(trap.id, trap.severity, trap.message.format(snapshot=ctx.snapshot_date)))

    # de-duplicate, keep order
    seen, unique = set(), []
    for i in issues:
        if (i.rule, i.message) not in seen:
            seen.add((i.rule, i.message))
            unique.append(i)
    if any(i.severity == "error" for i in unique):
        return GuardResult(False, None, unique)

    # Rewrite: cap the number of rows returned
    cap = ctx.max_rows + 1
    limit = tree.args.get("limit")
    if limit is None:
        tree = tree.limit(cap)
    else:
        value = limit.expression
        if not (isinstance(value, exp.Literal) and not value.is_string and str(value.this).isdigit() and int(value.this) <= cap):
            tree = tree.limit(cap)
    return GuardResult(True, tree.sql(dialect="duckdb"), unique)


def error_text(issues) -> str:
    return " ".join(f"[{i.rule}] {i.message}" for i in issues if i.severity == "error")


def warning_texts(issues) -> list[str]:
    return [f"[{i.rule}] {i.message}" for i in issues if i.severity == "warning"]
