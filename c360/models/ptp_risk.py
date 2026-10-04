"""Promise risk model: the chance that a promise to pay is broken or only partly kept.

    python -m c360.models.ptp_risk train          # evaluate out of time, then fit the production model
    python -m c360.models.ptp_risk score PTP-...  # live: features (same SQL as training) -> probability + reasons

Inputs are the feature store's model features (c360/features/definitions.yaml, `use_in_model` true),
nothing else: no protected attributes, no province or language, no vulnerability flags.

Model: standardised logistic regression. Each feature's contribution to the log-odds is
coef x (value - training mean) / training sd, so the reasons shown to the agent are exact, not an
approximation. A gradient-boosted model is fitted on the same split for comparison only.

Two fits:
  evaluation  trained on promises made before 2026-04-01, tuned on April 2026, tested on May-Aug 2026
              (a later period with a higher break rate: an honest out-of-time test)
  production  refitted on every labelled promise made before 2026-09-01 (September is mostly still open),
              used for live scoring

A score is advice to a person, never an automatic action: Layer 3 shows it inside the next best action,
which an agent approves, edits or rejects.
"""
import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

import joblib
import numpy as np

from c360.features import store

MODEL_NAME = "ptp_risk"
VERSION = "ptp-risk-1.0"
TRAIN_END = dt.datetime(2026, 4, 1)
VALID_END = dt.datetime(2026, 5, 1)
LABEL_END = dt.datetime(2026, 9, 1)            # promises made later are mostly unresolved at the snapshot
BANDS = [(0.65, "high"), (0.40, "medium"), (0.0, "low")]
SKIP_REASON = {"has_call_text", "has_salary_history", "has_bureau"}   # data-availability flags, not reasons


def model_dir() -> Path:
    return store.features_dir() / "models"


def model_path() -> Path:
    return model_dir() / f"{MODEL_NAME}.joblib"


def out_dir() -> Path:
    return store.ROOT / "outputs" / "features"


def band(p: float) -> str:
    return next(name for cut, name in BANDS if p >= cut)


# ---------------------------------------------------------------- training

def _fit_logistic(X, y, C=1.0, expect=None, max_rounds=10):
    """Standardised logistic regression. `expect` holds +1 / -1 / 0 per column (the direction a feature should push the
    risk, from definitions.yaml). A column whose fitted weight has the wrong sign is dropped (weight 0) and the model is
    refitted, one column at a time, worst first."""
    from sklearn.linear_model import LogisticRegression
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    Z = (X - mu) / sd
    expect = np.zeros(X.shape[1]) if expect is None else np.asarray(expect, float)
    used = np.ones(X.shape[1], bool)
    dropped = []
    for _ in range(max_rounds + 1):
        lr = LogisticRegression(C=C, max_iter=2000).fit(Z[:, used], y)
        coef = np.zeros(X.shape[1])
        coef[used] = lr.coef_[0]
        wrong = np.where(used & (coef * expect < 0))[0]
        if not len(wrong) or len(dropped) >= max_rounds:
            break
        worst = wrong[np.argmax(np.abs(coef[wrong]))]
        used[worst] = False
        dropped.append(int(worst))
    return {"coef": coef, "intercept": float(lr.intercept_[0]), "mean": mu, "sd": sd, "dropped": dropped}


def _predict(m, X):
    z = m["intercept"] + ((X - m["mean"]) / m["sd"]) @ m["coef"]
    return 1 / (1 + np.exp(-z))


def _metrics(y, p) -> dict:
    from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
    order = np.argsort(-p)
    top = order[: max(1, len(p) // 10)]
    bands = {}
    for name in ("high", "medium", "low"):
        sel = np.array([band(x) == name for x in p])
        bands[name] = {"share": float(sel.mean()), "actual_not_kept": float(y[sel].mean()) if sel.any() else None}
    deciles = []
    for i, idx in enumerate(np.array_split(np.argsort(p), 10)):
        deciles.append({"decile": i + 1, "mean_predicted": float(p[idx].mean()), "actual": float(y[idx].mean()), "n": int(len(idx))})
    return {"n": int(len(y)), "base_rate": float(y.mean()), "mean_predicted": float(p.mean()), "auc": float(roc_auc_score(y, p)),
            "brier": float(brier_score_loss(y, p)), "log_loss": float(log_loss(y, p, labels=[0, 1])),
            "top_decile_rate": float(y[top].mean()), "top_decile_lift": float(y[top].mean() / y.mean()),
            "bands": bands, "calibration_deciles": deciles}


def train(log=print) -> dict:
    from sklearn.ensemble import HistGradientBoostingClassifier
    feats = store.feature_names(model_only=True)
    specs = {f["name"]: f for f in store.feature_specs()}
    expect = [{"up": 1, "down": -1}.get(specs[f].get("expect"), 0) for f in feats]
    meta = store.meta()
    if meta.get("definitions_sha256") != store.definitions_sha():
        raise RuntimeError("feature table was built from different definitions; run: python -m c360.features build")
    df = store.load_table(with_labels=True)
    df = df[df.as_of_ts < LABEL_END]
    X, y, t = df[feats].to_numpy(float), df.not_kept.to_numpy(int), df.as_of_ts
    tr, va, te = (t < TRAIN_END).to_numpy(), ((t >= TRAIN_END) & (t < VALID_END)).to_numpy(), (t >= VALID_END).to_numpy()

    # regularisation strength chosen on the validation month only
    grid = {}
    for C in (0.01, 0.1, 1.0):
        m = _fit_logistic(X[tr], y[tr], C, expect)
        grid[C] = _metrics(y[va], _predict(m, X[va]))["log_loss"]
    C = min(grid, key=grid.get)
    ev = _fit_logistic(X[tr | va], y[tr | va], C, expect)
    p_te = _predict(ev, X[te])
    gbm = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, random_state=0).fit(X[tr | va], y[tr | va])
    p_gbm = gbm.predict_proba(X[te])[:, 1]
    evaluation = {"train": {"from": str(t[tr].min()), "to": str(t[tr | va].max()), "n": int((tr | va).sum()),
                            "base_rate": float(y[tr | va].mean())},
                  "test": _metrics(y[te], p_te), "test_period": [str(VALID_END.date()), str(LABEL_END.date())],
                  "chosen_C": C, "dropped_wrong_sign": [feats[j] for j in ev["dropped"]], "validation_log_loss_by_C": {str(k): v for k, v in grid.items()},
                  "gbm_comparison": {"auc": _metrics(y[te], p_gbm)["auc"], "brier": _metrics(y[te], p_gbm)["brier"]}}
    # same-period check: shows how much of any gap is the shift in time rather than the model
    rng = np.random.default_rng(0)
    idx = np.where(te)[0]
    rng.shuffle(idx)
    half = len(idx) // 2
    within = _fit_logistic(X[idx[:half]], y[idx[:half]], C, expect)
    evaluation["within_period_auc"] = _metrics(y[idx[half:]], _predict(within, X[idx[half:]]))["auc"]

    prod = _fit_logistic(X, y, C, expect)
    binary = [f for j, f in enumerate(feats) if set(np.unique(X[:, j])) <= {0.0, 1.0}]
    artefact = {"name": MODEL_NAME, "version": VERSION, "trained_at": dt.datetime.now().isoformat(timespec="seconds"),
                "features": feats, "definitions_version": meta["definitions_version"],
                "definitions_sha256": meta["definitions_sha256"], "serving_build": meta["serving_build"],
                "label": store.definitions()["label"]["description"], "C": C,
                "coef": prod["coef"], "intercept": prod["intercept"], "mean": prod["mean"], "sd": prod["sd"],
                "dropped_wrong_sign": [feats[j] for j in prod["dropped"]], "binary": binary,
                "train_rows": int(len(y)), "train_base_rate": float(y.mean()), "evaluation": evaluation,
                "eval_model": {"coef": ev["coef"], "intercept": ev["intercept"], "mean": ev["mean"], "sd": ev["sd"]}}
    model_dir().mkdir(parents=True, exist_ok=True)
    joblib.dump(artefact, model_path())
    _load.cache_clear()
    out_dir().mkdir(parents=True, exist_ok=True)
    (out_dir() / "ptp_risk_evaluation.json").write_text(json.dumps(_jsonable(artefact), indent=2, default=str))
    card = model_card(artefact)
    docs = store.ROOT / "docs" / "features"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "model_card_ptp_risk.md").write_text(card)
    tm = evaluation["test"]
    log(f"[ptp_risk] C={C}; out-of-time test (May-Aug 2026, n={tm['n']:,}, base {tm['base_rate']:.1%}): AUC {tm['auc']:.3f}, "
        f"Brier {tm['brier']:.3f}, top-decile lift {tm['top_decile_lift']:.2f}; GBM AUC {evaluation['gbm_comparison']['auc']:.3f}; "
        f"within-period AUC {evaluation['within_period_auc']:.3f}")
    log(f"[ptp_risk] production model on {len(y):,} promises -> {model_path()}")
    return artefact


def _jsonable(a):
    out = {k: v for k, v in a.items() if k not in ("coef", "mean", "sd", "eval_model")}
    out["weights"] = {f: {"coef_per_sd": float(c), "mean": float(m), "sd": float(s)}
                      for f, c, m, s in zip(a["features"], a["coef"], a["mean"], a["sd"])}
    return out


# ---------------------------------------------------------------- scoring + explanations

from functools import lru_cache  # noqa: E402


@lru_cache(maxsize=1)
def _load(path: str, mtime: float):
    return joblib.load(path)


def load_model():
    p = model_path()
    if not p.exists():
        return None
    return _load(str(p), p.stat().st_mtime)


def _fmt(template: str, value: float, row: dict) -> str:
    return template.format(value=value, abs=abs(value),
                           ptp_amount=f"${math.expm1(row.get('ptp_amount_log', 0)):,.0f}")


def explain(row: dict, model=None, k_up=3, k_down=2) -> dict:
    """Probability, band and plain-English reasons for one feature row (from store.online or store.offline)."""
    model = model or load_model()
    specs = {f["name"]: f for f in store.feature_specs()}
    x = np.array([float(row[f]) for f in model["features"]])
    z = (x - model["mean"]) / model["sd"]
    contrib = model["coef"] * z
    logit = model["intercept"] + float(contrib.sum())
    p = 1 / (1 + math.exp(-logit))
    groups = {}
    for f, c, v, mu, w in zip(model["features"], contrib, x, model["mean"], model["coef"]):
        spec = specs[f]
        if w == 0 or f in SKIP_REASON or (spec.get("requires") and not row.get(spec["requires"])):
            continue
        groups.setdefault(spec.get("reason_group") or f, []).append((f, float(c), v, mu))
    items = []
    for g, members in groups.items():
        total = sum(c for _, c, _, _ in members)
        if abs(total) < 0.02:
            continue
        # the member that describes the case: an active 0/1 flag first, else the member that moved the score most
        on = [m for m in members if m[0] in model.get("binary", []) and m[2] == 1]
        cont = [m for m in members if m[0] not in model.get("binary", [])]
        f, _, v, mu = on[0] if on else max(cont or members, key=lambda m: abs(m[1]))
        text = _fmt(specs[f]["up"] if v > mu else specs[f]["down"], v, row)
        items.append({"reason": g, "feature": f, "value": float(v), "contribution": float(total), "text": text[0].upper() + text[1:]})
    up = sorted([i for i in items if i["contribution"] > 0], key=lambda i: -i["contribution"])[:k_up]
    down = sorted([i for i in items if i["contribution"] < 0], key=lambda i: i["contribution"])[:k_down]
    b = band(p)
    summary = (f"{p:.0%} chance this promise is not kept in full ({b} risk). "
               + ("Main reasons: " + "; ".join(i["text"].lower() for i in up) + "." if up else "No single factor stands out."))
    return {"probability_not_kept": p, "band": b, "summary": summary, "raises_risk": up, "lowers_risk": down,
            "model": model["version"], "definitions": model["definitions_version"], "advisory": True}


def score(ptp_id: str, serving=None) -> dict | None:
    """Live: compute the promise's features now (same SQL as the training table) and explain the score."""
    model = load_model()
    if model is None:
        return None
    if model["definitions_sha256"] != store.definitions_sha():
        return {"ptp_id": ptp_id, "error": "feature definitions changed since the model was trained; retrain before scoring"}
    row = store.online(ptp_id, serving=serving).get(ptp_id)
    if row is None:
        return None
    out = explain(row, model)
    out["probability_not_kept"] = float(out["probability_not_kept"])
    out.update(ptp_id=ptp_id, c360_id=row["c360_id"], case_id=row["case_id"], as_of=str(row["as_of_ts"]),
               safeguards=_safeguards(row))
    return out


def _safeguards(row: dict) -> list[str]:
    """Signals shown to the agent whatever the score; they call for care, never for more pressure."""
    s = []
    if row.get("txt_hardship") or row.get("hardship_flagged_90d"):
        s.append("Financial hardship was mentioned: check affordability and offer hardship options before asking for payment "
                 "(POL-COLL-004 §4.2).")
    if row.get("txt_distress"):
        s.append("The customer sounded distressed on the promise call: no pressure; consider the vulnerable-customer script.")
    if row.get("txt_dispute") or row.get("dispute_flagged_90d"):
        s.append("A dispute was raised: resolve it before collecting the disputed amount (PRC-COLL-012 §6.1).")
    return s


# ---------------------------------------------------------------- model card

def model_card(a: dict) -> str:
    e, tm = a["evaluation"], a["evaluation"]["test"]
    specs = {f["name"]: f for f in store.feature_specs()}
    w = sorted(zip(a["features"], a["coef"]), key=lambda t: -abs(t[1]))
    L = [f"# Model card: promise risk ({a['version']})", "",
         f"Generated by `python -m c360.models.ptp_risk train` on {a['trained_at']}. Nothing here is typed by hand.", "",
         "## What it predicts", "", f"- Target: {a['label']}.",
         "- Unit: one promise to pay, scored as of the end of the call in which it was made.",
         "- Use: advice inside Layer 3's next best action. A person approves, edits or rejects every action; the score never "
         "acts on its own, and a high score leads to a supportive step (reminder by call, affordability check, smaller plan), "
         "never to more pressure.", "",
         "## Inputs", "",
         f"- {len(a['features'])} features from the feature store (`c360/features/definitions.yaml`, version "
         f"{a['definitions_version']}, fingerprint `{a['definitions_sha256']}`): the same SQL builds the training table and the live "
         "lookup.",
         "- Not used: protected attributes (kept apart in MongoDB `restricted_protected_attributes`, read only by the fairness check), "
         "province, language, agent, vulnerability flags, and `txt_distress` (shown to the agent as a safeguard only).", "",
         "## How well it works (out of time)", "",
         f"- Trained on {e['train']['n']:,} promises made {e['train']['from'][:10]} to {e['train']['to'][:10]} (not kept: "
         f"{e['train']['base_rate']:.1%}); regularisation C = {e['chosen_C']} chosen on April 2026.",
         f"- Weights against the expected direction (dropped and refitted): {', '.join(a['dropped_wrong_sign']) or 'none'} "
         "(production model).",
         f"- Tested on {tm['n']:,} promises made {e['test_period'][0]} to {e['test_period'][1]} (not kept: {tm['base_rate']:.1%}, "
         "a stress period with a much higher break rate than training).", "",
         "| Measure | Value |", "|---|---:|",
         f"| AUC (ranking) | {tm['auc']:.3f} |", f"| Brier score | {tm['brier']:.3f} |",
         f"| Top 10% highest scores: not-kept rate | {tm['top_decile_rate']:.1%} (lift {tm['top_decile_lift']:.2f}x) |",
         f"| Mean predicted vs actual | {tm['mean_predicted']:.1%} vs {tm['base_rate']:.1%} |",
         f"| Gradient-boosted comparison, same split (AUC) | {e['gbm_comparison']['auc']:.3f} |",
         f"| Same-period AUC (fit and test inside May-Aug) | {e['within_period_auc']:.3f} |", "",
         "Risk bands on the test period:", "", "| Band | Share of promises | Actually not kept |", "|---|---:|---:|"]
    for name in ("high", "medium", "low"):
        b = tm["bands"][name]
        L.append(f"| {name} | {b['share']:.1%} | {'-' if b['actual_not_kept'] is None else format(b['actual_not_kept'], '.1%')} |")
    L += ["", "Calibration by decile of predicted risk (test period):", "", "| Decile | Mean predicted | Actual |", "|---:|---:|---:|"]
    L += [f"| {d['decile']} | {d['mean_predicted']:.1%} | {d['actual']:.1%} |" for d in tm["calibration_deciles"]]
    L += ["", "The production model is refitted on every labelled promise up to August 2026, including the stress period, "
          f"({a['train_rows']:,} promises, not kept {a['train_base_rate']:.1%}), so its probabilities reflect current conditions.", "",
          "## Weights (production model, change in log-odds per standard deviation)", "", "| Feature | Weight | Meaning |", "|---|---:|---|"]
    L += [f"| `{f}` | {c:+.3f}{' (dropped: wrong sign)' if f in a['dropped_wrong_sign'] else ''} | {specs[f]['description']} |" for f, c in w]
    L += ["", "## Limitations", "",
          "- Logistic regression: effects are additive; the boosted comparison above shows how much a non-linear model would add.",
          "- The break rate shifted sharply from May 2026; probabilities from a model trained before a shift run low. Retrain "
          "monthly and watch mean predicted vs actual.",
          "- Text patterns were tuned on the 500 public labelled notes (outputs/features/text_feature_check.md); no separate "
          "held-out text set exists.",
          "- Fairness: see `docs/features/fairness_report.md` (measurement only; protected attributes never enter the model).", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m c360.models.ptp_risk", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("train")
    s = sub.add_parser("score")
    s.add_argument("ptp_id")
    a = ap.parse_args(argv)
    if a.cmd == "train":
        train()
    else:
        r = score(a.ptp_id)
        print(json.dumps(r, indent=2, default=str) if r else f"no promise {a.ptp_id} (or no trained model)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
