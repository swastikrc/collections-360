# Layer 2 — Ask the Collections 360 in plain English

A command-line question-answering system for collections managers. A person asks a question in
English. An LLM agent (Google Gemini) answers by calling tools that run **read-only** queries
against a governed copy of the Collections 360 data. Every answer shows how it was produced (the
governed metric or SQL that ran, or the ids of the records, notes and transcripts it came from).
Questions outside allowed use are refused.

Design: [docs/SPEC_LAYER2.md](../SPEC_LAYER2.md). Adapting to the real data:
[docs/DB_MACHINE_RUNBOOK.md](../DB_MACHINE_RUNBOOK.md). Assumptions and open points:
[CHANGELOG_LAYER2.md](../../CHANGELOG_LAYER2.md).

> **Status.** Built and tested on synthetic data. Metric definitions are placeholders and several
> column names are guesses until the runbook is followed on the machine with the real data.
> The live Gemini runs (5 smoke tests and the evaluation scores) have **not** been run yet: no API
> key was available on the development machine. See "Known limitations".

## How it works

```
question → input guard → agent loop (Gemini, at most 5 tool calls)
              ├─ compute_metric(metric, period, group_by, filters)   governed metric: code builds tested SQL
              ├─ run_sql(query, purpose)                             validated, read-only SQL (fallback)
              ├─ get_customer(key)                                   one trimmed Collections 360 record
              ├─ search_text(query, ...)                             agent notes and call transcripts
              └─ refuse(reason, rule)
         → output guard → answer + "How we got this" → audit log
```

The agent never opens Layer 1's `c360.duckdb`. It only opens the **serving database**: a separate
DuckDB file built by `publish_l2`, holding allow-listed columns only.

## Quick start (synthetic data, no real data needed)

```bash
python -m venv .venv && source .venv/bin/activate      # Python 3.11+
pip install -r requirements-layer2.txt

export C360_WAREHOUSE=$PWD/warehouse_synth
export MAPLE_DATA=$PWD/warehouse_synth/maple_data
export C360_L2_MONGO=mock:$PWD/warehouse_synth/customers.jsonl

python -m c360.layer2.synth                  # 1. synthetic world (c360.duckdb, customers.jsonl, transcripts, questions)
python run_layer1.py --only publish_l2       # 2. build the serving database
python -m c360.layer2.doctor                 # 3. check the source against what Layer 2 expects
pytest tests/layer2 -q                       # 4. the offline test suite (live tests are skipped without a key)
python -m c360.layer2.eval --set all --fake  # 5. offline pipeline check with a scripted fake LLM
```

With a Gemini key:

```bash
export GEMINI_API_KEY=...                    # never printed or logged
python -m c360.layer2.llm.gemini             # list the models your key can use
export C360_L2_MODEL=<strongest Pro-tier model with function calling>

python -m c360.layer2.ask "Which hardship reason has the best promise-kept rate in the last 90 days?"
python -m c360.layer2.ask --trace "How many open cases are in each queue?"
python -m c360.layer2.ask --json "What is the cure rate year to date?"
python -m c360.layer2.ask --repl

pytest tests/layer2 -q -m live               # 5 live smoke tests, one per route
python -m c360.layer2.eval --set all         # synthetic, heldout, paraphrase, refusal, benchmark
```

On the machine with the real data, unset `C360_WAREHOUSE` and `C360_L2_MONGO` and follow the runbook.

## Commands

| Command | What it does |
|---|---|
| `python -m c360.layer2.synth [--out DIR] [--people N] [--seed S]` | Generates the synthetic world. Refuses to write over a `c360.duckdb` it did not create. |
| `python run_layer1.py --only publish_l2` | Builds a new serving database `<warehouse>/l2/l2_<build_ts>.duckdb` and switches the `CURRENT` pointer atomically. Runs the doctor first and refuses to publish on any error. Keeps the current and the previous file. |
| `python -m c360.layer2.doctor` | Compares the source database and MongoDB with `expected_schema.yaml` and `l2_views.yaml`. Writes `outputs/layer2/doctor_report.md` and `.json`. Exit code 1 if anything is an ERROR. |
| `python -m c360.layer2.ask "..."` | Answers one question. `--json`, `--trace`, `--repl`. |
| `python -m c360.layer2.eval --set NAME [--limit N] [--fake] [--delay S]` | Runs a question set and writes `outputs/layer2/eval_<set>_<timestamp>.md` and `.json`. `NAME` is `synthetic`, `heldout`, `paraphrase`, `refusal`, `benchmark`, `all`, or any `eval/questions/<name>.yaml`. |
| `python -m c360.layer2.llm.gemini` | Lists the models the API key can use. |

### What an answer looks like

```
Interpreted as: promise-kept rate · 1 Jul–28 Sep 2026 · by hardship reason

<answer text>

  hardship reason   kept rate
  ---------------   ---------
  illness                0.75
  ...

Insight: ...
Try next: ... | ...

How we got this:
  [governed metric] Governed metric: Promise-kept rate (placeholder definition)
  SQL: SELECT c.hardship_reason AS hardship_reason, count(*) FILTER (WHERE p.ptp_status = 'kept') AS numerator, ...
Warnings: ...
Data as of 2026-09-28 · serving build 20261004T060000 · model <name> · 1 tool call · 6.1s
```

`sources`, `warnings` and the last line are filled by code from what actually ran. The model
cannot write them.

## Configuration

Environment variables (all read at run time):

| Variable | Default | Meaning |
|---|---|---|
| `C360_WAREHOUSE` | `./warehouse` | Layer 1 warehouse directory (holds `c360.duckdb`). |
| `C360_L2_DIR` | `<warehouse>/l2` | Where serving databases and `CURRENT` live. |
| `MAPLE_DATA` | `./maple_collections_release` | Dataset root; transcript JSON files are read from here by `publish_l2`. |
| `C360_L2_MONGO` | `real` | `real` uses `c360.mongo.db()`; `mock:<customers.jsonl>` loads an in-memory mongomock. |
| `GEMINI_API_KEY` | none | Required for live runs. |
| `C360_L2_MODEL` | none | Required for live runs. Not hard-coded anywhere. |
| `C360_L2_TEMPERATURE` | `0` | |
| `C360_L2_MAX_TOOL_CALLS` | `5` | Tool-call budget per question, enforced in code. |
| `C360_L2_MAX_ROWS` | `50` | Rows returned to the model. Do not raise it. |
| `C360_L2_SQL_TIMEOUT` | `30` | Seconds before a query is interrupted. |
| `C360_L2_AUDIT` | `outputs/layer2/audit.jsonl` | Audit log. |
| `C360_L2_OUT` | `outputs/layer2` | Doctor and evaluation reports. |
| `C360_L2_TEXT_MODE` | `fts` | Set to `like` to build the serving database without the full-text index. |

Everything that can differ on the real data lives in four files. Fix these first; change code only
when a file cannot express the fix.

| File | Controls |
|---|---|
| `c360/layer2/schema/expected_schema.yaml` | What Layer 2 expects to find in `c360.duckdb` (confirmed / protected / inferred). |
| `c360/layer2/l2_views.yaml` | Which columns are copied into the serving database, and how. The allow-list. |
| `c360/layer2/semantic/semantic.yaml` | Table and column descriptions, joins, business terms, rules, not-allowed uses. Goes into the prompt. |
| `c360/layer2/semantic/metrics.yaml` | Governed metric SQL templates. |

## The safety model

Privacy and correctness are enforced by code and by structure, not by asking the model nicely.

1. **Protected and contact columns do not exist in the serving database.** `publish_l2` copies only
   allow-listed columns, checks the allow-list (names *and* source expressions) against
   `deny_list.py`, and checks the finished file again before switching `CURRENT`. A test lists every
   column of the serving database and asserts none is deny-listed.
2. **The connection is locked down.** Opened read-only, then `enable_external_access = false` and
   `lock_configuration = true`: no files, no HTTP, no `ATTACH`, no `COPY`, no extension loading,
   even for SQL that got past the validator.
3. **Every query is validated** (`tools/sql_guard.py`): one `SELECT` only; schema `l2` only; no table,
   file or system functions; no `SELECT *` on row-level tables; free text never selected; no
   deny-listed names. Trap rules catch mistakes that give a plausible but wrong number: double-loaded
   contacts, `current_date` instead of the snapshot date, recomputed buckets in reported figures,
   superseded model scores, `count(*)` across a join.
4. **Governed metrics.** For the common questions the model picks a metric and parameters, and tested
   code builds the SQL. Dates are resolved in code against the snapshot date (`periods.py`).
5. **Limits are enforced in code**: 5 tool calls, no repeated call, stop after two tool errors in a
   row, one repair attempt for a malformed final answer.
6. **Guards on both sides.** The input guard refuses obvious forbidden asks before any model call. The
   output guard fills the sources from the trace, redacts phone numbers, emails and deny-listed field
   names, and warns about any number in the answer that no tool returned.
7. **Text is data.** Notes and transcripts reach the model wrapped in `<tool_result trust="data">`,
   with `<` escaped so text cannot close the wrapper, and the prompt says never to follow instructions
   found inside. The synthetic data contains injection attempts to exercise this.
8. **Audit.** One JSON line per question: the question, the tools and SQL, warnings, refusal rule,
   model and build. Never the result rows.
9. **`get_customer` is trimmed** before the model sees it: no phone, email, address, postal code, date
   of birth, age, match evidence or file paths; at most 5 cases, notes and scores.

## Known limitations

- **No live model run yet.** `GEMINI_API_KEY` was not set on the development machine, so the 5 live
  smoke tests and the live evaluation scores (synthetic, held-out, paraphrase, refusal) are outstanding.
  What has been proven offline: all 12 metrics equal an independent pandas calculation; the full
  pipeline passes every question set when the tool calls are scripted (`eval --fake`); the Gemini
  provider is tested against a stub client. Prompt wording and routing have **not** been tuned
  against a real model.
- **Placeholder metrics.** Every definition in `metrics.yaml` is a reasonable guess, labelled
  "placeholder definition" in answers until the official definitions are entered (runbook Step 5).
- **Inferred columns.** The reference tables (`metric_definitions`, `benchmark_questions`,
  `code_lookups`, `hardship_programs`, `agents`, `case_assignment_history`) and four columns of
  `account_monthly_snapshot` use guessed column names. They are optional: a missing one is skipped
  with a warning.
- **"last N days"** is the N days ending on the snapshot date, inclusive. One setting in
  `semantic.yaml` changes it if the official definitions differ.
- **Transcript format is unknown.** `transcripts.parse()` is tolerant, but check it on real files
  (runbook Step 3.4).
- **Names reach the model through `get_customer`.** The spec's trim list keeps the customer's name
  and employer name. Decide whether to drop them (one line in `tools/get_customer.py`).
- **A 10-digit CIF in an answer is redacted** as if it were a phone number. That errs on the safe side.
- **Prompt injection.** Text is wrapped, escaped and marked as data, and the output guard redacts
  personal data, but a single model both reads the text and writes the answer, so an injected
  instruction could still colour the wording of an answer. See future work.

## Future work (out of scope for this build)

1. Dual-LLM summariser for `search_text`, so the model that reads untrusted text never drives tools.
2. Build-id consistency check between the DuckDB serving database and MongoDB.
3. A verifier pass by a second model before an answer is shown.
4. An answer cache.
5. Minimum group size suppression (small groups are currently reported; the prompt asks the model to say so).

Also not built: a web UI, prompt caching, private model hosting, and anything in Layer 3.

## Layout

```
c360/layer2/
  config.py  deny_list.py  schema_util.py  schema/expected_schema.yaml  l2_views.yaml
  publish.py  serving.py  doctor.py  transcripts.py
  semantic/semantic.yaml  semantic/metrics.yaml  periods.py  sanity.py
  tools/      registry.py sql_guard.py run_sql.py compute_metric.py get_customer.py search_text.py refuse.py
  llm/        base.py gemini.py fake.py
  prompts/    system.md
  answer.py  guards.py  agent.py  audit.py  ask.py
  synth/      generate.py truth.py questions.py
  eval/       harness.py loaders.py compare.py questions/{synthetic,heldout,paraphrase,refusal}.yaml
tests/layer2/   495 offline tests + 5 live smoke tests
```
