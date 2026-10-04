"""Independent ground truth for the synthetic world.

Every answer here is computed with pandas straight from Layer 1's curated/golden tables and the
MongoDB JSON Lines file. Nothing in this module touches the serving database, metrics.yaml,
compute_metric or periods.py: dates arrive as explicit ISO start/end strings, so a mistake in the
Layer 2 code cannot leak into the expected answers.
"""
import datetime as dt
import json
from pathlib import Path

import pandas as pd

BUCKET_RANK = {"current": 0, "1-30": 1, "31-60": 2, "61-90": 3, "91-120": 4, "121-150": 5, "151-180": 6, "180+": 7}
ALL_TIME = ("1900-01-01", "2999-12-31")

_CURATED = ["promises_to_pay", "collections_cases", "contact_history", "offers", "account_monthly_snapshot",
            "card_accounts", "loan_accounts", "deposit_accounts", "model_scores", "external", "customers",
            "agent_notes"]
_GOLDEN = ["dq_results", "crm_person_map", "case_xref", "account_xref"]


def _day(series):
    return pd.to_datetime(series).dt.normalize()


def _between(series, start, end):
    d = _day(series)
    return (d >= pd.Timestamp(start)) & (d <= pd.Timestamp(end))


def _clean(v, digits=6):
    if v is None or (isinstance(v, float) and v != v):
        return None
    if hasattr(v, "item"):
        v = v.item()
    return round(v, digits) if isinstance(v, float) else v


class Truth:
    def __init__(self, frames, docs=None):
        self.f = frames
        self.docs = docs or []

    @classmethod
    def from_db(cls, db_path, jsonl_path=None):
        import duckdb
        con = duckdb.connect(str(db_path), read_only=True)
        frames = {t: con.execute(f"SELECT * FROM curated.{t}").df() for t in _CURATED}
        frames.update({t: con.execute(f"SELECT * FROM golden.{t}").df() for t in _GOLDEN})
        con.close()
        jsonl_path = Path(jsonl_path) if jsonl_path else Path(db_path).parent / "customers.jsonl"
        docs = []
        if jsonl_path.exists():
            with open(jsonl_path) as f:
                docs = [json.loads(line) for line in f if line.strip()]
        return cls(frames, docs)

    # ---- helpers ------------------------------------------------------------------------------
    def _with_case(self, df):
        cases = self.f["collections_cases"][["case_id", "primary_product", "queue", "hardship_reason", "current_bucket"]]
        return df.merge(cases, on="case_id", how="left")

    @staticmethod
    def _filter(df, where, names):
        for k, v in (where or {}).items():
            df = df[df[names.get(k, k)] == v]
        return df

    @staticmethod
    def _result(df, by, names, fn):
        """Scalar when `by` is None, else {label: value} (rows with a null label are left out)."""
        if not by:
            return _clean(fn(df))
        col = names.get(by, by)
        out = {}
        for label, grp in df.groupby(col, dropna=True):
            out[label.item() if hasattr(label, "item") else label] = _clean(fn(grp))
        return out

    # ---- promises to pay ----------------------------------------------------------------------
    _PTP_DIMS = {"product": "primary_product", "channel": "ptp_channel"}

    def _due_promises(self, start, end, where):
        p = self._with_case(self.f["promises_to_pay"])
        p = p[_between(p["ptp_due_date"], start, end)]
        p = p[p["ptp_status"].isin(["kept", "partially_kept", "broken"])]
        return self._filter(p, where, self._PTP_DIMS)

    def promise_kept_rate(self, start, end, by=None, where=None):
        p = self._due_promises(start, end, where)
        return self._result(p, by, self._PTP_DIMS, lambda g: (g["ptp_status"] == "kept").mean() if len(g) else None)

    def promise_broken_rate(self, start, end, by=None, where=None):
        p = self._due_promises(start, end, where)
        return self._result(p, by, self._PTP_DIMS, lambda g: (g["ptp_status"] == "broken").mean() if len(g) else None)

    def broken_promise_rate(self, start, end, by=None, where=None):      # official name
        return self.promise_broken_rate(start, end, by, where)

    def collected_amount(self, start, end, by=None, where=None):
        """Official: payments received against promises, by payment date."""
        p = self._with_case(self.f["promises_to_pay"])
        p = p[p["amount_paid_against"].fillna(0) > 0]
        p = self._filter(p[_between(p["paid_date"], start, end)], where, self._PTP_DIMS)
        return self._result(p, by, self._PTP_DIMS, lambda g: float(g["amount_paid_against"].sum()))

    def promise_count(self, start, end, by=None, where=None):
        p = self._with_case(self.f["promises_to_pay"])
        p = self._filter(p[_between(p["ptp_created_ts"], start, end)], where, {**self._PTP_DIMS, "status": "ptp_status"})
        return self._result(p, by, {**self._PTP_DIMS, "status": "ptp_status"}, len)

    # ---- cases -----------------------------------------------------------------------------------
    _CASE_DIMS = {"product": "primary_product", "bucket": "current_bucket"}

    def cure_rate(self, start, end, by=None, where=None):
        """Official: cases open at the start of the period that cured within it (holds excluded)."""
        c = self.f["collections_cases"]
        open_at_start = (_day(c["case_open_date"]) <= pd.Timestamp(start)) & (
            c["outcome_date"].isna() | (_day(c["outcome_date"]) >= pd.Timestamp(start)))
        holds = c["insolvency_hold_flag"].fillna(False).astype(bool) | c["deceased_hold_flag"].fillna(False).astype(bool)
        c = self._filter(c[open_at_start & ~holds].copy(), where, self._CASE_DIMS)
        c["cured_in_period"] = c["cure_flag"].fillna(False).astype(bool) & _between(c["cure_date"], start, end)
        return self._result(c, by, self._CASE_DIMS, lambda g: g["cured_in_period"].mean() if len(g) else None)

    def hardship_rate(self, start, end, by=None, where=None):
        """Official: share of cases opened in the period with hardship_flag."""
        c = self.f["collections_cases"]
        c = self._filter(c[_between(c["case_open_date"], start, end)], where, self._CASE_DIMS)
        return self._result(c, by, self._CASE_DIMS, lambda g: g["hardship_flag"].fillna(False).astype(bool).mean() if len(g) else None)

    def _open_cases(self, start, end, where):
        c = self.f["collections_cases"]
        c = c[c["outcome"].isna()]
        return self._filter(c[_between(c["case_open_date"], start, end)], where, self._CASE_DIMS)

    def open_case_count(self, start=ALL_TIME[0], end=ALL_TIME[1], by=None, where=None):
        return self._result(self._open_cases(start, end, where), by, self._CASE_DIMS, len)

    def past_due_amount(self, start=ALL_TIME[0], end=ALL_TIME[1], by=None, where=None):
        return self._result(self._open_cases(start, end, where), by, self._CASE_DIMS,
                            lambda g: float(g["total_overdue"].sum()))

    def average_dpd(self, start=ALL_TIME[0], end=ALL_TIME[1], by=None, where=None):
        return self._result(self._open_cases(start, end, where), by, self._CASE_DIMS,
                            lambda g: float(g["current_dpd"].mean()) if len(g) else None)

    # ---- roll rate -----------------------------------------------------------------------------
    def roll_rate(self, start, end, by=None, where=None):
        """Official: accounts in a bucket at the snapshot on/before `start` that are in a worse bucket at
        the snapshot on/before `end`. Buckets from dpd; charged-off accounts and accounts in a case on
        insolvency hold are left out; accounts missing at the end count as not rolled."""
        s = self.f["account_monthly_snapshot"].copy()
        s["d"] = _day(s["month_end_date"])
        d0 = s.loc[s["d"] <= pd.Timestamp(start), "d"].max()
        d1 = s.loc[s["d"] <= pd.Timestamp(end), "d"].max()
        if pd.isna(d0) or pd.isna(d1) or not d1 > d0:
            return None if not by else {}
        held = set()
        cases = self.f["collections_cases"]
        for ids in cases.loc[cases["insolvency_hold_flag"].fillna(False).astype(bool), "account_ids"]:
            held.update(list(ids) if ids is not None else [])
        # buckets are worked out from days past due (the stored bucket disagrees with dpd in some snapshot rows)
        edges = [(0, "current"), (30, "1-30"), (60, "31-60"), (90, "61-90"), (120, "91-120"), (150, "121-150"), (180, "151-180")]
        s["dpd_bucket"] = s["dpd"].map(lambda v: None if pd.isna(v) else next((name for top, name in edges if v <= top), "180+"))
        a = s[(s["d"] == d0) & s["dpd_bucket"].notna() & ~s["write_off_flag"].fillna(False).astype(bool)
              & (s["account_status"].fillna("") != "charged_off") & ~s["account_id"].isin(held)]
        b = s.loc[s["d"] == d1, ["account_id", "dpd_bucket"]].rename(columns={"dpd_bucket": "next_bucket"})
        j = a.merge(b, on="account_id", how="left")
        j["from_bucket"] = j["dpd_bucket"]
        j["product"] = j["product_type"]
        j["product_family"] = j["account_id"].str[:2].map({"C-": "card", "L-": "loan"}).fillna("deposit")
        j["rolled"] = (j["next_bucket"].map(BUCKET_RANK) > j["dpd_bucket"].map(BUCKET_RANK)).fillna(False)
        j = self._filter(j, where, {})
        return self._result(j, by, {}, lambda g: g["rolled"].mean() if len(g) else None)

    # ---- accounts ------------------------------------------------------------------------------
    def _card_loan(self):
        c = self.f["card_accounts"].assign(product_family="card", product=lambda d: d["product_code"])
        l = self.f["loan_accounts"].assign(product_family="loan", product=lambda d: d["product_type"])
        cols = ["product_family", "product", "dpd", "dq_stale_snapshot", "chargeoff_date", "chargeoff_amount"]
        return pd.concat([c[cols], l[cols]], ignore_index=True)

    def delinquency_rate(self, start=ALL_TIME[0], end=ALL_TIME[1], by=None, where=None):
        """Official: active (current-snapshot) card and loan accounts 1+ dpd; accounts without dpd left out."""
        a = self._card_loan()
        a = self._filter(a[~a["dq_stale_snapshot"].astype(bool) & a["dpd"].notna()], where, {})
        return self._result(a, by, {}, lambda g: (g["dpd"] >= 1).mean() if len(g) else None)

    def write_off_rate(self, start, end, by=None, where=None):
        a = self._filter(self._card_loan(), where, {})
        return self._result(a, by, {}, lambda g: _between(g["chargeoff_date"], start, end).mean() if len(g) else None)

    def write_off_count(self, start, end, by=None, where=None):
        a = self._card_loan()
        a = self._filter(a[_between(a["chargeoff_date"], start, end)], where, {})
        return self._result(a, by, {}, len)

    def write_off_amount(self, start, end, by=None, where=None):
        a = self._card_loan()
        a = self._filter(a[_between(a["chargeoff_date"], start, end)], where, {})
        return self._result(a, by, {}, lambda g: float(g["chargeoff_amount"].fillna(0).sum()))

    def payroll_delay_share(self, start=ALL_TIME[0], end=ALL_TIME[1], by=None, where=None):
        """Official: customers with a chequing payroll deposit whose payroll is > 7 days late (current snapshot)."""
        d = self.f["deposit_accounts"]
        d = d[(d["product_type"] == "chequing") & d["payroll_deposit_flag"].fillna(False).astype(bool)
              & ~d["dq_stale_snapshot"].astype(bool)]
        x = self.f["account_xref"]
        x = x[x["role"] == "primary"][["account_id", "c360_id"]]
        d = d.merge(x, left_on="deposit_account_id", right_on="account_id", how="left")
        d = d[d["c360_id"].notna()].copy()
        open_cases = {doc["_id"]: (doc.get("collections") or {}).get("open_cases", 0) or 0 for doc in self.docs}
        d["with_open_case"] = d["c360_id"].map(lambda c: open_cases.get(c, 0) > 0)
        d = self._filter(d, where, {})
        def share(g):
            if not len(g):
                return None
            late = g.loc[g["payroll_delay_days"] > 7, "c360_id"].nunique()
            return late / g["c360_id"].nunique()
        return self._result(d, by, {}, share)

    # ---- contacts ------------------------------------------------------------------------------
    _CONTACT_DIMS = {"channel": "channel_std", "product": "primary_product", "hour": "contact_hour_local"}

    def _contacts(self, start, end, where):
        h = self.f["contact_history"]
        h = h[h["dq_duplicate_of"].isna()]                   # double-loaded contacts never count
        h = self._with_case(h[_between(h["contact_ts_local"], start, end)])
        return self._filter(h, where, self._CONTACT_DIMS)

    @staticmethod
    def _attempts(h):
        return h[h["direction"] == "outbound"]

    def rpc_rate(self, start, end, by=None, where=None):
        """Official: share of outbound CALL attempts that were right-party contacts."""
        h = self._contacts(start, end, where)
        a = h[(h["direction"] == "outbound") & (h["channel_std"] == "call")]
        return self._result(a, by, self._CONTACT_DIMS, lambda g: g["rpc_flag"].astype(bool).mean() if len(g) else None)

    def contact_attempts(self, start, end, by=None, where=None):
        return self._result(self._attempts(self._contacts(start, end, where)), by, self._CONTACT_DIMS, len)

    def contact_attempts_per_case(self, start, end, by=None, where=None):
        a = self._attempts(self._contacts(start, end, where))
        dims = {**self._CONTACT_DIMS, "bucket": "current_bucket"}
        return self._result(a, by, dims, lambda g: len(g) / g["case_id"].nunique() if g["case_id"].nunique() else None)

    def ptp_rate(self, start, end, by=None, where=None):
        h = self._contacts(start, end, where)
        h = h[h["rpc_flag"].fillna(False).astype(bool)]
        return self._result(h, by, self._CONTACT_DIMS, lambda g: g["ptp_made_flag"].fillna(False).astype(bool).mean() if len(g) else None)

    def _costed(self, start, end, where):
        h = self._contacts(start, end, where)
        return h[h["channel_std"] != "system"]

    def cost_per_contact(self, start, end, by=None, where=None):
        h = self._costed(start, end, where)
        return self._result(h, by, self._CONTACT_DIMS, lambda g: float(g["contact_cost_cad"].sum()) / len(g) if len(g) else None)

    def cost_per_rpc(self, start, end, by=None, where=None):
        h = self._costed(start, end, where)
        def per_rpc(g):
            n = int(g["rpc_flag"].fillna(False).astype(bool).sum())
            return float(g["contact_cost_cad"].sum()) / n if n else None
        return self._result(h, by, self._CONTACT_DIMS, per_rpc)

    def payment_on_contact_amount(self, start, end, by=None, where=None):
        h = self._contacts(start, end, where)
        h = h[h["payment_taken_flag"].fillna(False).astype(bool)]
        return self._result(h, by, self._CONTACT_DIMS, lambda g: float(g["payment_amount"].sum()))

    # ---- offers ----------------------------------------------------------------------------------
    _OFFER_DIMS = {"program": "program_id", "product": "primary_product"}

    def hardship_offer_acceptance_rate(self, start, end, by=None, where=None):
        o = self._with_case(self.f["offers"])
        o = self._filter(o[_between(o["offer_ts"], start, end)], where, self._OFFER_DIMS)
        return self._result(o, by, self._OFFER_DIMS, lambda g: (g["customer_response"] == "accepted").mean() if len(g) else None)

    def offers_accepted(self, start, end):
        o = self.f["offers"]
        return int(((o["customer_response"] == "accepted") & _between(o["offer_ts"], start, end)).sum())

    def avg_accepted_offer_payment(self):
        o = self.f["offers"]
        return _clean(float(o.loc[o["customer_response"] == "accepted", "proposed_payment_amount"].mean()))

    # ---- questions outside the governed metrics (custom SQL route) ---------------------------
    def customers_with_hardship_flag(self):
        return sum(1 for d in self.docs if d["hardship_indicators"]["financial_hardship_flag"])

    def customers_with_links_pending_review(self):
        return sum(1 for d in self.docs if d["identity"]["links_pending_review"] > 0)

    def cards_in_stored_bucket(self, bucket):
        return int((self.f["card_accounts"]["dpd_bucket"] == bucket).sum())

    def card_bucket_mismatches(self):
        return int(self.f["card_accounts"]["dq_bucket_mismatch"].fillna(False).astype(bool).sum())

    def duplicate_contacts(self):
        return int(self.f["contact_history"]["dq_duplicate_of"].notna().sum())

    def loans_with_co_borrower(self):
        return int(self.f["loan_accounts"]["co_borrower_ref"].notna().sum())

    def unsecured_loan_count(self):
        return int((~self.f["loan_accounts"]["secured_flag"].astype(bool)).sum())

    def avg_bureau_risk_score(self):
        return _clean(float(self.f["external"]["risk_score"].mean()))

    def superseded_scores(self):
        return int(self.f["model_scores"]["dq_superseded_duplicate"].astype(bool).sum())

    def dq_check_count(self):
        d = self.f["dq_results"]
        return int((d["run_ts"] == d["run_ts"].max()).sum())

    def open_cases_with_flag(self, flag):
        c = self.f["collections_cases"]
        return int((c["outcome"].isna() & c[flag].fillna(False).astype(bool)).sum())

    def total_exposure_open_hardship(self):
        c = self.f["collections_cases"]
        return _clean(float(c.loc[c["outcome"].isna() & c["hardship_flag"].astype(bool), "total_exposure"].sum()))

    def customers_by_segment(self):
        m = self.f["crm_person_map"]
        master = m[m["is_master"].astype(bool)].merge(self.f["customers"][["crm_customer_id", "segment"]], on="crm_customer_id")
        return {k: int(v) for k, v in master.groupby("segment").size().items()}

    def deposits_with_garnishment(self):
        return int(self.f["deposit_accounts"]["garnishment_flag"].fillna(False).astype(bool).sum())

    def cases_with_repaired_link(self):
        x = self.f["case_xref"]
        return int(x["link_method"].str.startswith("repaired").sum())

    # ---- single-customer and text lookups --------------------------------------------------
    def sample_case(self, which="open_hardship"):
        """A deterministic case to ask about: the first (by id) open hardship case with a linked person."""
        c = self.f["collections_cases"].merge(self.f["case_xref"][["case_id", "c360_id"]], on="case_id")
        c = c[c["c360_id"].notna() & c["outcome"].isna()]
        if which == "open_hardship":
            c = c[c["hardship_flag"].astype(bool) & c["hardship_reason"].notna()]
        return c.sort_values("case_id").iloc[0].to_dict()

    def case_field(self, case_id, field):
        c = self.f["collections_cases"]
        return _clean(c.loc[c["case_id"] == case_id, field].iloc[0])

    def customer_open_cases(self, c360_id):
        doc = next(d for d in self.docs if d["_id"] == c360_id)
        return int(doc["collections"]["open_cases"])

    def notes_mentioning(self, phrase):
        n = self.f["agent_notes"]
        return int(n["note_text"].str.contains(phrase, case=False, regex=False).sum())

    # ---- generic entry point used by the question files -----------------------------------
    def resolve(self, fn, args=None):
        if fn.startswith("_") or not hasattr(self, fn):
            raise KeyError(f"unknown truth function: {fn}")
        return getattr(self, fn)(**(args or {}))


def iso(d):
    return d.isoformat() if isinstance(d, (dt.date, dt.datetime)) else str(d)
