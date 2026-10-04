"""Promise risk model: sign constraints, exact explanations, live scoring on the real data when built."""
import json

import numpy as np
import pytest

from c360.features import store
from c360.models import ptp_risk


def test_wrong_sign_weights_are_dropped():
    rng = np.random.default_rng(0)
    n = 20000
    a = rng.normal(size=n)
    b = a + rng.normal(scale=0.3, size=n)              # correlated with a
    y = (rng.random(n) < 1 / (1 + np.exp(-(1.5 * a - 0.8 * b)))).astype(int)
    X = np.c_[a, b]
    free = ptp_risk._fit_logistic(X, y, C=1.0)
    assert free["coef"][1] < 0 and free["dropped"] == []
    fixed = ptp_risk._fit_logistic(X, y, C=1.0, expect=[1, 1])     # domain says both raise the risk
    assert fixed["dropped"] == [1] and fixed["coef"][1] == 0 and fixed["coef"][0] > 0


def _toy_model(features, coef, mean, binary=()):
    return {"features": features, "coef": np.array(coef, float), "mean": np.array(mean, float), "sd": np.ones(len(features)),
            "intercept": 0.0, "binary": list(binary), "version": "test", "definitions_version": "test"}


def test_explanation_is_exact_grouped_and_skips_missing_data():
    feats = ["ptp_cover_ratio", "ptp_full_cover", "ptp_token_payment", "dpd_at_promise", "bureau_score", "has_bureau", "txt_hardship",
             "has_call_text"]
    m = _toy_model(feats, [0.5, -2.0, -1.0, 0.02, -0.01, 0.3, 0.8, 0.0], [0.7, 0.3, 0.1, 30, 715, 0.7, 0.2, 0.8],
                   binary=["ptp_full_cover", "ptp_token_payment", "has_bureau", "txt_hardship", "has_call_text"])
    row = {"ptp_cover_ratio": 0.4, "ptp_full_cover": 0, "ptp_token_payment": 0, "dpd_at_promise": 90, "bureau_score": 715, "has_bureau": 0,
           "txt_hardship": 1, "has_call_text": 1, "ptp_amount_log": 5.0}
    out = ptp_risk.explain(row, m)
    logit = sum(c * (row[f] - mu) for f, c, mu in zip(feats, m["coef"], m["mean"]))
    assert out["probability_not_kept"] == pytest.approx(1 / (1 + np.exp(-logit)))
    reasons = {i["reason"]: i for i in out["raises_risk"] + out["lowers_risk"]}
    # the three cover features are one reason; their contributions add up
    assert reasons["cover"]["contribution"] == pytest.approx(0.5 * (0.4 - 0.7) - 2.0 * (0 - 0.3) - 1.0 * (0 - 0.1))
    assert reasons["cover"]["text"].startswith("It is a part payment covering 40%")
    assert "bureau" not in reasons and "has_bureau" not in reasons            # no bureau record: no bureau reason
    assert reasons["hardship_text"]["text"] == "The call mentions financial hardship"
    assert all(i["contribution"] > 0 for i in out["raises_risk"]) and all(i["contribution"] < 0 for i in out["lowers_risk"])
    assert out["advisory"] is True


def test_definitions_declare_directions_for_explained_features():
    specs = {f["name"]: f for f in store.feature_specs()}
    for name in ("prior_broken_in_case", "dpd_at_promise", "txt_hardship", "ptp_full_cover", "bureau_score"):
        assert specs[name].get("expect") in ("up", "down")
    assert ptp_risk.band(0.9) == "high" and ptp_risk.band(0.5) == "medium" and ptp_risk.band(0.1) == "low"


@pytest.mark.skipif(not ptp_risk.model_path().exists(), reason="model not trained (python -m c360.models.ptp_risk train)")
def test_live_score_on_real_data(real_data):
    import duckdb
    model = ptp_risk.load_model()
    if model["definitions_sha256"] != store.definitions_sha():
        pytest.skip("model trained on older feature definitions; retrain")
    assert not set(model["features"]) & {"txt_distress"}
    con = duckdb.connect(str(store.serving_db()), read_only=True)
    (ptp,) = con.execute("SELECT ptp_id FROM l2.promises_to_pay WHERE ptp_status = 'open' ORDER BY ptp_id LIMIT 1").fetchone()
    r = ptp_risk.score(ptp)
    assert 0 <= r["probability_not_kept"] <= 1 and r["band"] in ("low", "medium", "high")
    assert r["summary"].startswith(f"{r['probability_not_kept']:.0%} chance")
    json.dumps(r)                                                               # the API can return it as is
    batch_row = store.offline(ptp).get(ptp)
    if batch_row is not None and store.meta()["serving_file"] == store.serving_db().name:
        assert ptp_risk.explain(batch_row, model)["probability_not_kept"] == pytest.approx(r["probability_not_kept"])
    ev = model["evaluation"]["test"]
    assert ev["auc"] > 0.8                                                      # out-of-time, recorded at training
