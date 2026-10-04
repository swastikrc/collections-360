"""Feature store: one definition, point-in-time correct, batch == live.

The point-in-time tests run on a tiny hand-built serving database (no real data needed). The last
test runs on the real serving database and the built feature table when they exist.
"""
import datetime as dt
import random
import re

import duckdb
import pytest

from c360.features import store

FORBIDDEN = r"province|language|vulnerab|accent|gender|marital|citizen|newcomer|accessib|birth|first_name|last_name|email|phone|postal|agent_id"


def test_definitions_are_unique_and_documented():
    specs = store.feature_specs()
    names = [f["name"] for f in specs]
    assert len(names) == len(set(names)) and len(names) >= 20
    assert {f["source"] for f in specs} == {"structured", "text"}
    for f in specs:
        assert f["description"] and f["up"] and f["down"], f["name"]
    assert "txt_distress" not in store.feature_names(model_only=True)          # safeguard signal, never a model input


def test_no_protected_or_contact_columns_in_any_feature_sql():
    defs = store.definitions()
    sql = (defs["spine"] + "".join(g["sql"] for g in defs["groups"])).lower()
    assert not re.search(FORBIDDEN, sql)
    assert "l2." in sql and not re.search(r"\b(curated|golden|bronze)\.", sql)   # governed serving data only


def test_the_label_never_reaches_the_feature_table():
    sql = store.feature_sql().lower()
    assert "ptp_status" not in store.definitions()["spine"].lower()
    assert "paid_date" not in sql and "amount_paid_against" not in sql and "broken_reason" not in sql
    # ptp_status appears only for *earlier* promises whose grace period had ended before the call
    hist = next(g for g in store.definitions()["groups"] if g["name"] == "promise_history")["sql"]
    assert "< s.as_of_date" in hist


# ---------------------------------------------------------------- tiny world

@pytest.fixture()
def tiny(tmp_path):
    p = tmp_path / "serving.duckdb"
    con = duckdb.connect(str(p))
    con.execute("CREATE SCHEMA l2; CREATE SCHEMA meta")
    con.execute("CREATE TABLE meta.build AS SELECT 'TEST' AS build_ts")
    con.execute("""CREATE TABLE l2.promises_to_pay (ptp_id VARCHAR, c360_id VARCHAR, case_id VARCHAR, contact_id VARCHAR,
        ptp_created_ts TIMESTAMP, ptp_amount DOUBLE, ptp_due_date DATE, overdue_at_creation DOUBLE, dpd_at_creation BIGINT,
        ptp_sequence_in_case BIGINT, prior_broken_ptp_count BIGINT, ptp_channel VARCHAR, ptp_type VARCHAR, grace_days BIGINT,
        ptp_status VARCHAR)""")
    con.execute("""INSERT INTO l2.promises_to_pay VALUES
        ('P1', 'C1', 'K1', 'X1', '2026-03-10 10:00', 100, '2026-03-20', 400, 45, 2, 1, 'call', 'single', 2, 'broken'),
        ('P0', 'C1', 'K1', 'X0', '2026-02-01 09:00', 50,  '2026-02-10', 300, 20, 1, 0, 'call', 'single', 2, 'kept'),
        ('P9', 'C1', 'K1', 'X9', '2026-03-05 09:00', 70,  '2026-03-15', 300, 40, 1, 0, 'web',  'single', 2, 'broken'),
        ('P2', 'C2', 'K2', 'X2', '2026-03-10 10:00', 80,  '2026-03-12', 0,   10, 1, 0, 'web',  'single', 2, 'open')""")
    con.execute("""CREATE TABLE l2.contact_history (contact_id VARCHAR, c360_id VARCHAR, contact_ts_local TIMESTAMP,
        direction VARCHAR, rpc_flag BOOLEAN, hardship_mentioned_flag BOOLEAN, dispute_raised_flag BOOLEAN, is_duplicate BOOLEAN)""")
    con.execute("""INSERT INTO l2.contact_history VALUES
        ('X1', 'C1', '2026-03-10 10:00', 'outbound', TRUE, FALSE, FALSE, FALSE),   -- the promise call itself
        ('A1', 'C1', '2026-03-01 10:00', 'inbound',  TRUE, TRUE,  FALSE, FALSE),
        ('A2', 'C1', '2026-03-02 10:00', 'outbound', FALSE, FALSE, FALSE, FALSE),
        ('A3', 'C1', '2026-03-02 10:00', 'outbound', FALSE, FALSE, FALSE, TRUE),   -- duplicate: ignored
        ('A4', 'C1', '2026-01-01 10:00', 'outbound', FALSE, FALSE, TRUE,  FALSE),  -- 68 days before: 90d only
        ('A5', 'C1', '2026-03-11 10:00', 'inbound',  TRUE, TRUE,  TRUE,  FALSE)    -- after the call: never""")
    con.execute("""CREATE TABLE l2.salary_credit_history (c360_id VARCHAR, credit_month VARCHAR, credit_amount_total DOUBLE,
        gap_flag BOOLEAN)""")
    con.execute("""INSERT INTO l2.salary_credit_history VALUES ('C1', '2025-11', 3000, FALSE), ('C1', '2025-12', 3000, FALSE),
        ('C1', '2026-01', 3000, FALSE), ('C1', '2026-02', 1500, TRUE), ('C1', '2026-03', 9000, FALSE)""")
    con.execute("CREATE TABLE l2.bureau_history (c360_id VARCHAR, pull_date DATE, risk_score BIGINT, revolving_utilisation DOUBLE)")
    con.execute("""INSERT INTO l2.bureau_history VALUES ('C1', '2025-12-15', 640, 0.5), ('C1', '2026-02-15', 610, 0.8),
        ('C1', '2026-03-15', 500, 1.9)""")
    con.execute("CREATE TABLE l2.agent_notes (contact_id VARCHAR, note_text VARCHAR)")
    con.execute("""INSERT INTO l2.agent_notes VALUES ('X1', 'spoke w/ cust. lost job in Feb. will try to pay $100 after payday. cust crying'),
        ('A1', 'cust disputes the late fee')""")
    con.execute("CREATE TABLE l2.call_transcripts (contact_id VARCHAR, transcript_text VARCHAR)")
    con.close()
    return p


def test_point_in_time_rules(tiny):
    f = store.online(["P1", "P2"], serving=tiny)
    p1, p2 = f["P1"], f["P2"]
    assert p1["ptp_cover_ratio"] == 0.25 and p1["days_to_due"] == 10 and p1["dpd_at_promise"] == 45
    # earlier promises: P0 resolved before the call (kept); P9's grace period ends 17 Mar, after the call -> not counted
    assert p1["cust_prior_promises"] == 1 and p1["cust_prior_kept_rate"] == pytest.approx(2 / 3)
    # contacts: A1, A2 in 30 days (duplicate A3, the call itself and the later A5 excluded); A4 only in 90 days
    assert p1["contacts_30d"] == 2 and p1["inbound_30d"] == 1
    assert p1["rpc_rate_90d"] == pytest.approx((1 + 1) / (3 + 2))
    assert p1["hardship_flagged_90d"] == 1 and p1["dispute_flagged_90d"] == 1
    # salary: completed months before March only; Feb (1500, gap) vs Nov-Jan average 3000 -> -50%; March's 9000 unseen
    assert p1["has_salary_history"] == 1 and p1["salary_gap_months_3m"] == 1 and p1["salary_change_pct"] == pytest.approx(-0.5)
    # bureau: the 15 Mar pull is after the call
    assert p1["bureau_score"] == 610 and p1["bureau_score_change"] == -30 and p1["bureau_utilisation"] == pytest.approx(0.8)
    # text of the promise call only (A1's dispute note is another contact)
    assert (p1["txt_hardship"], p1["txt_hedged"], p1["txt_payday"], p1["txt_distress"], p1["txt_dispute"]) == (1, 1, 1, 1, 0)
    # a customer with nothing: neutral defaults, no NULLs, missing ratio is 0 (not the -1 clamp)
    assert p2["has_salary_history"] == 0 and p2["salary_change_pct"] == 0 and p2["has_bureau"] == 0 and p2["bureau_score"] == 715 and p2["bureau_utilisation"] == 0
    assert p2["ptp_cover_ratio"] == 1.0 and p2["self_serve_promise"] == 1 and p2["has_call_text"] == 0
    assert all(v is not None for v in p2.values())


def test_batch_and_live_are_identical_on_the_tiny_world(tiny, tmp_path):
    out = tmp_path / "features.duckdb"
    info = store.build(out=out, serving=tiny, log=lambda *_: None)
    assert info["rows"] == 4 and info["labels"] == 3
    ids = ["P0", "P1", "P2", "P9"]
    assert store.offline(ids, path=out) == store.online(ids, serving=tiny)
    m = store.meta(out)
    assert m["definitions_sha256"] == store.definitions_sha() and m["serving_build"] == "TEST"
    con = duckdb.connect(str(out), read_only=True)
    assert "not_kept" not in [r[0] for r in con.execute("DESCRIBE promise_features").fetchall()]


def test_later_data_does_not_change_a_promise_s_features(tiny):
    before = store.online("P1", serving=tiny)["P1"]
    store._serving_con.cache_clear()
    con = duckdb.connect(str(tiny))
    con.execute("INSERT INTO l2.contact_history VALUES ('Z', 'C1', '2026-03-12 10:00', 'inbound', TRUE, TRUE, TRUE, FALSE)")
    con.execute("INSERT INTO l2.salary_credit_history VALUES ('C1', '2026-04', 0, TRUE)")
    con.execute("INSERT INTO l2.bureau_history VALUES ('C1', '2026-04-01', 300, 2.5)")
    con.execute("""INSERT INTO l2.promises_to_pay VALUES
        ('P8', 'C1', 'K1', 'X8', '2026-03-11 09:00', 10, '2026-03-12', 300, 46, 3, 2, 'call', 'single', 0, 'broken')""")
    con.close()
    after = store.online("P1", serving=tiny)["P1"]
    store._serving_con.cache_clear()
    assert after == before


# ---------------------------------------------------------------- real data

def test_batch_equals_live_on_real_data(real_data):
    ids = random.Random(int(dt.date.today().strftime("%Y%m%d"))).sample(list(store.load_table().ptp_id), 200)
    live, batch = store.online(ids), store.offline(ids)
    assert len(live) == 200
    diffs = [(i, k) for i in ids for k in store.feature_names() if live[i][k] != batch[i][k]]
    assert diffs == []
