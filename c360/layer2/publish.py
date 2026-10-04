"""publish_l2: build the Layer 2 serving database from Layer 1's outputs.

The serving database is a separate DuckDB file holding only allow-listed, materialised columns
(schema `l2`), build metadata (schema `meta`) and a text index over notes and transcripts. The
Layer 2 agent only ever opens this file, read-only. Protected and contact columns do not exist
in it, so privacy does not depend on a prompt.

    python run_layer1.py --only publish_l2

Writes <C360_L2_DIR>/l2_<build_ts>.duckdb and switches the CURRENT pointer atomically. Layer 1's
c360.duckdb is attached read-only and never modified.
"""
import datetime as dt
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from c360.layer2 import config, deny_list, policies, transcripts

MONGO_BATCH = 20000


class PublishError(RuntimeError):
    pass


# ---- l2_views.yaml -------------------------------------------------------------------------------
def load_views(path=None) -> dict:
    return config.load_yaml(path or config.VIEWS_YAML)


def column_items(view) -> list[tuple[str, str, bool]]:
    """(l2 name, source expression, optional) for each allow-listed column of a DuckDB-sourced table."""
    items = []
    for name, spec in (view.get("columns") or {}).items():
        if isinstance(spec, dict):
            items.append((name, str(spec.get("expr", name)), bool(spec.get("optional", False))))
        else:
            items.append((name, str(spec if spec is not None else name), False))
    return items


def render(view, columns: list[tuple[str, str]]) -> str:
    """The table's SELECT with `{cols}` replaced by the allow-listed columns."""
    select_list = ", ".join(f'{expr} AS "{name}"' for name, expr in columns)
    return view["source"].replace("{cols}", select_list).strip()


@dataclass
class ViewPlan:
    name: str
    columns: list = field(default_factory=list)          # (l2 name, expr) that bind
    skipped: list = field(default_factory=list)          # (l2 name, expr, error) optional, not bound
    failed: list = field(default_factory=list)           # (l2 name, expr, error) required, not bound
    table_error: str | None = None                       # the source itself does not bind


def _bind_error(con, sql) -> str | None:
    try:
        con.execute(f"EXPLAIN {sql}")
        return None
    except Exception as e:      # noqa: BLE001 - any binder/catalog error is the answer we want
        return str(e).split("\n")[0]


def resolve_view(con, name, view) -> ViewPlan:
    """Work out which allow-listed columns exist in the attached source (`src`)."""
    plan = ViewPlan(name)
    plan.table_error = _bind_error(con, render(view, [("_probe", "1")]))
    if plan.table_error:
        return plan
    for col, expr, optional in column_items(view):
        err = _bind_error(con, render(view, [(col, expr)]))
        if err is None:
            plan.columns.append((col, expr))
        elif optional:
            plan.skipped.append((col, expr, err))
        else:
            plan.failed.append((col, expr, err))
    return plan


def create_file_text_tables(con, views, data_root, read_files=True) -> dict:
    """Temporary tables holding text parsed from files (call transcripts). Returns per-table stats."""
    stats = {}
    for name, view in views.items():
        ft = view.get("file_text")
        if not ft:
            continue
        ident, text = ft["id_column"], ft["text_column"]
        con.execute(f'CREATE OR REPLACE TEMP TABLE {ft["temp_table"]} ("{ident}" VARCHAR, "{text}" VARCHAR)')
        if not read_files:
            continue
        try:
            rows = con.execute(ft["query"]).fetchall()
        except Exception as e:      # noqa: BLE001 - reported by the doctor as a binding problem
            stats[name] = {"files": 0, "readable": 0, "error": str(e).split("\n")[0]}
            continue
        parsed, readable = [], 0
        base_dirs = ft.get("base_dirs") or ("",)
        for ident_value, rel_path in rows:
            body = transcripts.parse(transcripts.resolve(data_root, rel_path, base_dirs)) if rel_path else None
            readable += body is not None
            parsed.append((str(ident_value), body))
        if parsed:
            con.executemany(f'INSERT INTO {ft["temp_table"]} VALUES (?, ?)', parsed)
        stats[name] = {"files": len(rows), "readable": readable}
    return stats


# ---- MongoDB-sourced tables ------------------------------------------------------------------
def _dig(doc, path):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def build_mongo_table(con, name, view, mongo_db) -> int:
    import pandas as pd
    cols = view["columns"]
    con.execute(f"CREATE TABLE l2.{name} (" + ", ".join(f'"{c}" {spec["type"]}' for c, spec in cols.items()) + ")")
    select = ", ".join(f'CAST("{c}" AS {spec["type"]})' for c, spec in cols.items())
    projection = {spec["path"]: 1 for spec in cols.values()}
    total, batch = 0, []

    def flush():
        con.register("_mongo_batch", pd.DataFrame(batch, columns=list(cols), dtype=object))
        con.execute(f"INSERT INTO l2.{name} SELECT {select} FROM _mongo_batch")
        con.unregister("_mongo_batch")

    for doc in mongo_db[view.get("collection", "customers")].find({}, projection):
        row = []
        for spec in cols.values():
            v = _dig(doc, spec["path"])
            if isinstance(v, list):
                v = spec.get("join", "; ").join(str(x) for x in v)
            row.append(v)
        batch.append(row)
        if len(batch) >= MONGO_BATCH:
            flush()
            total += len(batch)
            batch = []
    if batch:
        flush()
        total += len(batch)
    return total


def _pipeline_version(mongo_db) -> str:
    try:
        doc = mongo_db.customers.find_one({}, {"lineage.pipeline_version": 1})
        return (doc or {}).get("lineage", {}).get("pipeline_version") or "unknown"
    except Exception:       # noqa: BLE001
        return "unknown"


# ---- text index ------------------------------------------------------------------------------
def build_text_index(con, views, built, text_mode=None) -> str:
    """Full-text index over the text columns. Falls back to 'like' when the extension is unavailable."""
    mode = text_mode or os.environ.get("C360_L2_TEXT_MODE") or "fts"
    if mode not in ("fts", "like"):
        raise PublishError(f"text mode must be 'fts' or 'like', got {mode!r}")
    if mode == "like":
        return "like"
    try:
        con.execute("INSTALL fts")
        con.execute("LOAD fts")
        for name, view in views.items():
            ti = view.get("text_index")
            if ti and name in built and ti["column"] in built[name]:
                con.execute(f"PRAGMA create_fts_index('l2.{name}', '{ti['id']}', '{ti['column']}', overwrite=1)")
        return "fts"
    except Exception as e:      # noqa: BLE001 - e.g. an offline machine cannot install the extension
        for name, view in views.items():
            if view.get("text_index"):
                try:
                    con.execute(f"PRAGMA drop_fts_index('l2.{name}')")
                except Exception:       # noqa: BLE001
                    pass
        print(f"  [publish_l2] full-text extension unavailable ({str(e).splitlines()[0]}); using keyword (LIKE) search")
        return "like"


def build_policy_table(con, data_root):
    """l2.policy_sections from the dataset's reference documents (current versions only). Not customer data."""
    rows, stats = policies.load_sections(data_root)
    cols = ", ".join(f'"{c}" {"INTEGER" if c == "authority" else "VARCHAR"}' for c in policies.COLUMNS)
    con.execute(f"CREATE TABLE l2.policy_sections ({cols})")
    if rows:
        con.executemany(f"INSERT INTO l2.policy_sections VALUES ({', '.join('?' * len(policies.COLUMNS))})",
                        [[r[c] for c in policies.COLUMNS] for r in rows])
    return len(rows), stats


def fts_schema(table: str) -> str:
    return f"fts_l2_{table}"


# ---- the step ------------------------------------------------------------------------------------
def _column_descriptions() -> dict:
    try:
        semantic = config.load_yaml(config.SEMANTIC_YAML)
    except FileNotFoundError:
        return {}
    out = {}
    for table, spec in (semantic.get("tables") or {}).items():
        for col, desc in (spec.get("columns") or {}).items():
            out[(table, col)] = desc if isinstance(desc, str) else (desc or {}).get("description", "")
    return out


def _cleanup(l2_dir: Path, keep: int = 2):
    files = sorted(p for p in l2_dir.glob("l2_*.duckdb"))
    for p in files[:-keep]:
        p.unlink(missing_ok=True)
        Path(str(p) + ".wal").unlink(missing_ok=True)
    for p in l2_dir.glob("l2_*.duckdb.tmp*"):
        p.unlink(missing_ok=True)


def publish_l2(quiet=False, text_mode=None, l2_dir=None, views_path=None, source_db=None) -> Path:
    """Build a new serving database and point CURRENT at it. Returns the new file's path."""
    import duckdb

    from c360.layer2 import doctor

    t0 = time.time()
    s = config.settings()
    l2_dir = Path(l2_dir) if l2_dir else s.l2_dir
    source_db = Path(source_db) if source_db else s.source_db
    views = load_views(views_path)
    log = (lambda *a: None) if quiet else (lambda *a: print(*a, flush=True))
    if not source_db.exists():
        raise PublishError(f"Layer 1 database not found: {source_db}. Run Layer 1 first (or the synthetic generator).")
    l2_dir.mkdir(parents=True, exist_ok=True)

    build_ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    while (l2_dir / f"l2_{build_ts}.duckdb").exists():      # one build per second at most
        time.sleep(0.2)
        build_ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    final = l2_dir / f"l2_{build_ts}.duckdb"
    tmp = l2_dir / f"l2_{build_ts}.duckdb.tmp"
    for stale in (tmp, Path(str(tmp) + ".wal")):
        stale.unlink(missing_ok=True)

    mongo_db = config.mongo_db()
    con = duckdb.connect(str(tmp))
    try:
        con.execute(f"ATTACH '{source_db}' AS src (READ_ONLY)")
        text_stats = create_file_text_tables(con, views, s.data_root)

        report = doctor.check(con=con, views=views, mongo=mongo_db, source_db=source_db)
        md_path, _ = doctor.write_report(report, s.out_dir)
        if report.errors:
            raise PublishError(f"doctor found {len(report.errors)} error(s); nothing was published. See {md_path}\n  "
                               + "\n  ".join(f"{i.subject}: {i.message}" for i in report.errors[:10]))

        con.execute("CREATE SCHEMA l2")
        built: dict[str, list[str]] = {}
        warnings = []
        for name, view in views.items():
            if view.get("kind") == "mongo":
                n = build_mongo_table(con, name, view, mongo_db)
                built[name] = list(view["columns"])
                log(f"    l2.{name:<26} {n:>12,} rows  (MongoDB projection)")
                continue
            plan = resolve_view(con, name, view)
            if plan.table_error or not plan.columns:
                warnings.append(f"l2.{name} skipped: {plan.table_error or 'no allow-listed column exists in the source'}")
                continue
            for col, _, err in plan.skipped:
                warnings.append(f"l2.{name}.{col} skipped (optional): {err}")
            con.execute(f"CREATE TABLE l2.{name} AS {render(view, plan.columns)}")
            built[name] = [c for c, _ in plan.columns]
            n = con.execute(f"SELECT count(*) FROM l2.{name}").fetchone()[0]
            log(f"    l2.{name:<26} {n:>12,} rows")
        for name, st in text_stats.items():
            if st.get("files") and st["readable"] < st["files"]:
                warnings.append(f"l2.{name}: {st['files'] - st['readable']:,} of {st['files']:,} transcript files "
                                "missing or unreadable (text left NULL)")

        n_pol, pol_stats = build_policy_table(con, s.data_root)
        built["policy_sections"] = list(policies.COLUMNS)
        log(f"    l2.{'policy_sections':<26} {n_pol:>12,} rows  (reference documents: {pol_stats.get('documents', 0)} current, "
            f"{pol_stats.get('superseded_skipped', 0)} superseded skipped)")
        if pol_stats.get("missing_files"):
            warnings.append(f"l2.policy_sections: {pol_stats['missing_files']} reference document file(s) not found")
        if not n_pol:
            warnings.append(f"l2.policy_sections is empty ({pol_stats.get('note', 'no documents found')}); search_policy will find nothing")

        mode = build_text_index(con, views, built, text_mode)

        con.execute("CREATE SCHEMA meta")
        snapshot = s.snapshot_date
        con.execute("""CREATE TABLE meta.build AS SELECT ? AS build_ts, CAST(? AS DATE) AS snapshot_date,
                       ? AS pipeline_version, ? AS source_db_path, CAST(? AS TIMESTAMP) AS source_db_mtime,
                       ? AS l2_version, ? AS text_search_mode""",
                    [build_ts, snapshot, _pipeline_version(mongo_db), str(source_db),
                     dt.datetime.fromtimestamp(source_db.stat().st_mtime), config.L2_VERSION, mode])
        con.execute("CREATE TABLE meta.columns (table_name VARCHAR, column_name VARCHAR, data_type VARCHAR, description VARCHAR)")
        descriptions = _column_descriptions()
        rows = con.execute("""SELECT table_name, column_name, data_type FROM information_schema.columns
                              WHERE table_catalog = current_database() AND table_schema = 'l2'
                              ORDER BY table_name, ordinal_position""").fetchall()
        con.executemany("INSERT INTO meta.columns VALUES (?, ?, ?, ?)",
                        [(t, c, typ, descriptions.get((t, c), "")) for t, c, typ in rows])
        con.execute("CREATE TABLE meta.row_counts (table_name VARCHAR, row_count BIGINT)")
        for name in built:
            con.execute(f"INSERT INTO meta.row_counts SELECT '{name}', count(*) FROM l2.{name}")

        for ft in (v.get("file_text") for v in views.values()):
            if ft:
                con.execute(f'DROP TABLE IF EXISTS {ft["temp_table"]}')
        con.execute("DETACH src")
        denied = [f"{sch}.{t}.{c}" for sch, t, c in con.execute(
            """SELECT table_schema, table_name, column_name FROM information_schema.columns
               WHERE table_catalog = current_database() AND table_schema NOT IN ('information_schema', 'pg_catalog')""").fetchall()
            if deny_list.is_denied(c)]
        if denied:
            raise PublishError("deny-listed column(s) in the serving database, nothing was published: " + ", ".join(denied)
                               + ". Remove them from l2_views.yaml (do not edit the deny-list).")
        counts = dict(con.execute("SELECT table_name, row_count FROM meta.row_counts").fetchall())
        con.execute("CHECKPOINT")
        con.close()
    except BaseException:
        try:
            con.close()
        except Exception:       # noqa: BLE001
            pass
        for stale in (tmp, Path(str(tmp) + ".wal")):
            stale.unlink(missing_ok=True)
        raise

    os.replace(tmp, final)
    pointer_tmp = l2_dir / "CURRENT.tmp"
    pointer_tmp.write_text(final.name + "\n")
    os.replace(pointer_tmp, l2_dir / "CURRENT")
    _cleanup(l2_dir)

    log(f"  [publish_l2] {len(built)} tables, {sum(counts.values()):,} rows, text search: {mode}, "
        f"{final.stat().st_size / 1e6:.1f} MB ({time.time() - t0:.1f}s)")
    for w in warnings:
        log(f"  [publish_l2] warning: {w}")
    log(f"  [publish_l2] serving database: {final}")
    return final


_SRC_REF = re.compile(r"\bsrc\.(\w+)\.(\w+)(?:\s+(?:AS\s+)?(?!ON\b|LEFT\b|RIGHT\b|INNER\b|JOIN\b|WHERE\b|GROUP\b|QUALIFY\b|ORDER\b)(\w+))?",
                      re.IGNORECASE)


def source_aliases(view) -> dict[str, tuple[str, str]]:
    """alias -> (schema, table) for every `src.<schema>.<table> <alias>` in a view's source."""
    out = {}
    for schema, table, alias in _SRC_REF.findall(view.get("source") or ""):
        out[(alias or table).lower()] = (schema.lower(), table.lower())
    return out


def referenced_columns(text: str, aliases: dict) -> set[tuple[str, str, str]]:
    """(schema, table, column) for every `alias.column` in a piece of SQL."""
    found = set()
    for alias, column in re.findall(r"\b(\w+)\.(\w+)\b", text or ""):
        if alias.lower() in aliases:
            schema, table = aliases[alias.lower()]
            found.add((schema, table, column.lower()))
    return found


if __name__ == "__main__":
    publish_l2()
