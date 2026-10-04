"""Doctor: what differs between a c360.duckdb and what Layer 2 expects.

    python -m c360.layer2.doctor

Compares Layer 1's database (and MongoDB) with schema/expected_schema.yaml and l2_views.yaml and
writes outputs/layer2/doctor_report.md and .json. Exit code 0 when there is no ERROR, 1 otherwise.
publish_l2 runs the same checks and refuses to publish while any ERROR remains.

Read-only: it attaches the source database READ_ONLY and never writes to MongoDB.
"""
import datetime as dt
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from c360.layer2 import config, deny_list, schema_util, transcripts
from c360.layer2 import publish as pub

REFERENCE_TABLES = ["metric_definitions", "benchmark_questions", "code_lookups", "hardship_programs"]
GET_CUSTOMER_FIELDS = ["identity.crm_ids", "profile", "contact", "accounts", "collections.cases", "hardship_indicators",
                       "exposure", "lineage.pipeline_version"]


@dataclass
class Item:
    level: str          # ERROR | WARNING | INFO
    check: str
    subject: str
    message: str


@dataclass
class Report:
    source_db: str = ""
    generated: str = ""
    items: list = field(default_factory=list)
    reference: dict = field(default_factory=dict)       # table -> {columns, rows}

    def add(self, level, check, subject, message):
        self.items.append(Item(level, check, subject, message))

    @property
    def errors(self):
        return [i for i in self.items if i.level == "ERROR"]

    @property
    def warnings(self):
        return [i for i in self.items if i.level == "WARNING"]

    @property
    def infos(self):
        return [i for i in self.items if i.level == "INFO"]

    def to_dict(self):
        return {"source_db": self.source_db, "generated": self.generated, "errors": len(self.errors),
                "warnings": len(self.warnings), "items": [asdict(i) for i in self.items], "reference": self.reference}

    def to_markdown(self):
        out = ["# Layer 2 doctor report", "",
               f"Source database: `{self.source_db}`  ", f"Generated: {self.generated}  ",
               f"**{len(self.errors)} error(s), {len(self.warnings)} warning(s), {len(self.infos)} note(s).** "
               + ("publish_l2 will refuse to run until the errors are fixed." if self.errors else "publish_l2 can run."), ""]
        for level, title, rows in (("ERROR", "Errors (must fix)", self.errors), ("WARNING", "Warnings (explain or fix)", self.warnings),
                                   ("INFO", "Notes", self.infos)):
            out += [f"## {title}", ""]
            if not rows:
                out += ["None.", ""]
                continue
            out += ["| Check | Subject | Finding |", "|---|---|---|"]
            out += [f"| {i.check} | `{i.subject}` | {i.message.replace('|', '/')} |" for i in rows]
            out.append("")
        if self.reference:
            out += ["## Reference tables (first rows; reference data only)", ""]
            for table, info in self.reference.items():
                out += [f"### curated.{table}", "", "Columns: " + ", ".join(f"`{c}`" for c in info["columns"]), ""]
                if info["rows"]:
                    out += ["| " + " | ".join(info["columns"]) + " |", "|" + "---|" * len(info["columns"])]
                    out += ["| " + " | ".join(str(v).replace("|", "/")[:120] for v in row) + " |" for row in info["rows"]]
                    out.append("")
        return "\n".join(out) + "\n"


def _source_catalog(con):
    rows = con.execute("""SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns
                          WHERE table_catalog = 'src'""").fetchall()
    catalog: dict = {}
    for schema, table, column, dtype in rows:
        catalog.setdefault((schema, table), {})[column] = dtype
    return catalog


def _usage(views):
    """Which source columns the allow-list needs: (schema, table, column) -> list of (view, l2 column, hard?)."""
    usage: dict = {}
    for name, view in views.items():
        if view.get("kind") == "mongo":
            continue
        aliases = pub.source_aliases(view)
        hard_table = bool(view.get("required"))
        join_text = (view.get("source") or "").replace("{cols}", "")
        for key in pub.referenced_columns(join_text, aliases):
            usage.setdefault(key, []).append((name, "(join)", hard_table))
        for col, expr, optional in pub.column_items(view):
            for key in pub.referenced_columns(expr, aliases):
                usage.setdefault(key, []).append((name, col, hard_table and not optional))
    return usage


def check(con=None, views=None, expected=None, mongo=None, source_db=None, data_root=None) -> Report:
    """Run every check. `con` may be a connection that already has the source attached as `src`."""
    import duckdb

    s = config.settings()
    source_db = Path(source_db) if source_db else s.source_db
    data_root = Path(data_root) if data_root else s.data_root
    views = views if views is not None else pub.load_views()
    expected = expected if expected is not None else schema_util.load_expected()
    report = Report(source_db=str(source_db), generated=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # 1. source opens read-only; schemas exist
    own = con is None
    if own:
        if not source_db.exists():
            report.add("ERROR", "source", str(source_db), "Layer 1 database not found. Run Layer 1 (or the synthetic generator) first.")
            return report
        con = duckdb.connect(":memory:")
        try:
            con.execute(f"ATTACH '{source_db}' AS src (READ_ONLY)")
        except Exception as e:      # noqa: BLE001
            report.add("ERROR", "source", str(source_db), f"cannot open read-only: {str(e).splitlines()[0]}")
            return report
        pub.create_file_text_tables(con, views, data_root, read_files=False)
    try:
        _run_checks(con, report, views, expected, mongo, data_root)
    finally:
        if own:
            con.close()
    return report


def _run_checks(con, report, views, expected, mongo, data_root):
    schemas = {r[0] for r in con.execute("SELECT schema_name FROM information_schema.schemata WHERE catalog_name = 'src'").fetchall()}
    for schema in ("curated", "golden"):
        if schema not in schemas:
            report.add("ERROR", "source", f"src.{schema}", "schema missing: Layer 1 has not been built in this database")
    report.add("INFO", "source", "src", "opened read-only; schemas found: " + ", ".join(sorted(schemas - {"information_schema", "pg_catalog", "main"})))
    catalog = _source_catalog(con)
    usage = _usage(views)

    # 2. expected tables and columns
    mentioned = set()
    for schema, tables in expected.items():
        for table, spec in tables.items():
            cols = catalog.get((schema, table))
            needed_hard = any(hard for (sc, tb, _), uses in usage.items() if (sc, tb) == (schema, table) for _, _, hard in uses)
            if cols is None:
                level = "ERROR" if needed_hard else "WARNING" if spec.get("confidence") == "confirmed" else "INFO"
                report.add(level, "schema", f"{schema}.{table}", "table missing"
                           + (" (required by the serving database)" if needed_hard else " (not required by the serving database)"))
                continue
            try:
                n = con.execute(f'SELECT count(*) FROM src.{schema}."{table}"').fetchone()[0]
                report.add("INFO", "schema", f"{schema}.{table}", f"{n:,} rows, {len(cols)} columns")
            except Exception as e:      # noqa: BLE001
                report.add("WARNING", "schema", f"{schema}.{table}", f"cannot count rows: {str(e).splitlines()[0]}")
            for part in schema_util.PARTS:
                for column, typ in (spec.get(part) or {}).items():
                    mentioned.add((schema, table, column))
                    uses = usage.get((schema, table, column), [])
                    if column not in cols:
                        hard = any(h for _, _, h in uses)
                        used_by = ", ".join(sorted({f"l2.{v}.{c}" for v, c, _ in uses})) or "nothing in l2_views.yaml"
                        if hard:
                            level = "ERROR"
                        elif part == "confirmed" or uses:
                            level = "WARNING"
                        else:
                            level = "INFO"
                        report.add(level, "schema", f"{schema}.{table}.{column}", f"{part} column missing (used by: {used_by})")
                    elif not schema_util.compatible(typ, cols[column]):
                        report.add("WARNING", "schema", f"{schema}.{table}.{column}", f"type is {cols[column]}, expected {typ}")

    # 3. every serving table binds
    for name, view in views.items():
        if view.get("kind") == "mongo":
            continue
        plan = pub.resolve_view(con, name, view)
        required = bool(view.get("required"))
        if plan.table_error:
            report.add("ERROR" if required else "WARNING", "views", f"l2.{name}",
                       f"source does not bind: {plan.table_error}" + ("" if required else " (optional table: it will be skipped)"))
            continue
        for col, expr, err in plan.failed:
            report.add("ERROR" if required else "WARNING", "views", f"l2.{name}.{col}", f"source expression `{expr}` does not bind: {err}")
        for col, expr, err in plan.skipped:
            report.add("WARNING", "views", f"l2.{name}.{col}", f"optional column skipped, source expression `{expr}` does not bind: {err}")
        if not plan.failed and not plan.skipped:
            report.add("INFO", "views", f"l2.{name}", f"binds ({len(plan.columns)} columns)")

    # 4. unexpected columns and tables (never auto-added)
    for (schema, table), cols in sorted(catalog.items()):
        if schema not in expected:
            continue
        if table not in expected[schema]:
            if not table.startswith("_"):
                report.add("INFO", "unexpected", f"{schema}.{table}", f"table not in expected_schema.yaml ({len(cols)} columns): "
                           + ", ".join(sorted(cols)))
            continue
        extra = sorted(c for c in cols if (schema, table, c) not in mentioned and not c.startswith("_"))
        if extra:
            flagged = [f"{c} (deny-listed)" if deny_list.is_denied(c) else c for c in extra]
            report.add("INFO", "unexpected", f"{schema}.{table}", "columns not mentioned anywhere (decide whether to allow-list): " + ", ".join(flagged))

    # 5. reference tables: columns and first rows (reference data only, never customer data)
    for table in REFERENCE_TABLES:
        cols = catalog.get(("curated", table))
        if cols is None:
            report.add("WARNING", "reference", f"curated.{table}", "reference table missing")
            continue
        try:
            cur = con.execute(f"SELECT * FROM src.curated.{table} LIMIT 3")
            names = [d[0] for d in cur.description]
            report.reference[table] = {"columns": names, "rows": [[str(v) for v in row] for row in cur.fetchall()]}
            report.add("INFO", "reference", f"curated.{table}", "columns: " + ", ".join(names))
        except Exception as e:      # noqa: BLE001
            report.add("WARNING", "reference", f"curated.{table}", f"cannot read: {str(e).splitlines()[0]}")

    # 6. transcript files
    for name, view in views.items():
        ft = view.get("file_text")
        if not ft:
            continue
        try:
            paths = [r[0] for r in con.execute(f"SELECT {ft['path_column']} FROM ({ft['query']}) LIMIT 20").fetchall()]
        except Exception as e:      # noqa: BLE001
            report.add("ERROR" if view.get("required") else "WARNING", "transcripts", f"l2.{name}",
                       f"cannot list transcript files: {str(e).splitlines()[0]}")
            continue
        base_dirs = ft.get("base_dirs") or ("",)
        located = [transcripts.resolve(data_root, p, base_dirs) for p in paths if p]
        existing = [f for f in located if f.exists()]
        keys = next((k for k in (transcripts.top_level_keys(f) for f in existing) if k), None)
        readable = sum(1 for f in existing if transcripts.parse(f))
        msg = (f"{len(existing)} of {len(paths)} sampled files exist under {data_root} "
               f"(folders tried: {', '.join(b or '.' for b in base_dirs)}); {readable} give text"
               + (f"; top-level JSON keys of the first readable file: {', '.join(keys)}" if keys else ""))
        report.add("WARNING" if paths and not readable else "INFO", "transcripts", f"l2.{name}",
                   msg + ("" if readable or not paths else ". Check MAPLE_DATA and transcripts.parse()."))

    # 7. MongoDB
    try:
        mongo = mongo if mongo is not None else config.mongo_db()
        n = mongo.customers.estimated_document_count()
        doc = mongo.customers.find_one({})
        if not doc:
            report.add("ERROR", "mongo", "customers", "collection is empty: run Layer 1's publish step")
        else:
            report.add("INFO", "mongo", "customers", f"reachable, {n:,} documents")
            for name, view in views.items():
                if view.get("kind") != "mongo":
                    continue
                for col, spec in view["columns"].items():
                    if deny_list.is_denied(spec["path"].split(".")[-1]) and spec["path"] != "_id":
                        report.add("ERROR", "deny_list", f"l2.{name}.{col}", f"projects deny-listed field `{spec['path']}`")
                    if pub._dig(doc, spec["path"]) is None and spec["path"] not in ("_id",):
                        report.add("WARNING", "mongo", f"l2.{name}.{col}", f"field `{spec['path']}` missing or null in the sampled document")
            for path in GET_CUSTOMER_FIELDS:
                if pub._dig(doc, path) is None:
                    report.add("WARNING", "mongo", f"customers.{path}", "field used by get_customer is missing in the sampled document")
    except Exception as e:      # noqa: BLE001
        report.add("ERROR", "mongo", "customers", f"MongoDB not reachable ({type(e).__name__}: {str(e).splitlines()[0][:160]})")

    # 8. full-text extension
    try:
        import duckdb
        probe = duckdb.connect(":memory:")
        probe.execute("INSTALL fts")
        probe.execute("LOAD fts")
        probe.close()
        report.add("INFO", "text_search", "fts", "full-text extension available: text_search_mode will be `fts`")
    except Exception as e:      # noqa: BLE001
        report.add("WARNING", "text_search", "fts", f"full-text extension unavailable ({str(e).splitlines()[0][:120]}): keyword (`like`) search will be used")

    # 9. deny-list: nothing personal or protected may be allow-listed, under any name
    for name, view in views.items():
        if view.get("kind") == "mongo":
            for col in view["columns"]:
                if deny_list.is_denied(col):
                    report.add("ERROR", "deny_list", f"l2.{name}.{col}", f"allow-listed column is deny-listed ({deny_list.reason(col)})")
            continue
        aliases = pub.source_aliases(view)
        for col, expr, _ in pub.column_items(view):
            if deny_list.is_denied(col):
                report.add("ERROR", "deny_list", f"l2.{name}.{col}", f"allow-listed column is deny-listed ({deny_list.reason(col)})")
            for schema, table, source_col in sorted(pub.referenced_columns(expr, aliases)):
                if deny_list.is_denied(source_col):
                    report.add("ERROR", "deny_list", f"l2.{name}.{col}",
                               f"source expression `{expr}` reads deny-listed column {schema}.{table}.{source_col} ({deny_list.reason(source_col)})")


def write_report(report, out_dir=None):
    out_dir = Path(out_dir) if out_dir else config.settings().out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    md, js = out_dir / "doctor_report.md", out_dir / "doctor_report.json"
    md.write_text(report.to_markdown())
    js.write_text(json.dumps(report.to_dict(), indent=2, default=str))
    return md, js


def main():
    report = check()
    md, _ = write_report(report)
    print(f"[doctor] {len(report.errors)} error(s), {len(report.warnings)} warning(s), {len(report.infos)} note(s) -> {md}")
    for item in report.errors + report.warnings:
        print(f"  {item.level:<7} {item.subject}: {item.message}")
    sys.exit(1 if report.errors else 0)


if __name__ == "__main__":
    main()
