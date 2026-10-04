"""Publish the golden Collections 360 to MongoDB.

DuckDB is the processing engine (raw -> curated -> identity resolution -> document assembly).
The served data product is MongoDB: this step streams the assembled documents into a fresh
`customers_build` collection (validated against the contract schema), indexes it, re-attaches any
interactions written back by Layers 3-4, then atomically renames it to `customers`. Readers never
see a half-built store. The DuckDB staging copy is dropped afterwards.
"""
import datetime as dt
import os
import time
from multiprocessing import Pool

from c360 import config, mongo

STAGING = "golden.c360_customer"
BATCH = 2000


def _bson_safe(v):
    """DuckDB returns datetime.date for DATE; BSON stores datetimes only."""
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day)
    if isinstance(v, dict):
        return {k: _bson_safe(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_bson_safe(x) for x in v]
    return v


def _load_partition(args):
    part, nparts, target = args
    import duckdb
    con = duckdb.connect(str(config.C360_DB), read_only=True)
    con.execute("SET threads=2")
    d = mongo.db()
    coll = d[target]
    cur = con.execute(f"SELECT * FROM {STAGING} WHERE hash(c360_id) % {nparts} = {part}")
    cols = ["_id" if c[0] == "c360_id" else c[0] for c in cur.description]
    n = 0
    while rows := cur.fetchmany(BATCH):
        coll.insert_many([_bson_safe(dict(zip(cols, r))) for r in rows], ordered=False)
        n += len(rows)
    con.close()
    return n


def publish(workers=None):
    t0 = time.time()
    d = mongo.db()
    d.command("ping")
    workers = workers or max(2, min(12, (os.cpu_count() or 4) - 4))
    d.drop_collection("customers_build")
    d.create_collection("customers_build", validator=mongo.CUSTOMER_VALIDATOR, validationLevel="strict")
    with Pool(workers) as pool:
        total = sum(pool.map(_load_partition, [(i, workers, "customers_build") for i in range(workers)]))
    print(f"    mongo: inserted {total:,} customer documents ({time.time() - t0:.0f}s)", flush=True)

    # Re-attach interactions written back since the last build
    from pymongo import UpdateOne
    ops = [UpdateOne({"_id": g["_id"]}, {"$set": {"interactions": g["items"]}})
           for g in d.interactions.aggregate([
               {"$sort": {"occurred_ts": -1}},
               {"$group": {"_id": "$c360_id", "items": {"$push": {k: f"${k}" for k in
                   ["interaction_id", "interaction_type", "source", "occurred_ts", "case_id", "payload", "written_by", "written_ts"]}}}}])]
    if ops:
        d.customers_build.bulk_write(ops, ordered=False)
    d.customers_build.update_many({"interactions": {"$exists": False}}, {"$set": {"interactions": []}})

    mongo.ensure_customer_indexes(d.customers_build)  # build indexes before the swap
    d.customers_build.rename("customers", dropTarget=True)
    mongo.ensure_indexes(d)
    print(f"    mongo: customers indexed and swapped in ({time.time() - t0:.0f}s)", flush=True)

    # Supporting collections
    con = config.connect()
    _replace(d.match_review_queue, con, "SELECT * FROM golden.match_review_queue")
    _replace(d.restricted_protected_attributes, con, "SELECT * FROM golden.c360_protected")
    d.restricted_protected_attributes.create_index("crm_customer_id")
    for t in ["c360_customer_flat", "c360_customer", "c360_protected"]:
        con.execute(f"DROP TABLE IF EXISTS golden.{t}")
    con.close()
    print(f"  mongo: published {d.customers.estimated_document_count():,} customers to "
          f"{mongo.MONGO_URI}/{mongo.MONGO_DB} ({time.time() - t0:.0f}s)", flush=True)


def _replace(coll, con, sql):
    cur = con.execute(sql)
    cols = [c[0] for c in cur.description]
    coll.drop()
    while rows := cur.fetchmany(BATCH * 5):
        coll.insert_many([_bson_safe(dict(zip(cols, r))) for r in rows], ordered=False)


if __name__ == "__main__":
    publish()
