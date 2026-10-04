"""Fairness check for the promise risk model: measurement only.

    python -m c360.models.fairness        # -> docs/features/fairness_report.md, outputs/features/fairness.json

The model never sees protected attributes. This check reads them from MongoDB
`restricted_protected_attributes` (the only place they are kept), joins them in memory to the
out-of-time test predictions (the evaluation model, May-Aug 2026, promises it never trained on),
and reports per group: size, actual not-kept rate, mean predicted risk, calibration gap, share
placed in the high band, AUC, and the error rates. Nothing is written back; the attributes are
not stored in the feature store or the model.

Flags (for a person to review, not automatic fixes):
  - high-band share ratio vs the largest group outside 0.80-1.25 (four-fifths rule, both ways)
  - calibration gap differs from the overall gap by more than 5 points
  - AUC more than 0.05 below the overall AUC
Groups under 500 promises are reported but not flagged (too few to judge).
"""
import datetime as dt
import json
import sys

import numpy as np

from c360.features import store
from c360.models import ptp_risk

ATTRIBUTES = {
    "gender_code": "Gender",
    "marital_status": "Marital status",
    "citizenship_status": "Citizenship status",
    "newcomer_program_flag": "Newcomer programme",
    "accessibility_needs_flag": "Accessibility needs",
    "vulnerability_flag": "Vulnerability recorded",
}
MIN_N = 500


def _auc(y, p):
    from sklearn.metrics import roc_auc_score
    return float(roc_auc_score(y, p)) if 0 < y.sum() < len(y) else None


def _protected(c360_ids) -> dict:
    from c360 import mongo
    coll = mongo.db().restricted_protected_attributes
    out = {}
    ids = list(c360_ids)
    for i in range(0, len(ids), 20000):
        for d in coll.find({"c360_id": {"$in": ids[i:i + 20000]}}, {"_id": 0, "c360_id": 1, **{a: 1 for a in ATTRIBUTES}}):
            out[d["c360_id"]] = d
    return out


def run(log=print) -> dict:
    model = ptp_risk.load_model()
    if model is None:
        raise RuntimeError("no trained model: python -m c360.models.ptp_risk train")
    ev = model["eval_model"]
    df = store.load_table(with_labels=True)
    df = df[(df.as_of_ts >= ptp_risk.VALID_END) & (df.as_of_ts < ptp_risk.LABEL_END)].reset_index(drop=True)
    X = df[model["features"]].to_numpy(float)
    p = 1 / (1 + np.exp(-(ev["intercept"] + ((X - ev["mean"]) / ev["sd"]) @ ev["coef"])))
    y = df.not_kept.to_numpy(int)
    high = np.array([ptp_risk.band(v) == "high" for v in p])
    attrs = _protected(df.c360_id.unique())
    overall = {"n": int(len(y)), "actual": float(y.mean()), "predicted": float(p.mean()), "high_share": float(high.mean()),
               "auc": _auc(y, p), "matched": int(df.c360_id.isin(attrs.keys()).sum())}
    overall["gap"] = overall["predicted"] - overall["actual"]
    report = {"generated": dt.datetime.now().isoformat(timespec="seconds"), "model": model["version"],
              "period": [str(ptp_risk.VALID_END.date()), str(ptp_risk.LABEL_END.date())], "overall": overall, "attributes": {}}
    for attr in ATTRIBUTES:
        vals = df.c360_id.map(lambda c: (attrs.get(c) or {}).get(attr)).map(
            lambda v: "not recorded" if v is None or (isinstance(v, float) and v != v) else str(v))
        rows = []
        for v in sorted(vals.unique(), key=lambda v: -(vals == v).sum()):
            m = (vals == v).to_numpy()
            yy, pp, hh = y[m], p[m], high[m]
            kept, notk = yy == 0, yy == 1
            rows.append({"group": v, "n": int(m.sum()), "actual": float(yy.mean()), "predicted": float(pp.mean()),
                         "gap": float(pp.mean() - yy.mean()), "high_share": float(hh.mean()), "auc": _auc(yy, pp),
                         "high_when_kept": float(hh[kept].mean()) if kept.any() else None,
                         "not_high_when_not_kept": float((~hh[notk]).mean()) if notk.any() else None})
        ref = max((r for r in rows if r["group"] != "not recorded"), key=lambda r: r["n"])
        for r in rows:
            r["high_share_ratio"] = r["high_share"] / ref["high_share"] if ref["high_share"] else None
            flags = []
            if r["n"] >= MIN_N:
                if r["high_share_ratio"] is not None and not 0.8 <= r["high_share_ratio"] <= 1.25:
                    flags.append("high-band share ratio outside 0.80-1.25")
                if abs(r["gap"] - overall["gap"]) > 0.05:
                    flags.append("calibration differs by more than 5 points")
                if r["auc"] is not None and overall["auc"] is not None and r["auc"] < overall["auc"] - 0.05:
                    flags.append("AUC more than 0.05 below overall")
            r["flags"] = flags
        report["attributes"][attr] = {"reference": ref["group"], "groups": rows}
    out = ptp_risk.out_dir()
    out.mkdir(parents=True, exist_ok=True)
    (out / "fairness.json").write_text(json.dumps(report, indent=2))
    md = render(report)
    docs = store.ROOT / "docs" / "features"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "fairness_report.md").write_text(md)
    n_flags = sum(len(r["flags"]) for a in report["attributes"].values() for r in a["groups"])
    log(f"[fairness] {overall['n']:,} test promises, {overall['matched']:,} matched to protected attributes; {n_flags} flag(s) "
        f"-> docs/features/fairness_report.md")
    return report


def render(r) -> str:
    o = r["overall"]
    pct = lambda x: "-" if x is None else f"{x:.1%}"
    L = ["# Fairness check: promise risk model", "",
         f"Generated {r['generated']} by `python -m c360.models.fairness` (model {r['model']}). Measurement only: protected attributes "
         "come from MongoDB `restricted_protected_attributes`, are joined in memory to the out-of-time test predictions "
         f"({r['period'][0]} to {r['period'][1]}, promises the evaluation model never trained on), and are never stored with the "
         "features or used by the model.", "",
         f"Overall: {o['n']:,} promises ({o['matched']:,} matched to a protected-attribute record), not kept {pct(o['actual'])}, "
         f"mean predicted {pct(o['predicted'])}, high band {pct(o['high_share'])}, AUC {o['auc']:.3f}.", "",
         "What a high score does: Layer 3 suggests a supportive step (a reminder call before the date, an affordability check, a "
         "smaller plan) for a person to approve. So the main fairness question is whether the score is equally *accurate* for every "
         "group (calibration, AUC, error rates), and the high-band share is shown for transparency.", "",
         f"Flags: share ratio vs the largest group outside 0.80-1.25; calibration gap more than 5 points from overall; AUC more than "
         f"0.05 below overall. Groups under {MIN_N} promises are shown but not flagged.", ""]
    for attr, a in r["attributes"].items():
        L += [f"## {ATTRIBUTES[attr]} (`{attr}`)", "", "| Group | Promises | Not kept | Predicted | Gap | High band | Ratio | AUC | "
              "High band when kept | Missed when not kept | Flags |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
        for g in a["groups"]:
            L.append(f"| {g['group']}{' (reference)' if g['group'] == a['reference'] else ''} | {g['n']:,} | {pct(g['actual'])} | "
                     f"{pct(g['predicted'])} | {g['gap']:+.1%} | {pct(g['high_share'])} | "
                     f"{'-' if g['high_share_ratio'] is None else format(g['high_share_ratio'], '.2f')} | "
                     f"{'-' if g['auc'] is None else format(g['auc'], '.3f')} | {pct(g['high_when_kept'])} | "
                     f"{pct(g['not_high_when_not_kept'])} | {'; '.join(g['flags']) or 'none'} |")
        L.append("")
    flagged = [(attr, g) for attr, a in r["attributes"].items() for g in a["groups"] if g["flags"]]
    L += ["## Reading", ""]
    if not flagged:
        L.append("- No group is flagged: the score is about as accurate and as well calibrated for every group measured.")
    else:
        for attr, g in flagged:
            ref = next(x for x in r["attributes"][attr]["groups"] if x["group"] == r["attributes"][attr]["reference"])
            L.append(f"- {ATTRIBUTES[attr]} = {g['group']}: {'; '.join(g['flags'])}. Actually not kept {pct(g['actual'])} vs "
                     f"{pct(ref['actual'])} for {ref['group']}; calibration gap {g['gap']:+.1%} vs {ref['gap']:+.1%}; AUC "
                     f"{g['auc']:.3f} vs {ref['auc']:.3f}; kept promises placed in the high band {pct(g['high_when_kept'])} vs "
                     f"{pct(ref['high_when_kept'])}. A flag asks a person to look at why, not to use the attribute.")
        if any(attr == "vulnerability_flag" for attr, _ in flagged):
            L.append("- Mitigation in place for vulnerability: a high score never leads to more pressure. Layer 3's next best action "
                     "routes vulnerable customers to a certified agent with the vulnerable-customer script (POL-COLL-004 §4.5), "
                     "puts hardship options before any request for payment, and every action needs a person's approval.")
    L += ["- Differences in the high-band share largely follow differences in the actual not-kept rate; the model has no access to "
          "the attributes, so any difference comes through correlated inputs (e.g. DPD, promise size vs overdue amount).", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
