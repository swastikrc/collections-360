# Feature store and promise risk model

This is the brief's **Layer 3 (Feature Store)**: "features from structured data and from text or voice, with one definition used for both training and live scoring". It also completes **Layer 4 (Models and Decisioning)** with a trained model whose decisions are explained and reviewed by a person.

## In one paragraph

Every feature is written once, as SQL, in [`c360/features/definitions.yaml`](../../c360/features/definitions.yaml). The store turns that file into a single SQL statement, and both of the following run it:

- the **batch build**, covering all 238,815 promises in 2 seconds, to produce the training table;
- the **live lookup**, covering one promise in about 0.1–0.5 s, at scoring time.

The only difference between the two is which promises are selected. A test checks 200 random promises on every run and finds batch and live values identical.

A logistic regression trained on the batch table predicts whether a promise to pay will be broken. It explains each score with exact per-feature contributions, written in plain English. The score appears in the agent terminal inside the next best action as advice, and a person approves, edits or rejects every action.

## What a feature is here

- **Entity:** one promise to pay.
- **Point in time:** the end of the call in which the promise was made. A feature may only use what existed then:
  - earlier contacts;
  - earlier promises whose grace period had already ended;
  - completed salary months;
  - bureau pulls on or before that day;
  - the note and transcript of the promise call itself.
- **Not used:** current-state tables such as `card_accounts`, `bureau_latest` and the case status, because for an old promise they would show what happened afterwards.
- **Proof of point-in-time correctness:** `tests/features/test_feature_store.py` adds a later contact, salary month, bureau pull and promise, and checks that a past promise's features do not change.

| Group | Source | Features |
|---|---|---|
| promise | structured | promised amount; cover ratio (promised / overdue); full cover; token payment; days to the due date; DPD; DPD of 120 or more; promise number in the case; promises already broken in the case; self-serve channel; instalment |
| promise_history | structured | the customer's earlier resolved promises; smoothed kept rate |
| contacts | structured | contacts and inbound contacts in the last 30 days; right-party contact rate over 90 days; hardship or dispute flagged in the last 90 days |
| income | structured | salary visible; months with a salary gap in the last 3; salary change last month |
| bureau | structured | bureau record available; risk score; change since the previous pull; utilisation |
| call_text | **text** | note or transcript present; hedged commitment; firm commitment; tied to payday; hardship language; asked for hardship options; dispute language; distress (**safeguard only**) |

The full list, with each feature's meaning, is in [`feature_catalogue.md`](feature_catalogue.md), generated from the definitions.

**Text features** are regular expressions over the agent note and transcript of the promise call, run inside the same SQL. Only 0/1 flags are stored, and no text leaves the serving database. They were checked against the 500 hand-labelled notes ([`outputs/features/text_feature_check.md`](../../outputs/features/text_feature_check.md)):

| Pattern | Precision | Recall |
|---|---:|---:|
| hardship | 93% | 97% |
| hedged intent | 89% | 100% |
| firm intent | 98% | 100% |
| dispute | 88% | 88% |

The patterns were tuned on those same 500 notes, so these figures are optimistic.

**Voice is not used.** `voice_samples.csv` covers only 2,000 calls and carries `customer_accent_region`, a proxy for national origin. The brief asks for text *or* voice, and text covers 82% of promises.

**What the features never use:**

- protected attributes;
- province;
- language;
- agent;
- vulnerability flags.

Everything comes from the governed Layer 2 serving database, which holds no names or contact details. A test fails if any feature's SQL mentions one of these columns. `txt_distress` is kept in the store and shown to the agent, but `use_in_model: false` keeps it out of the model.

## The model

- **Target:** the promise is broken or only partly kept (1), or kept in full (0). Open and cancelled promises have no label.
- **Evaluation fit:** trained on promises made up to March 2026, with regularisation tuned on April 2026, then tested **out of time** on May–August 2026. That period is a stress period: the break rate rises from 37% to 61%.
- **Production fit:** refitted on all 190,225 labelled promises made up to August 2026.

| Out-of-time test (62,583 promises) | Value |
|---|---:|
| AUC | **0.888** |
| Brier score | 0.124 |
| Mean predicted vs actual not-kept | 57.8% vs 60.8% |
| Low band (26.5% of promises): actually not kept | 8.1% |
| High band (58.4%): actually not kept | 84.9% |
| Gradient-boosted trees, same split (comparison only) | 0.936 |

**The cover ratio is U-shaped.** Promises that clear the whole overdue amount are almost always kept (2–5% broken). Small good-faith payments under 25% are usually kept. Part payments in between are usually broken (59–90%). A first straight-line model scored AUC 0.674. Adding the full-cover and token-payment flags *to the definitions* raised that to 0.888 while keeping every weight readable.

**Weights stay intuitive.** A feature with a known direction (for example, earlier broken promises raise risk) declares it with `expect`. A fitted weight with the opposite sign, usually a side effect of correlated features, is dropped and the model is refitted. Four features were dropped this way, and they are listed in the [model card](model_card_ptp_risk.md). This keeps the explanations from saying things like "earlier broken promises lower the risk".

**Explanations are exact.** Each feature's contribution to the log-odds is its weight × (value − mean) / sd. Related features are reported together as one reason; for example, the three cover features become "it is a part payment covering 38% of the overdue amount". Example from the live terminal:

> 79% chance this promise is not kept in full (high risk). Main reasons: it is a part payment covering 38% of the overdue amount; the account was 73 days past due; a weak bureau score (583).

## Decisions and human review (Layer 4)

`c360/layer3/nba.py` scores an open promise and uses the score **inside** the existing rules, never instead of them:

- **Rule order is unchanged:** legal holds, cease-contact, representatives, disputes, and hardship before any request for payment.
- **High risk turns the reminder into a call:** for a high-risk upcoming promise, the reminder is a **call** instead of an SMS, so the agent can check the amount is affordable and agree a smaller amount or a plan rather than let the promise break. A high score never leads to more pressure.
- **Safeguards are shown to the agent:** the model's signals for hardship (the brief's "flag financial hardship"), distress and dispute appear as safeguards.
- **No advice where it doesn't apply:** holds, letters-only cases and representatives get no risk advice.
- **The rules work without the model:** if the model is unavailable, the rules run unchanged, and `C360_L3_PROMISE_MODEL=off` switches it off.
- **A person decides:** the agent sees the band, the probability and the reasons in the next-best-action panel, then approves, edits or rejects. Nothing is sent until a person approves.

## Fairness

`python -m c360.models.fairness` produces [`fairness_report.md`](fairness_report.md). This is measurement only:

- Protected attributes are read from MongoDB `restricted_protected_attributes`, the only place they are kept.
- They are joined in memory to the out-of-time test predictions and never stored with the features or the model.

**Results:**

- **Gender, marital status, citizenship, newcomer status and accessibility needs:** the model behaves evenly. Calibration gaps range from −1.7 to −4.4 points, AUC from 0.86 to 0.91, and the high-band share ratio from 0.91 to 1.11.
- **One flag, customers with a recorded vulnerability:**
  - They are placed in the high band 1.27× as often, partly because they really do break more promises (72.5% vs 58.7%).
  - The model is also less accurate for them: AUC 0.855 vs 0.891, and 35% of their kept promises are placed in the high band vs 21%.
  - Mitigation: a high score only ever leads to supportive steps; vulnerable customers are routed to certified agents with the vulnerable-customer script; and a person approves every action.

## How to run

```bash
pip install -r requirements-features.txt
export MAPLE_DATA=$PWD/maple_collections_release
python -m c360.features build              # batch table -> warehouse/features/features.duckdb (+ feature_catalogue.md)
python -m c360.features get PTP-7011743    # live lookup, compared with the batch row
python -m c360.features validate-text      # text patterns vs the 500 labelled notes
python -m c360.models.ptp_risk train       # evaluation + production fit -> model card, warehouse/features/models/
python -m c360.models.ptp_risk score PTP-7011743
python -m c360.models.fairness             # -> fairness_report.md
pytest tests/features tests/layer3 -q
```

**Version guard:** the model records a fingerprint of the feature SQL it was trained on. If the definitions change, scoring refuses until the model is retrained, which prevents silent training and serving skew.

## Files

| File | What |
|---|---|
| `c360/features/definitions.yaml` | the one definition: spine, label, text patterns, 33 features in 6 groups |
| `c360/features/store.py` | SQL builder, batch build, live lookup, offline read |
| `c360/features/__main__.py` | command line, text pattern check, catalogue |
| `c360/models/ptp_risk.py` | training, sign-checked logistic regression, explanations, live scoring, model card |
| `c360/models/fairness.py` | fairness measurement |
| `c360/layer3/nba.py`, `web/terminal/terminal.js` | the score inside the next best action and the agent terminal |
| `tests/features/*`, `tests/layer3/test_nba_promise_risk.py` | point in time, batch == live, signs, explanations, integration |
