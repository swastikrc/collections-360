"""Feature store: one definition (definitions.yaml) -> one SQL statement -> batch table and live lookup.

    from c360.features import store
    store.build()                          # every promise -> warehouse/features/features.duckdb
    store.online("PTP-0001234")            # one promise, computed now from the serving database
    store.offline("PTP-0001234")           # the same promise, read back from the batch table

`feature_sql(where)` is the only place SQL is put together. The batch build calls it with
`TRUE` (every promise); the live lookup calls it with `ptp_id IN (...)`. Both run it on the governed
Layer 2 serving database, opened read-only, so the two paths cannot drift apart.
"""
import datetime as dt
import hashlib
import os
from functools import lru_cache
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[2]       # repo root; c360.config is not imported here so test suites can redirect it
DEFINITIONS = Path(__file__).with_name("definitions.yaml")
ID_COLUMNS = ["ptp_id", "c360_id", "case_id", "as_of_ts"]


def features_dir() -> Path:
    return Path(os.environ["C360_FEATURES_DIR"]) if os.environ.get("C360_FEATURES_DIR") else ROOT / "warehouse" / "features"


def features_db() -> Path:
    return features_dir() / "features.duckdb"


def serving_db() -> Path:
    from c360.layer2 import config as l2config
    d = l2config.settings().l2_dir
    return d / (d / "CURRENT").read_text().strip()


# ---------------------------------------------------------------- definitions

@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> dict:
    d = yaml.safe_load(Path(path).read_text())
    names = [f["name"] for g in d["groups"] for f in g["features"]]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise ValueError(f"feature defined twice: {sorted(dup)}")
    return d


def definitions(path=None) -> dict:
    p = Path(path or DEFINITIONS)
    return _load(str(p), p.stat().st_mtime)


def definitions_sha(path=None) -> str:
    """Fingerprint of what the features compute (the generated SQL and the label), not of their wording: editing a
    description or an explanation template keeps it; changing a column, a window or a pattern changes it."""
    defs = definitions(path)
    return hashlib.sha256((feature_sql("TRUE", defs) + label_sql(defs)).encode()).hexdigest()[:16]


def feature_specs(defs=None) -> list[dict]:
    """Every feature with its group and source (structured / text), in definition order."""
    defs = defs or definitions()
    return [dict(f, group=g["name"], source=g["source"]) for g in defs["groups"] for f in g["features"]]


def feature_names(defs=None, model_only=False) -> list[str]:
    return [f["name"] for f in feature_specs(defs) if not model_only or f.get("use_in_model", True)]


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _fill(sql: str, defs: dict) -> str:
    for name, pattern in (defs.get("text_patterns") or {}).items():
        sql = sql.replace("{re:" + name + "}", _quote(pattern))
    if "{re:" in sql:
        raise ValueError(f"unknown text pattern in: {sql[sql.index('{re:'):][:40]}")
    return sql


def feature_sql(where: str = "TRUE", defs=None) -> str:
    """The single SQL statement behind every feature value. `where` filters the spine (promises_to_pay)."""
    defs = defs or definitions()
    ctes = [f"spine AS (\n{defs['spine'].replace('{spine_where}', where)}\n)"]
    cols, joins = [f"spine.{c}" for c in ID_COLUMNS], []
    for g in defs["groups"]:
        ctes.append(f"g_{g['name']} AS (\n{_fill(g['sql'], defs)}\n)")
        joins.append(f"LEFT JOIN g_{g['name']} USING (ptp_id)")
        cols += [f"g_{g['name']}.{f['name']}" for f in g["features"]]
    return ("WITH " + ",\n".join(ctes) + "\nSELECT " + ",\n       ".join(cols) + "\nFROM spine\n" + "\n".join(joins)
            + "\nORDER BY spine.ptp_id")


def label_sql(defs=None) -> str:
    return (defs or definitions())["label"]["sql"]


# ---------------------------------------------------------------- batch

def build(out=None, serving=None, log=print) -> dict:
    """Compute every feature for every promise and write the feature table, the label table and meta."""
    defs = definitions()
    serving = Path(serving or serving_db())
    out = Path(out or features_db())
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = dt.datetime.now()
    src = duckdb.connect(str(serving), read_only=True)
    try:
        feats = src.execute(feature_sql("TRUE", defs)).df()
        labels = src.execute(label_sql(defs)).df()
        build_ts = src.execute("SELECT build_ts FROM meta.build").fetchone()[0]
    finally:
        src.close()
    tmp = out.with_suffix(".tmp.duckdb")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    try:
        con.register("f", feats)
        con.register("l", labels)
        con.execute("CREATE TABLE promise_features AS SELECT * FROM f ORDER BY ptp_id")
        con.execute("CREATE TABLE promise_labels AS SELECT * FROM l ORDER BY ptp_id")
        con.execute("CREATE TABLE meta (key VARCHAR, value VARCHAR)")
        meta = {"definitions_version": defs["version"], "definitions_sha256": definitions_sha(),
                "serving_build": str(build_ts), "serving_file": serving.name, "built_at": t0.isoformat(timespec="seconds"),
                "rows": str(len(feats)), "labelled_rows": str(len(labels)),
                "features": ",".join(feature_names(defs))}
        con.executemany("INSERT INTO meta VALUES (?, ?)", list(meta.items()))
    finally:
        con.close()
    tmp.replace(out)
    secs = (dt.datetime.now() - t0).total_seconds()
    log(f"[features] {len(feats):,} promises x {len(feature_names(defs))} features, {len(labels):,} labels -> {out} ({secs:.0f}s)")
    return {**meta, "path": out, "rows": len(feats), "labels": len(labels), "seconds": secs}


def meta(path=None) -> dict:
    con = duckdb.connect(str(path or features_db()), read_only=True)
    try:
        return dict(con.execute("SELECT key, value FROM meta").fetchall())
    finally:
        con.close()


def load_table(path=None, with_labels=False):
    """The batch feature table as a DataFrame (optionally inner-joined to the labels, for training)."""
    con = duckdb.connect(str(path or features_db()), read_only=True)
    try:
        if with_labels:
            return con.execute("SELECT f.*, l.not_kept, l.ptp_status, l.resolved_date FROM promise_features f "
                               "JOIN promise_labels l USING (ptp_id) ORDER BY ptp_id").df()
        return con.execute("SELECT * FROM promise_features ORDER BY ptp_id").df()
    finally:
        con.close()


def offline(ptp_ids, path=None) -> dict:
    ids = [ptp_ids] if isinstance(ptp_ids, str) else list(ptp_ids)
    con = duckdb.connect(str(path or features_db()), read_only=True)
    try:
        df = con.execute("SELECT * FROM promise_features WHERE ptp_id IN (SELECT unnest($ids)) ORDER BY ptp_id", {"ids": ids}).df()
    finally:
        con.close()
    return _rows(df)


# ---------------------------------------------------------------- live

@lru_cache(maxsize=2)
def _serving_con(path: str):
    con = duckdb.connect(path, read_only=True)
    con.execute("SET enable_external_access = false")
    return con


def online(ptp_ids, serving=None) -> dict:
    """Compute the features now, for one promise or a few, with the same SQL as the batch build.
    Returns {ptp_id: {feature: value, ...}}; ids that do not exist are absent."""
    ids = [ptp_ids] if isinstance(ptp_ids, str) else list(ptp_ids)
    con = _serving_con(str(serving or serving_db())).cursor()
    df = con.execute(feature_sql("ptp_id IN (SELECT unnest($ids))"), {"ids": ids}).df()
    return _rows(df)


def _rows(df) -> dict:
    out = {}
    for rec in df.to_dict(orient="records"):
        for k, v in rec.items():
            if hasattr(v, "item"):
                rec[k] = v.item()
            if k == "as_of_ts" and v is not None and hasattr(v, "to_pydatetime"):
                rec[k] = v.to_pydatetime()
        out[rec["ptp_id"]] = rec
    return out
