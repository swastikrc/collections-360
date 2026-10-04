"""Raw zone: land every source file in DuckDB exactly as delivered.

CSV columns are kept as VARCHAR so nothing is lost on the way in: leading zeros on
CIFs survive, the outcome code `NA` stays a code, and mixed date formats remain
visible to the data quality checks. Parquet tables keep their physical types.
Each table gets `_src_file` and `_load_ts` lineage columns.
"""
import json
import time

from c360.config import BRONZE_DB, DATA_ROOT, SCHEMA_JSON, connect


def load_bronze(tables=None, force=False):
    schema = json.loads(SCHEMA_JSON.read_text())
    con = connect(BRONZE_DB)
    existing = {r[0] for r in con.execute("select table_name from information_schema.tables").fetchall()}
    for name, spec in schema.items():
        if tables and name not in tables:
            continue
        if name in existing and not force:
            continue
        t0 = time.time()
        path = DATA_ROOT / spec["path"]
        if spec["format"] == "csv":
            src = (f"read_csv('{path}', header=true, all_varchar=true, quote='\"', escape='\"', "
                   f"nullstr='', max_line_size=10000000, parallel=true)")
        else:
            src = f"read_parquet('{path}/*/*.parquet', hive_partitioning=true, union_by_name=true)"
        con.execute(f"""CREATE OR REPLACE TABLE {name} AS
                        SELECT *, '{spec['path']}' AS _src_file, now() AS _load_ts FROM {src}""")
        n = con.execute(f"select count(*) from {name}").fetchone()[0]
        print(f"  bronze.{name:<26} {n:>12,} rows  ({time.time() - t0:5.1f}s)", flush=True)
    con.close()


if __name__ == "__main__":
    load_bronze()
