# Layer 2 changelog

Built on the development machine against synthetic data, following `docs/SPEC_LAYER2.md`.
Each phase lists what was built, what was assumed, and what is known to be uncertain.
The DB machine adds its own section at the end (see `docs/DB_MACHINE_RUNBOOK.md`).

## Environment

- Python 3.11.14 in `.venv` (the machine's system Python is 3.13 and had none of the dependencies).
- Versions pinned in `requirements-layer2.txt`: duckdb 1.5.6, pymongo 4.18.2, mongomock 4.3.0,
  google-genai 2.28.0, sqlglot 30.21.0, pyyaml 6.0.3, pydantic 2.13.5, pytest 9.1.1, pandas 3.0.6, numpy 2.4.6.
- `GEMINI_API_KEY` was **not set** on this machine during the build. See "Live Gemini status" at the end.

## Phase 0 — Skeleton

Built
- Package layout under `c360/layer2/`, `tests/layer2/`, `requirements-layer2.txt`.
- `c360/config.py`: `WAREHOUSE` now honours `C360_WAREHOUSE` (the only edit to that file).
- `run_layer1.py`: new last step `publish_l2` (also `--only publish_l2`).
- `c360/layer2/config.py`: all settings read from the environment at call time.
- `c360/layer2/deny_list.py`: `golden.PROTECTED_COLUMNS` plus the spec's anchored personal-data patterns.

Assumed
- `Settings.source_db` is `<C360_WAREHOUSE>/c360.duckdb`, i.e. the same file as `c360.config.C360_DB`.

Deviations
- The deny-list is slightly **stricter** than the spec: it also blocks `city`, `fsa`, `geo_lat*`, `geo_lon*`,
  which the data contract lists as proxies for protected attributes. `province_code` stays available.

## Phase 1 — Expected schema and synthetic world

Built
- `schema/expected_schema.yaml`: every table and column of Appendix A, each marked confirmed / protected / inferred.
- `python -m c360.layer2.synth`: 2,000 people in about 4 seconds. The curated tables are typed and flagged by
  **Layer 1's own code** (`curated._cast_expr`, `curated.DERIVED`, `normalize` macros), and the MongoDB documents
  are assembled by **Layer 1's own `golden.build_golden()`**, then exported to `customers.jsonl`. So the document
  shape is the real one, not an imitation.
- `synth/truth.py`: expected answers computed with pandas from the curated tables, with explicit dates, without
  touching the serving DB, `metrics.yaml`, `compute_metric` or `periods.py`.
- `synth/questions.py` → `eval/questions/synthetic.yaml`: 38 questions (26 governed metric, 8 custom SQL,
  2 single-customer, 2 text).
- All traps planted: ~5% double-loaded contacts, stored bucket ≠ recomputed bucket, protected and personal
  columns in `curated.customers`, two groups with fewer than 5 cases (`queue = insolvency`,
  `hardship_reason = bereavement`), superseded model scores, shifted bureau rows, repaired and unmatched case
  links, three prompt-injection strings in notes and one in a transcript, ~8% of transcript files missing,
  the `channel` → `channel_v2` rename.

Assumed
- Month-end snapshots are the 12 month-ends from 2025-09-30 to 2026-08-31 (the last complete month before the
  snapshot date). The real table may include a September row.
- Transcript JSON is `{"transcript_id", "language", "turns": [{"speaker", "text"}]}`. The real format is unknown.
- Columns of the reference tables are the spec's guesses.

Safety
- The generator refuses to write into a directory that holds a `c360.duckdb` it did not create (it leaves a
  `SYNTHETIC` marker file), so it cannot overwrite a real warehouse.
- Its default output directory is `$C360_WAREHOUSE` when set, otherwise `./warehouse_synth` — never `./warehouse`.

## Phase 2 — Serving database

Built
- `l2_views.yaml`: 22 serving tables, each an explicit allow-list (`{cols}` templating; no `SELECT *` anywhere).
- `publish.py` (`python run_layer1.py --only publish_l2`): attaches `c360.duckdb` READ_ONLY, runs the doctor,
  materialises schema `l2`, projects `customer_summary` from MongoDB, parses transcript files, builds the
  full-text index, writes `meta.build` / `meta.columns` / `meta.row_counts`, runs the deny-list check on the
  finished file, then renames `.tmp` → `.duckdb` and switches `CURRENT` atomically. Keeps current + one previous.
- `serving.py`: opens `CURRENT` read-only, then `SET enable_external_access = false` and
  `SET lock_configuration = true`; queries run in a worker thread and are interrupted on timeout.
- `transcripts.py`: tolerant transcript JSON → text.
- Synthetic build: 22 tables, 61,737 rows, 11 MB, about 2 seconds.

Assumed
- `golden.account_xref` has exactly one `role = 'primary'` row per account (otherwise account tables would
  fan out). A test asserts this on synthetic data; **check row counts on the real data** (runbook Step 4).
- `l2.contact_history.channel` is `curated.contact_history.channel_std` (the `channel` / `channel_v2` rename
  already resolved). The raw `channel` and `channel_v2` columns are not copied.
- `l2.bureau_latest` keeps one row per person (or per CIF when the CIF is not linked to a person).

Deviations
- The spec says `code_lookups` and `metric_definitions` should "copy all columns", but also that the allow-list
  must be the only way a column enters the serving DB and that there is never a `SELECT *`. The safer rule won:
  both tables have explicit (guessed, optional) column lists. The doctor lists the real columns for the DB machine.
- Not copied although harmless: `entity_id` and `crm_customer_id` on `model_scores`, `voice_sample_id` and
  `file_path` on `call_transcripts`, and every source-system customer key (`src_customer_ref`, `cif`,
  `co_borrower_ref`, `joint_holder_ref`, `coll_customer_ref`, `crm_customer_id`). `has_joint_holder` was added
  next to the spec's `has_co_borrower`.
- New optional env var `C360_L2_OUT` (default `outputs/layer2`) so tests don't write reports into the repo, and
  `C360_L2_TEXT_MODE=like` to force keyword search.

Uncertain
- `preferred_language` is allow-listed in `l2.customers` as the spec asks, but the data contract lists it as a
  proxy that must not drive decisions. It is fine for reporting; a human should confirm.

## Phase 3 — Doctor

Built
- `python -m c360.layer2.doctor` → `outputs/layer2/doctor_report.md` and `.json`, exit code 1 on any ERROR.
  Nine checks in the spec's order. `publish_l2` runs the same checks and refuses to publish on an ERROR.
- Synthetic database: 0 errors, 0 warnings.

Decisions
- A missing **confirmed** column is an ERROR only when a required, non-optional serving column (or a join)
  needs it; otherwise it is a WARNING. This keeps an unused column from blocking the publish.
- Deny-list check covers the source expression as well as the L2 name, so a personal column cannot be
  allow-listed under a harmless alias (for example `contact_number: c.primary_phone_std` is an ERROR).
- Unexpected source columns are only listed (and flagged when they match the deny-list). Nothing is auto-added.

## Phase 4 — Semantic layer and tools

Built
- `semantic/semantic.yaml`: 22 tables with column descriptions and value lists, join paths, business terms,
  rules, the not-allowed list from the data contract.
- `semantic/metrics.yaml`: the 12 placeholder metrics. **All 12 agree with the independent pandas truth** for a
  scalar, a grouping and (for several) a filter.
- `periods.py`: every form in spec 7.6 plus a few tolerant extras (`Q3 2026`, `2026-08`, `yesterday`, ...).
- `tools/sql_guard.py`: rules R1–R6 and traps T1–T5 (catalogue in `sql_guard.TRAPS`); 110 unit tests.
- `sanity.py`, the five tools, `tools/registry.py` (JSON-schema tool declarations and a dispatcher that never raises).
- `compute_metric` binds every value as a parameter; the SQL shown to the user has the literals inlined and runs as-is.

Assumed (each is a real-data question for the DB machine)
- **"last N days" is the N days ending on the snapshot date, inclusive** (`last 90 days` = 1 Jul–28 Sep 2026, as in
  the spec's example output). Layer 1's `golden.py` counts back from the snapshot date instead (30 Jun–28 Sep).
  Switch with `periods.last_n_days: exclusive` in `semantic.yaml` if the official definitions do that.
- Rate metrics leave out promises that are not yet due (`ptp_status = 'open'`).
- `roll_rate`: stored buckets; an account "rolls" when its bucket at the next month-end is worse; accounts with a
  missing bucket on either side are left out; product is read from the account id prefix (`C-` card, `L-` loan).
- `rpc_rate` = right-party contacts ÷ outbound non-system contacts, duplicates excluded.
- `open_case_count`, `past_due_amount`, `average_dpd` describe cases open **now**; a period filters on the case
  open date and defaults to all time.

Decisions
- `sql_guard` uses an allow-list for functions sqlglot has no class for, plus a deny-list by name for everything.
  A new harmless function may need adding to `_SAFE_ANONYMOUS`.
- R4 is stricter than the spec: `SELECT *` is refused on **every** row-level table (the spec says person-grain and
  account-grain), and selecting a whole row through a table alias (`SELECT c FROM l2.customers c`) is refused too.
- R5: `note_text` / `transcript_text` may appear in a `WHERE` or `FILTER` predicate (so "how many notes mention X"
  can be counted) but can never be selected, grouped or sorted.
- Governed-metric SQL is validated too (defence in depth) but the T5 hint is not shown for it.
- `search_text` takes its filters as flat arguments (`case_id`, `c360_id`, `period`, `queue`, `product`, `source`)
  because Gemini handles flat schemas better; the nested `filters` form is accepted as well. With no keywords but
  a `case_id` or `c360_id`, it lists that customer's most recent notes.
- `get_customer` also drops `interactions[].payload` (free text written back by Layer 3) and caps transcripts
  and interactions at 5.

Uncertain
- `get_customer` keeps the customer's **name** and **employer name** in the trimmed document, because the spec's
  drop list does not include them. They are sent to the LLM. A human should decide whether to drop them.

## Phase 5 — LLM, agent, guards, CLI

Built
- `llm/base.py` (provider interface), `llm/gemini.py` (google-genai), `llm/fake.py` (scripted fakes).
- `prompts/system.md` and its renderer: tables, joins, terms, rules, codes, metric catalogue, routing guide,
  not-allowed list, answer schema. About 37k characters on the synthetic serving database.
- `answer.py`, `guards.py`, `agent.py`, `audit.py`, `ask.py` (`--json`, `--trace`, `--repl`).
- Offline acceptance: every item in spec section 11 Phase 5 has a test with the fake LLM (budget stop, duplicate
  call, two consecutive errors, validator rejection then corrected call, refuse, JSON repair then fallback,
  redaction, ungrounded number, sources hold every executed SQL, audit without result rows).

Decisions
- When the budget is reached in the middle of a turn, every remaining tool call of that turn gets a
  "not executed" reply before the forced final call (Gemini needs one response per function call).
- A forced stop (budget or errors) downgrades `answered` to `partial` and adds a warning.
- An empty or over-long question comes back as a refusal with rule `invalid_input` (no model call).
- A model error (safety block, exhausted retries) gives a `partial` answer with a warning; sources already
  gathered are still listed.
- `SourceRef` has an extra `sql` field so "How we got this" can print the SQL.
- Tool calling is disabled for forced-final and repair turns by sending `mode = NONE` while keeping the
  declarations, so the earlier function calls in the conversation stay valid.
- Assistant turns are replayed to Gemini verbatim (`Message.raw`) to keep thought signatures intact.
- Number grounding skips whole numbers up to 10, and any digits that appear in the question, the tool
  arguments or a result string (ids, dates, bucket labels).

Uncertain
- The Gemini provider was only exercised against a stub client built from the SDK's own types. The request
  shape (`parameters_json_schema`, `ToolConfig(mode=NONE)`, function-response ids) follows the installed SDK
  (google-genai 2.28.0) but has **not been run against the live API**. If Gemini rejects a tool schema, simplify
  that tool's schema in `tools/registry.py` (runbook, Troubleshooting).
- The input guard's patterns are conservative on purpose. On the refusal set it catches 5 of 16 questions on its
  own; the other 11 rely on the model calling `refuse`.

## Phase 6 — Evaluation

Built
- `eval/harness.py`, `compare.py`, `loaders.py`, `python -m c360.layer2.eval`.
- Question sets: `synthetic.yaml` (38, generated), `heldout.yaml` (20), `paraphrase.yaml` (22 rewordings in
  10 groups), `refusal.yaml` (16). Held-out expected answers are resolved from `truth.py` at load time.
  The held-out set was written before any tuning and has not been used for tuning (no tuning has happened).
- `load_benchmark()`: detects the question / answer / gold SQL / id / category columns, parses numbers,
  percentages, lists and small tables, runs gold SQL read-only on the source database, falls back to manual
  scoring. The synthetic `benchmark_questions` table has one row of each shape.
- `--fake`: a scripted fake LLM plays each question's intended tool call and reports the result.

Scores (fake LLM: proves the pipeline, says nothing about the model)

| Set | Result |
|---|---|
| synthetic | 37 / 37 auto-scored, 1 manual |
| heldout | 20 / 20 |
| paraphrase | 22 / 22, 10 of 10 groups consistent |
| refusal | 16 / 16 (5 by the input guard, 11 by a scripted `refuse`) |
| benchmark (synthetic rows) | 1 / 1 scored, 5 skipped (no script) |

No answer in any set contained a phone number, an email or a deny-listed field. Average tool calls 1.0.

**Live targets not measured.** Synthetic ≥ 90%, held-out ≥ 80%, paraphrase ≥ 85%, refusal = 100% and average
tool calls ≤ 2.5 all need a real model. See "Live Gemini status".

## Phase 7 — Documentation and hand-off readiness

Built
- `docs/layer2/README.md`.
- `docs/DB_MACHINE_RUNBOOK.md`: a copy of the runbook, so it travels with the repository.
- Dry run in a fresh shell (`env -i`, new virtualenv, **system Python 3.13**): `pip install -r requirements-layer2.txt`,
  synth, `publish_l2`, doctor (exit 0), `pytest tests/layer2 -q -m "not live"` (all passing), `eval --set all --fake`.
  The same suite passes on Python 3.11.
- Runbook Steps 1–3 dry run on the synthetic database: Step 1 passes, Step 2 exits 0 with no errors or
  warnings, so Step 3 had nothing to fix.

Final test count: 495 offline tests passing, 5 live tests skipped (no key).

## Live Gemini status (action needed)

`GEMINI_API_KEY` was not available on the development machine, so these are **not done**:

1. Choosing the model: run `python -m c360.layer2.llm.gemini`, pick the strongest Pro-tier model with function
   calling, export it as `C360_L2_MODEL`, and **record the exact name here**.
2. The 5 live smoke tests: `pytest tests/layer2 -q -m live`.
3. Phase 6 live runs and tuning: `python -m c360.layer2.eval --set all`, then iterate on `semantic.yaml`,
   `metrics.yaml`, the prompt and the trap rules with general changes only, and record the final scores here.

Everything needed for those three steps is in place; they need only the key.

### First live attempt (2026-10-04, free-tier key)

- The key is valid: `python -m c360.layer2.llm.gemini` lists the models.
- `gemini-3.1-pro-preview` (the strongest Pro model on the list): every call returns HTTP 429. The free tier has
  **no quota** for it. `gemini-2.5-pro` and `gemini-2.5-flash` return 404 (closed to new users).
- `gemini-3.8-flash` works, including function calling: it stated a plan and called
  `compute_metric(metric=promise_kept_rate, period="August 2026")` correctly. Its free tier allows
  **20 requests per day**, which ran out during the smoke tests.
- Live smoke tests with `gemini-3.8-flash`: 2 of 5 passed (custom SQL route, text route). The other 3 stopped on
  the quota error, not on a code or model mistake; each came back as a `partial` answer with the 429 in its warnings.
- So the live request path (tool declarations, function calls, function responses, final JSON) is confirmed
  working for two full questions. **No model has been chosen yet** and no evaluation scores exist: both need a
  key with paid quota (one question costs 2-4 requests of about 10k input tokens each; the full evaluation is
  roughly 100 questions).

## Deviations from the spec (summary)

- Deny-list also blocks `city`, `fsa`, `geo_lat*`, `geo_lon*` (stricter).
- `code_lookups` and `metric_definitions` use explicit optional column lists instead of "copy all columns".
- `SELECT *` is refused on every row-level table, not only person- and account-grain ones (stricter).
- `search_text` filters are flat arguments (the nested `filters` object is accepted too).
- `SourceRef.sql`, `C360_L2_OUT`, `C360_L2_TEXT_MODE`, `synth/questions.py`, `schema_util.py` and
  `prompts/__init__.py` are additions the spec's layout does not list.
- Phase 5 live acceptance and Phase 6 live targets are outstanding (no API key).

## DB machine

Adapting Layer 2 to the real data, following `docs/DB_MACHINE_RUNBOOK.md`. Machine: the Layer 1 build
machine (Fedora 42, 22 cores, 30 GB RAM). Layer 1 was not rebuilt.

### Preconditions (2026-10-04)

- Python 3.13.13 (system). All ten packages in `requirements-layer2.txt` were already installed at the pinned
  versions, so `pip install` changed nothing.
- `warehouse/c360.duckdb`: 5.3 GB, last written 2026-10-03 20:30 (the Layer 1 build). No Layer 1 or other
  DuckDB process running.
- MongoDB `collections360`: reachable after `podman start c360-mongo` (started by the user);
  `customers` = 1,000,008 documents.
- `MAPLE_DATA` exported explicitly as `<repo>/maple_collections_release` (the same path `c360.config` falls back to);
  transcript files are under `files/transcripts/<year>/`.
- `GEMINI_API_KEY` and `C360_L2_MODEL` are loaded from `~/.config/c360/layer2.env` (mode 600, outside the repo).
  The key is never printed. `C360_L2_MODEL` is currently `gemini-3.8-flash`, which is **not** Pro-tier; to be
  checked before Step 7.
- Both Layer 1 edits Layer 2 depends on are present: `c360/config.py` line 9 (`C360_WAREHOUSE` override) and
  `run_layer1.py` (`publish_l2` as the last step).
- Found on arrival: a serving database `warehouse/l2/l2_20261004T064849.duckdb` (767 MB, 12:19 local) with
  `CURRENT` pointing at it, plus `outputs/layer2/doctor_report.*` from the same time. Not created in this session.
  Step 4 builds a fresh one; it is not relied on.

### Step 1 — Transferred code on synthetic data

- What changed: nothing.
- Evidence: `pytest tests/layer2 -q -m "not live"` → **495 passed, 5 deselected** (the live tests) in 37 s on
  Python 3.13 / duckdb 1.5.6. No version fixes needed.
- Confirmed the run did not touch the real warehouse: `c360.duckdb`, `warehouse/l2/CURRENT` and the existing
  serving file kept their timestamps.

### Step 2 — Doctor on the real data

- What changed: nothing (read-only run).
- Evidence: `python -m c360.layer2.doctor` → exit 0, **0 errors, 20 warnings, 108 notes** (1.3 s).
  All 16 required serving tables bind; MongoDB reachable (1,000,008 customers); FTS extension available.
- Warnings, to be fixed or explained in Step 3:
  - 9 inferred columns missing, each reported twice (schema + views): `account_monthly_snapshot.{product_family,
    past_due_amount, credit_limit}`, `metric_definitions.definition`, `code_lookups.description`,
    `hardship_programs.{product, max_duration_months}`, `agents.{team, skill}`.
  - `golden.source_customer_xref.top_candidates` is a list of structs, expected string (not copied to L2).
  - Transcripts: 0 of 20 sampled files exist. `file_path` (`transcripts/2026/09/<id>.json`) is relative to
    `<dataset>/files/`, not to the dataset root.
- Also noted: `curated.benchmark_questions` has columns `question_id, question_text, difficulty, metric_id,
  as_of_date, tolerance, split` — **no expected answer and no gold SQL** in the database.

### Step 3 — Schema mismatches fixed (config first)

Result: `python -m c360.layer2.doctor` → exit 0, **0 errors, 0 warnings** (was 20 warnings), 108 notes.
`pytest tests/layer2 -q -m "not live"` → **524 passed** (495 + 29 new).

Config changes (`l2_views.yaml`, `expected_schema.yaml`, `semantic.yaml`; column descriptions copied from
`curated._column_docs`, i.e. the official `schema.json`):
- **Guessed columns replaced by the real ones** (all six tables now `confidence: confirmed`):
  - `account_monthly_snapshot`: `product_family`/`past_due_amount`/`credit_limit` do not exist. Real columns added:
    `product_type, account_status, balance, limit_or_original_amount, minimum_due, payment_made, overlimit_flag,
    write_off_flag, salary_credit_amount`. **Finding:** the table holds **quarter-end** snapshots only (11 dates,
    2023-12-31 … 2026-06-30), for every product including deposits, not 12 month-ends of cards and loans. Descriptions
    corrected; the placeholder `roll_rate` (consecutive month-ends) is dealt with in Step 5.
  - `metric_definitions`: `definition` → real `business_definition` plus `grain, time_basis, default_filters, unit,
    good_direction, synonyms, source_tables, example_question, certified_flag`.
  - `code_lookups`: L2 column `description` now reads `description_en`; added `description_fr, attributes_json,
    active_flag, effective_from`. (L2 name kept so the prompt's code rendering is unchanged.)
  - `hardship_programs`: `product` → `product_types` (list), `max_duration_months` → `duration_months`; all 21 real
    columns (eligibility rules, payment reduction, versions). Grain is one row per programme **version**.
  - `agents`: `team`/`skill` → `team_id`, `role`, `skill_*` (1–5 scores), `languages`, `shift_pattern`, capacity,
    `status`. **Left out on purpose:** `agent_display_name` (a name), `hire_date`, and individual performance and HR
    fields (`*_rate_90d`, `qa_score_avg_90d`, `complaints_90d`, `coaching_flag`, `fte`). Conservative; a human can
    widen it.
  - `case_assignment_history`: all real columns except `assignment_reason` (one of its values is `vulnerability`, which
    would reveal a protected attribute of the customer).
- **Unexpected columns reviewed** and the clearly useful, non-personal ones allow-listed (`optional: true`), e.g.
  `contact_history.{within_permitted_hours_flag, contact_cost_cad, agent_site, test_cell, strategy_code, ptp_made_flag,
  dial_result, system_event_type, ops_incident_id, dpd_at_contact, bucket_at_contact, …}`,
  `collections_cases.{province_code, test_cell, assignment_method, strategy_code, cure_date, days_to_cure,
  promises_*, dispute_*, complaint_*, contact caps, …}`, `promises_to_pay.{prior_broken_ptp_count, ptp_type, product,
  agent_id, paid_date, …}`, `offers.{decline_reason, eligibility_result, nba_recommended_flag, …}`, plus account,
  bureau-history, model-score and transcript metadata. Not added: month-series columns (`*_m01..m12`), free-text
  message bodies, load metadata, `interpreter_used_flag`, `agent_coded_sentiment`, `code_switch_flag`.
- `golden.source_customer_xref.top_candidates`: expected type corrected to `list` (not copied to L2).
- **Transcripts:** `file_path` is relative to `<dataset>/files/`. New `file_text.base_dirs: [files, ""]` in
  `l2_views.yaml` (folders tried in order; `""` keeps the synthetic layout working). Doctor: 20 of 20 sampled files
  found and parsed. Real JSON has `turns: [{speaker, text, …}]`; `transcripts.parse()` needed no change.

Code changes (config could not express them):
- `transcripts.resolve()` + its use in `publish.create_file_text_tables()` and the doctor (reads `base_dirs`).
- `deny_list.py`: **stricter** — new `REAL_DATA_PATTERNS` for real columns the patterns missed:
  `agent_display_name`, `name_suffix`, `counterparty_name*`, customer vulnerability (`vulnerable_customer_flag`,
  `vulnerability_*`), accent (`*accent*`), free-text message bodies (`sms_text`, `sms_reply_text`, `email_subject`),
  `fsa_income_index`. Agent training flags (`vulnerable_customer_certified`, `certified_for_vulnerable_flag`) stay allowed.

Tests and synthetic world:
- New tests: transcript structure sample (real keys, invented text) and `base_dirs` resolution; blocked/allowed
  examples for the new deny patterns.
- Synthetic generator now writes the real column names for the six tables (same number of random draws, so every
  synthetic expected answer is unchanged). Three tests updated for the renamed columns (`agents.team` → `team_id`,
  `product_family` → `product_type`, doctor message "inferred" → "confirmed column missing"; one test's made-up
  extra column renamed because `shift_pattern` is now real). No assertion was weakened.

For a human decision (not changed): call transcripts contain customers' **names in the spoken text** (e.g. the
agent asking for the customer by name); `search_text` snippets carry it to the model. Same question as the open
`get_customer` name decision.

### Step 4 — Real serving database

- Command: `python run_layer1.py --only publish_l2` → `warehouse/l2/l2_20261004T072831.duckdb`, `CURRENT` switched.
  **60 s**, 22 tables, 34,358,346 rows, **1,096.6 MB**, `text_search_mode = fts`. Deny-list check on the finished
  file passed (publish aborts otherwise). The 12:19 file found on arrival is kept as the one previous build.
- `meta.build`: snapshot 2026-09-28, `pipeline_version` layer1-1.0.0, source mtime 2026-10-03 20:30:04, l2 layer2-1.0.0.
- Row counts: every L2 table equals its curated source exactly (0.000% difference), so `account_xref` has one primary
  row per account on the real data (no fan-out). `customers` and `customer_summary` = 1,000,008 (= people).
  `bureau_latest` = 999,978 vs 1,000,000 pulls: one row per person, and 22 people have two linked CIFs.
- `c360_id` coverage: customers, deposit accounts, salary history, model scores 100%; snapshots 99.9999%;
  loans 99.9963%; card accounts 99.9936%; case assignments 99.9939%; cases 99.9962%; promises 99.9929%;
  agent notes 99.9921%; offers 99.9902%; transcripts 99.9840%; contacts 99.9589%. Bureau latest 98.32% and bureau
  history 98.31%: 16,825 bureau CIFs link to no person, 16,433 of them no-hit / thin-file pulls with no reported
  identity (a known Layer 1 limitation, DQ-VAL-11).
- Transcript text: 25,000 of 25,000 transcripts have parsed text.

### Step 5 — Official metric definitions

`metrics.yaml` rewritten from `curated.metric_definitions` (15 official metrics). Each translation follows the
official business definition, formula, time basis and default filters; every official metric is now
`status: verified` with `defined_in: metric_definitions:<id>` and the official synonyms as `aliases`.

| Official metric | Template (L2) | Choice where the official text leaves one open |
|---|---|---|
| promise_kept_rate | kept / (kept + partially_kept + broken), due date in period | — (open and cancelled excluded, as written). New dimensions: test_cell, assignment_method, agent_site, prior_broken_promises, ptp_type |
| broken_promise_rate | broken / resolved, due date in period. Old key `promise_broken_rate` kept as an alias | — |
| cure_rate | cases open at period start, cured (cure_date) within the period; insolvency and deceased holds excluded | "open at the start" = opened on or before day 1 and not closed before it |
| roll_rate | accounts with a bucket at the snapshot on/before period start → worse bucket at the snapshot on/before period end; charged-off and insolvency-hold accounts excluded; stored buckets | data has **quarter-end** snapshots only, so a period inside one quarter returns nothing; accounts missing at the end count as not rolled (literal formula) |
| rpc_rate | rpc / outbound **call** attempts (placeholder used all outbound non-system contacts) | duplicates excluded (semantic rule 2) |
| ptp_rate | ptp_made_flag / right-party contacts | duplicates excluded |
| cost_per_contact | sum(contact_cost_cad) / contacts, system events excluded | duplicates excluded |
| cost_per_rpc | sum(contact_cost_cad) / right-party contacts, system events excluded | duplicates excluded |
| collected_amount | sum(promises_to_pay.amount_paid_against) by paid_date | the only payment-level data in the listed source tables |
| hardship_rate | hardship_flag / cases, case open date in period | — |
| delinquency_rate | dpd ≥ 1 / active card + loan accounts | active = on the current snapshot (cards: active or blocked; closed and charged-off rows are stale snapshots); accounts with no dpd left out |
| write_off_rate | charge-off date in period / all card + loan accounts | — ; plus `write_off_count` and `write_off_amount` (numerator and its amount) |
| average_dpd | avg(current_dpd), open cases | unchanged from the placeholder (matched) |
| contact_attempts_per_case | outbound attempts / distinct cases | duplicates excluded; `contact_attempts` (numerator) kept |
| payroll_delay_share | distinct customers with chequing payroll > 7 days late / distinct customers with chequing payroll, current snapshot | grain customer → distinct `c360_id` |

Kept as `status: placeholder` (no official counterpart, tests and prompt depend on them): `promise_count`,
`payment_on_contact_amount`, `hardship_offer_acceptance_rate`, `open_case_count`, `past_due_amount`.

Hand-check on the real serving database: `compute_metric` (one period + one grouping each) vs independently written
SQL that follows the definition literally (`outputs/layer2/metric_hand_check.json`). **All 18 agree exactly**; rates
in [0, 1], counts and amounts plausible against table sizes.

  | Metric | Period | compute_metric | hand SQL | |
  |---|---|---|---|---|
  | `promise_kept_rate` | Q2 2026 | 0.526927 | 0.526927 | agree |
  | `broken_promise_rate` | Q2 2026 | 0.438836 | 0.438836 | agree |
  | `cure_rate` | August 2026 | 0.204752 | 0.204752 | agree |
  | `roll_rate` | Q2 2026 | 0.0552548 | 0.0552548 | agree |
  | `rpc_rate` | August 2026 | 0.438216 | 0.438216 | agree |
  | `ptp_rate` | August 2026 | 0.315321 | 0.315321 | agree |
  | `cost_per_contact` | August 2026 | 1.05578 | 1.05578 | agree |
  | `cost_per_rpc` | August 2026 | 3.98645 | 3.98645 | agree |
  | `collected_amount` | August 2026 | 2.81664e+06 | 2.81664e+06 | agree |
  | `hardship_rate` | year to date | 0.220574 | 0.220574 | agree |
  | `delinquency_rate` | (snapshot) | 0.115966 | 0.115966 | agree |
  | `write_off_rate` | last 12 months | 0.000790042 | 0.000790042 | agree |
  | `write_off_count` | last 12 months | 841 | 841 | agree |
  | `write_off_amount` | last 12 months | 1.11414e+07 | 1.11414e+07 | agree |
  | `average_dpd` | (snapshot) | 56.1478 | 56.1478 | agree |
  | `contact_attempts_per_case` | August 2026 | 3.63402 | 3.63402 | agree |
  | `contact_attempts` | August 2026 | 247426 | 247426 | agree |
  | `payroll_delay_share` | (snapshot) | 0.0819641 | 0.0819641 | agree |

`semantic.yaml`:
- 57 value lists replaced with the **real distinct values** (the dev build's lists were synthetic, e.g. queue had
  `legal`/`insolvency`, primary_product was `card | loan`, outcome codes were wrong).
- `terms` rewritten against real data (24): resolved promise, self-serve promise, active account, charged off,
  call attempt, outside permitted hours, dispute, randomised cases, customer province; "unsecured loan" verified
  (`secured_flag = false` = personal_loan, unsecured_loc, student_loc); roll rate notes quarter-end snapshots;
  "contact attempt" = outbound (system events are `direction = internal` in the real data).
- `code_columns` adds `product_code` and `strategy_code` (codes rendered from `l2.code_lookups`).

Prompt size: the Step 3 additions pushed the system prompt over its 60,000-character budget (test). Rather than
raise the budget, 121 low-value extra columns added in Step 3 were removed again from all three config files
(e.g. contact delivery flags, day-7/14/30 checkpoints, talk ratios, cost estimates, strategy_version). Real prompt
62,957 → **56,117** characters. Serving DB rebuilt: `l2_20261004T074419.duckdb`, 988.6 MB, 49 s.

Synthetic world and tests (so the offline suite keeps proving the templates):
- `truth.py`: independent pandas versions of every official definition (cure, roll, rpc, ptp, costs, collected,
  hardship, delinquency, write-off rate/count/amount, attempts per case, payroll delay share).
- Generator: `cure_date`, `paid_date`, `contact_cost_cad`, `ptp_made_flag` derived from values already drawn (no new
  random draws); synthetic system events now have `direction = internal` as in the real data.
- Synthetic / held-out / paraphrase questions reworded where the old definition no longer applies (S06, S07, S10,
  S12, S26, H03, P06a/b: "cases closed in…", RPC rate "by channel", roll rate "by product" → product_family).
- Tests: metric catalogue lists all 23 metrics and checks verified vs placeholder; labels for verified metrics no
  longer say "(placeholder definition)" (a placeholder metric is checked to still say it); two tolerances got
  `abs=1e-6` like the other metric tests (truth rounds to 6 digits); prompt test needles updated.
- `pytest tests/layer2 -q -m "not live"` → **546 passed**. Doctor → 0 errors, 0 warnings.

Open decision — "last N days" (not changed; currently inclusive, 1 Jul–28 Sep for 90 days). Evidence: no official
metric defines a rolling N-day window (time bases are calendar month, period of due/contact/open date, or snapshot);
the benchmark questions use explicit ranges ending on the snapshot day ("in the 12 months to 28 September 2026",
"July–September 2026"), i.e. **end-inclusive at the snapshot date**, which is what the inclusive setting does.

### Step 6 — Real benchmark questions

- `curated.benchmark_questions` (35 rows: 21 `dev`, 14 `test`) has `question_id, question_text, difficulty,
  metric_id, as_of_date, tolerance, split` — **no answer and no gold SQL**. The expected answers for the 21 dev
  questions are in the release file `labels/benchmark_dev_answers.csv` (`gold_answer, gold_sql_reference,
  gold_sources, trap_notes`); test answers are hidden.
- `eval/loaders.py` (harness only; nothing reaches the agent, the prompt, semantic files or code):
  - merges expected answers by `question_id` from `benchmark_answers_path()` — `<dataset>/labels/benchmark_dev_answers.csv`,
    override `C360_L2_BENCHMARK_ANSWERS`;
  - parses the real formats: small CSV tables with a header (key = first column, value = first numeric column after
    it), single-value tables (`<metric>_pct` / `35.1`), `Refuse: …`, free text → manual;
  - per-question tolerance from the `tolerance` column: `±0.5 pts` on a `*_pct` column = ±0.005 as a ratio,
    `±0.1 days`, `±1 CAD`, `exact`; `direction of effect` → no numeric tolerance;
  - **precedence changed:** a stated answer now wins over gold SQL (gold SQL runs only when there is no stated
    answer). The labels file's gold SQL targets raw table names and would fail or differ on the curated database;
    the stated answer is the official expected result. One dev test updated to the new precedence.
  - questions are tagged `split:dev` / `split:test`.
- Parsed: 15 dev questions auto-scorable (11 tables, 1 scalar, 3 refusals); 6 dev questions free text (manual:
  policy answers and call summaries); 14 test questions manual (answers hidden).
- New tests: answer-format parsing with invented values; labels-file merge on the synthetic table.
  `pytest tests/layer2 -q -m "not live"` → **548 passed**.

Classification of the 35 questions (route the question needs):

| Route | Questions | Count |
|---|---|---:|
| governed metric | BQ-003, 004, 007, 011, 012, 013, 015, 017, 025, 027, 028, 032, 034, 035 | 14 |
| custom SQL | BQ-006, 008, 010, 014 | 4 |
| call transcript (text) | BQ-016, 021, 022 | 3 |
| policy / reference document | BQ-001, 002, 005, 009, 019, 020, 026, 030, 033 | 9 |
| refusal | BQ-018, 023, 024, 029, 031 | 5 |

Most used: promise rates (5), contact metrics (4), case metrics (4). Gap found: the serving database has **no
reference-document text** (policies, procedures, FAQ, incident reports) and no `ops_events`, so the 9 policy /
document questions cannot be answered yet — the runbook's "question needs a table not in L2" cause, handled in Step 7.

### Before Step 7 — model check (STOPPED)

- `python -m c360.layer2.llm.gemini` lists three Pro-tier models: `gemini-3.1-pro-preview`, `gemini-pro-latest`,
  `gemini-2.5-pro`. One-request probe with this key (2026-10-04):
  - `gemini-3.1-pro-preview` → **HTTP 429** (quota exceeded)
  - `gemini-pro-latest` → **HTTP 429** (quota exceeded)
  - `gemini-2.5-pro` → HTTP 404 (closed to new users)
  - `gemini-3.8-flash` (the configured `C360_L2_MODEL`, Flash tier) → works.
- Per the instructions ("if calls return HTTP 429, stop; do not switch to a weaker model"), Step 7 (live evaluation),
  the live smoke tests (`pytest -m live`) and everything after them were **not run**. No model has been recorded as
  chosen. Waiting for a decision: a key with Pro quota, or explicit approval to use `gemini-3.8-flash`.
- **Model decision (user, 2026-10-04): run on `gemini-3.8-flash`** (Flash tier) because this key has no Pro quota.
  `C360_L2_MODEL=gemini-3.8-flash`. Scores below are for this model; a Pro model may do better.

### Step 7 — live runs on gemini-3.8-flash (STOPPED: daily quota)

- Live smoke tests (`pytest tests/layer2 -q -m live`, synthetic world): **4 of 5 passed** — metric, customer, text and
  refusal routes. The custom-SQL route came back `partial`: the model planned correctly and ran two valid `run_sql`
  queries (loans by `has_co_borrower`), then the third model call failed with HTTP 429. Not a code or model error.
- The 429 is the **free-tier daily cap**: `GenerateRequestsPerDayPerProjectPerModel-FreeTier`, limit **20 requests
  per day** per model, retry after ≈ 57,700 s (≈ 16 h). Today's 20 were used by the model probes and the smoke tests.
- One question costs 2–4 requests, so the benchmark alone (35 questions) needs roughly 100 requests, plus the
  refusal, held-out and paraphrase sets. Step 7 cannot run on this key's free tier. Stopped as instructed; no switch to
  another model (each model has its own 20/day free quota, which would not be enough either).

## Layer 3 merged (2026-10-04)

`layer3.zip` merged at the repo root: only new paths (`c360/layer3/`, `tests/layer3/`, `docs/layer3/`,
`requirements-layer3.txt`, `LAYER3_MERGE.md`); nothing existing was overwritten. `.gitignore` gained `warehouse_l3/`,
`outputs/layer3/` (as the merge notes ask) and `layer3.zip`. Dependencies installed (fastapi 0.118.0, uvicorn
0.37.0, httpx 0.28.1). As delivered: local store built in 19 s, 24/24 tests pass, post-call accuracy matches its README.

Integration with Layers 1–2:
- New `C360_L3_SOURCE=mongo` (`c360/layer3/data/source.py: MongoSource`): customer documents come from Layer 1's
  golden record in MongoDB through `c360.store.C360Store`, enriched from the Layer 3 store by case / account id. The
  golden record wins on overlap (identity incl. merged CRM records, recomputed buckets, accounts); both CRM records of
  a merged person contribute their cases; every Layer 1 key format is accepted (card ref, borrower ref, CIF too).
  Portfolio and worklist still come from the local store.
- `c360/layer3/config.maple_data()` also finds the release at `./maple_collections_release` (this repo's layout).
- With `C360_L3_WRITEBACK=mongo`, decisions now land on the real customer document (the golden `_id`).
- Checked end to end on the real data with `mongo`: health, portfolio, worklist, customer by case / account / CRM /
  card ref / CIF, next best action (CS-2026-614370: promise reminder by email, no call while on a hardship plan),
  conflict check (SMS refused: no consent, POL-COLL-001 §5.3), call review, policy search, customer login (wrong
  surname → 401), assistant (job loss → hardship options with human review). Bad / unknown keys → 400 / 404.
- 6 new offline tests (`tests/layer3/test_mongo_source.py`). Layer 3: 30 passed; Layer 2: 548 passed.

For a human decision: Layer 3 loads `cases.vulnerable_customer_flag` and `contacts.vulnerability_disclosed_flag`
(vulnerability is a protected attribute in the brief). Its README says vulnerability only *adds* safeguards (certified
agent, vulnerable-customer script, never lowers priority), which POL-COLL-004 asks for, but confirm the team is happy
to defend that. Customer names and city are shown in the agent terminal, which is normal for servicing; the Layer 2
deny-list applies to the analytics database and the LLM, not to this operational app.

### Block 1, item 1 — policy documents searchable from Layer 2 (runbook Step 7 cause "question needs data not in L2")

Why: 9 of the 35 benchmark questions are policy / procedure / incident questions; the serving database had no
document text, so Layer 2 could not answer them.
- New `c360/layer2/policies.py`: reads `data/reference_documents.csv`, resolves files under `files/`, **skips superseded
  documents**, splits into citable sections (`§x.y`, FAQ `Qn`), BM25 search with the authority rule (policy > terms >
  procedure > regulatory summary > call script / incident > FAQ / release note; FAQ and scripts ranked behind any
  reasonably relevant governing section). Same logic as Layer 3's `policy.py`, kept as an independent copy so neither
  layer depends on the other.
- `publish_l2` builds `l2.policy_sections` (cite, doc_id, section, doc_type, title, heading, version, language,
  effective_date, authority, section_text) — not customer data; the publish-time deny-list check still covers it.
- New tool `search_policy(query, limit=4, language="EN")` (registry, JSON schema, wrapped as data); answers cite the
  sections (`SourceRef.kind = "policy"`, label "policy documents"); routing guide: rules / limits / procedures /
  programme terms / incidents → `search_policy`; refusal rule now says "neither in the tables nor in the documents".
- New serving tables `ops_events` (6 rows) and `qa_checklist` (10 rows), real columns, described in `semantic.yaml`.
- Real build `l2_20261004T100107.duckdb`: 25 tables; `policy_sections` 39 sections from 17 current documents (2
  superseded skipped). Doctor: 0 errors, 0 warnings. Spot check on topic keywords: the governing section comes first
  (call attempts → POL-COLL-001 §5.2; grace period → PRC-COLL-010 §3.3; reduced payment programme → HPT-2026 /
  POL-COLL-004 §4.2; do-not-call list → POL-COLL-001 §5.3; dialer outage → IR-2026-0923; late fees / app outage →
  IR-2026-0914). No document covers legal action / suing (only the "not legal advice" banner), so such questions
  cannot be answered from documents — not tuned for.
- Synthetic world: 4 synthetic documents (current policy, superseded v1, disagreeing FAQ, incident report); real column
  names for `ops_events` / `qa_checklist`.
- Tests: new `tests/layer2/test_policy_search.py` (7 tests: no superseded text, policy outranks FAQ, errors, agent cites
  sections, loader counts, ranking). Existing tests that pin the tool list updated from 5 to 6 tools; the allow-list
  test now also pins the one code-built table (`policy_sections`) to exactly its fixed column list. **555 passed.**

### Block 1, item 2 — benchmark submission CSV, extra questions, resume

- New `c360/layer2/eval/submission.py`: writes the release template format `question_id, answer, sql_or_sources,
  refused` from evaluation results, exactly as the system answered (no editing). `answer` = answer text, then any
  table as a small CSV; refused → `Refused: <reason>`. `sql_or_sources` = the SQL that ran (governed or custom), else
  the policy citations, or the note / transcript / customer ids. Template order first, extra ids after; template ids
  without an answer are reported and left blank. Merge runs: `python -m c360.layer2.eval.submission --from a.json b.json`.
- Every `--set benchmark` run now writes `outputs/layer2/benchmark_answers_benchmark_<ts>.csv` automatically.
- Eval results now keep the full `answer` (model_dump) so the CSV can be rebuilt from the JSON at any time.
- `--questions-file extra.csv` runs questions from a CSV (for the extra questions released at 19:00 IST) without
  rebuilding Layer 1 (`loaders.load_questions_csv`, same column detection and labels-file merge as the benchmark).
- `--resume-from earlier.json …` reuses earlier answers (re-scored against today's expected answers) and re-asks only
  questions that are new or hit a model error (e.g. a 429), so a quota stop does not waste the calls already made.
  `--only BQ-003,BQ-010` runs selected ids.
- Tests: `tests/layer2/test_submission.py` (4). **559 passed.**

### Block 1, items 3–4 — real held-out questions, performance

- `eval/questions/heldout_real.yaml`: **18 real-data questions** (R01–R18), none a rewording of a benchmark question:
  governed metrics (promise rates, cure rate by product, average DPD by product, RPC, cost per RPC, hardship rate by
  queue, loan delinquency by product, payroll delay share, outbound SMS attempts, charge-off amount), custom SQL
  (accepted offers, insolvency holds by product, duplicate contacts, offers by programme), one customer, one policy
  (programme duration) and one refusal. Each expected answer comes from hand-written SQL stored in `check_sql`, run
  directly on the serving database (not through `compute_metric`). Generated by a script; regenerate after a new build.
  A first draft question (average DPD of late-stage cases) was replaced because it was a slice of BQ-004.
  Evidence: `eval --set heldout_real --fake` → 18/18 (the scripted plans' tool results equal the independent SQL).
- Performance (runbook Step 8, `outputs/layer2/performance.md`): every governed metric < 0.1 s except `roll_rate`
  (10–11 s). Cause: the insolvency-hold exclusion was a correlated `NOT EXISTS … list_contains(...)` per snapshot row.
  Rewritten in `metrics.yaml` as a `held` CTE (set of held account ids) + `NOT IN`: identical result (Q2 2026 0.055255,
  equal to the hand-written SQL), now 0.09–0.4 s. All 23 metrics (with and without a grouping) together: 1.6 s.
  No pre-aggregated tables needed.
- One dev test asserted that `heldout_real.yaml` did not exist yet (it was waiting for this step); it now checks the
  file loads with literal answers and nothing skipped. **559 passed.**

### Block 1, items 5–6 — acceptance report generator, live "go" script

- `python -m c360.layer2.acceptance [--examples]` builds `docs/layer2/acceptance_report.md` (runbook Step 9) from the
  evidence on disk: environment, doctor, serving tables with row counts and c360_id coverage, every metric with its
  status and hand check, latest **live** evaluation per set (fake runs listed separately), failures, safety
  demonstrations run live (deny-listed columns = 0; direct file read blocked by the connection; validator rejects
  `curated.customers`, `read_csv`, `SELECT *` on a person table, selecting note text), example answers per route
  (`--examples`, needs the key), limitations. Generated now: live sections say "not run yet".
- `scripts/layer2_live.sh [--extra FILE | --only-extra FILE]`: the whole live sequence — preconditions and a one-call
  quota check (stops on HTTP 429 before spending more), offline tests + doctor, live smoke tests, refusal set,
  benchmark (resumes from the latest live run), held-out-real, extra questions, merged submission CSV
  (`outputs/layer2/benchmark_answers.csv`), acceptance report with examples. Delay between questions
  `C360_L2_EVAL_DELAY` (default 1 s).
- Tests: Layer 2 **559 passed**; Layer 3 30 passed (unchanged).

## Feature store + promise risk model (2026-10-04, ~16:15 IST)

Not Layer 2 code; recorded here so every change of the day is in one place. Full write-up: `docs/features/README.md`.

- **What:** new `c360/features/` (one definition in `definitions.yaml`, 33 features in 6 groups, structured + text; batch build + live lookup from the same SQL); new `c360/models/ptp_risk.py` (sign-checked logistic regression, exact plain-English reasons, model card) and `c360/models/fairness.py` (measurement only). `c360/layer3/nba.py` uses the score as advice inside the rules; `terminal.js` shows a PROMISE RISK line. `requirements-features.txt`.
- **Reads:** the Layer 2 serving database read-only (no Layer 1 rebuild, no writes to Layer 1 outputs or MongoDB). The fairness check reads `restricted_protected_attributes` in memory only.
- **Evidence:** batch == live on 200 random promises every test run (300 checked by hand, 0 differences); point-in-time tests on a hand-built database; out-of-time AUC 0.888 (May–Aug 2026, break rate 61% vs 37% in training), predicted 57.8% vs actual 60.8%; text patterns 88–98% precision on the 500 labelled notes; fairness: one flag (recorded vulnerability, high-band ratio 1.27; mitigation in the report).
- **Tests:** `tests/features` 11 passed; `tests/layer3` 34 passed (4 new); `tests/layer2` 559 passed. The 2 `tests/layer3/test_api.py` failures seen only when Layer 2 and Layer 3 run in one pytest process were there before this change (Layer 2's conftest switches the process to its synthetic world); run the folders separately.

## Agent terminal: groups, similar customers, charts, plain wording (2026-10-04, ~17:15 IST)

Layer 3 UI work, requested by the user. Full description: `docs/layer3/README.md`, section "Agent terminal: groups…".

- **Store and API:**
  - The Layer 3 store gained four columns: `fsa`, `occupation_category`, `industry_naics2` and `employer_name`. The
    store was rebuilt in 19 s.
  - `source.py`: new `segments()`, `layoff_alerts()`, `peers()` and `program_names()`; worklist filters; extra chart
    data in `portfolio()`.
  - `api.py`: new `/groups` and `/cases/{id}/peers` endpoints; `program_name` added to the next best action.
- **Plain action labels:** the labels and two reasons in `nba.py` were reworded. The action codes are unchanged. The
  promise-risk explanation templates now say "days late"; the feature SQL fingerprint is unchanged, so the model
  needs no retraining.
- **Terminal:**
  - New Groups screen (F9) and a similar-customers panel.
  - 5 new charts on the overview, and charts on the customer screen.
  - Plain wording throughout, plus a glossary.
  - Syntax checked with `gjs`. Rendered with headless Firefox on the real data (overview, groups by employer, postal
    area L4W, customer CS-2026-730434).
- **Tests:** `tests/layer3/test_groups.py` (5 new). `tests/layer3` + `tests/features`: **50 passed**.
