# RUNBOOK — Adapting Layer 2 to the real Collections 360 data

**Audience:** Claude Code, running on the machine that has the real Layer 1 outputs (`warehouse/c360.duckdb`, MongoDB `collections360`, the Maple dataset files).
**Read first:** `SPEC_LAYER2.md` (the design) and `docs/layer2/README.md` (how to run it). Do not redesign Layer 2. Your job is to make it work correctly on the real data and prove it.

---

## 0. Context you need

- Layer 2 was built on another machine against **synthetic data**. Its table and column names came from reading the Layer 1 code; columns the code never referenced were **guessed**. Metric definitions are **placeholders**, and the benchmark questions were never seen.
- Everything that can differ from the real data lives in four config files. **Fix config first; change code only when config can't express the fix.**

| File | What it controls |
|---|---|
| `c360/layer2/schema/expected_schema.yaml` | what Layer 2 expects to find in `c360.duckdb` |
| `c360/layer2/l2_views.yaml` | which columns are copied into the serving DB, and how |
| `c360/layer2/semantic/semantic.yaml` | descriptions, joins, business terms, rules, codes (goes into the prompt) |
| `c360/layer2/semantic/metrics.yaml` | governed metric SQL templates |

- `CHANGELOG_LAYER2.md` from the dev machine lists every assumption and known uncertainty. Read it before Step 1.

## 1. Rules (non-negotiable)

1. **Never expose protected or contact data.** Never add to `l2_views.yaml` any column matching `c360/layer2/deny_list.py` (protected attributes, names, DOB, age, phones, emails, addresses, postal codes, national IDs, `*_raw`). Never edit the deny-list to make something pass. If a real column looks like personal data but isn't on the deny-list, **add it to the deny-list**, not to the allow-list.
2. **Never weaken a safety check** (SQL validator rules, read-only connection, external-access block, loop cap, refusal behaviour) to make a test or benchmark pass.
3. **Read-only on production data.** Don't write to `c360.duckdb`, don't modify MongoDB `customers`, and don't delete or rebuild Layer 1 outputs. The only Layer 1 step you may run is `python run_layer1.py --only publish_l2`, which writes a **new** file under `warehouse/l2/`. If you believe a Layer 1 rebuild is needed, stop and say why.
4. **No benchmark answer hard-coding.** Never put a benchmark question, its expected answer or its gold SQL into the prompt, semantic files or code. Every semantic change must be **general**: a definition, a join path, a business term, a trap rule, a value vocabulary. For each change, ask yourself: "Would this also help a question the benchmark doesn't contain?"
5. **Don't send bulk customer data to the LLM.** Don't raise `C360_L2_MAX_ROWS` above 50 or remove `get_customer` trimming.
6. **Record every change** in `CHANGELOG_LAYER2.md` under a "DB machine" heading: what changed, why, what evidence (doctor line, query result, failing question ID).
7. Keep the tests green: after each step, run `pytest tests/layer2 -q`. Tests that use synthetic data must keep passing; if a config change legitimately breaks a synthetic test, update the synthetic generator or test, and say so in the changelog.

## 2. Preconditions

```bash
cd <repo root>
python --version                      # 3.11+
pip install -r requirements-layer2.txt
ls -lh warehouse/c360.duckdb          # must exist (Layer 1 built)
python -c "from c360 import mongo; d = mongo.db(); d.command('ping'); print(d.customers.estimated_document_count())"
echo $MAPLE_DATA                      # dataset root (transcript JSON files live under it)
export GEMINI_API_KEY=...             # do not print it
export C360_L2_MODEL=<model name recorded in CHANGELOG_LAYER2.md>
unset C360_WAREHOUSE C360_L2_MONGO    # use the real warehouse and real MongoDB
```

**Important:** DuckDB allows one writing process per file. Make sure no Layer 1 process is running, and don't run Layer 1 while you work.

If any precondition fails, stop and report exactly what is missing.

## 3. Procedure

### Step 1 — Prove the transferred code works (synthetic)

```bash
pytest tests/layer2 -q -m "not live"
```
All must pass before touching real data. If something fails because of a different library version on this machine, fix it (pin versions in `requirements-layer2.txt`) and note it.

### Step 2 — Run the doctor on real data

```bash
python -m c360.layer2.doctor
```
Read `outputs/layer2/doctor_report.md` fully. Expect ERRORs and WARNINGs; that's normal.

### Step 3 — Fix schema mismatches (config only)

Work through the doctor report in this order, re-running the doctor after each batch:

1. **Missing required tables/columns.** Find the real name: `DESCRIBE curated.<table>`, and `SELECT * FROM curated._column_docs WHERE table_name = '<table>'` (descriptions from the official `schema.json`). Update `expected_schema.yaml` and the column's source expression in `l2_views.yaml`. If a confirmed column truly doesn't exist, the Layer 1 code that references it would fail too, so first check whether Layer 1 really ran in full.
2. **Inferred tables** (`metric_definitions`, `benchmark_questions`, `code_lookups`, `hardship_programs`, `agents`, `case_assignment_history`, `account_monthly_snapshot` extras): replace guessed columns with the real ones. For `agents`, allow-list only non-personal attributes (no names).
3. **Unexpected columns** listed by the doctor: for each, decide:
   - useful for questions and not personal → add to `l2_views.yaml` + a description in `semantic.yaml`;
   - personal, or a proxy for a protected attribute → add to the deny-list;
   - not useful → ignore.
   Never add a column you don't understand. Check `_column_docs` first.
4. **Transcripts:** the doctor shows the top-level JSON keys of a real transcript. If `transcripts.parse()` produces poor text (check 3 files manually: `python -c "from c360.layer2.transcripts import parse; print(parse('<path>')[:800])"`), adjust the parser to the real structure. Add a unit test using a small copied-and-anonymised structural sample (keys only, invented text, no real customer text in the repo).
5. **Text search mode:** if `fts` can't be installed (offline machine), `like` mode is used automatically. Note it; it's acceptable.

Done when `python -m c360.layer2.doctor` exits 0 (no ERRORs). Remaining WARNINGs must each be explained in the changelog.

### Step 4 — Build the real serving database

```bash
time python run_layer1.py --only publish_l2
```
Check:
- it finishes (expect minutes, not hours; `account_monthly_snapshot` has ~12.6M rows and `contact_history` ~2.7M);
- `meta.row_counts` matches the source: compare each L2 table with `SELECT count(*)` on its curated source (person-level tables should match the number of rows with a `c360_id` plus those without; investigate any difference over 0.1%);
- `c360_id` coverage per person-linked table (expect ≥ 99% for cases, contacts, promises, accounts given Layer 1's linkage results; report actual numbers);
- the deny-list check passed (publish aborts otherwise);
- `customer_summary` row count ≈ 1,000,008.

Record build time, file size and coverage numbers in the changelog.

### Step 5 — Replace placeholder metrics with the official definitions

```bash
python - <<'EOF'
import duckdb; from c360 import config
con = duckdb.connect(str(config.C360_DB), read_only=True)
print(con.execute("SELECT * FROM curated.metric_definitions").fetchdf().to_string())
EOF
```
For **each** official metric:
1. Find or create the matching entry in `metrics.yaml`. Use the official name as the key (snake_case) and keep the old key as an alias if the prompt or tests reference it.
2. Translate the official definition into the template (numerator/denominator or full `sql`), using L2 tables only. Watch for: stored vs recomputed buckets (official definitions almost certainly use **stored**), duplicate contacts, open/closed case definitions, date column choice (due date vs created date), and denominators (which statuses count).
3. Set `status: verified` and add `defined_in: metric_definitions:<id>`.
4. Validate: run it via `compute_metric` for one period and one grouping, and also with a hand-written SQL query that follows the definition literally. They must agree. Sanity-check magnitudes (rates in [0,1], counts plausible against table sizes).
5. Remove placeholder metrics that have no official counterpart **only if** nothing depends on them; otherwise keep them clearly labelled `status: placeholder` (they show as "placeholder definition" in answers).

Also update `semantic.yaml`:
- `terms`: fix "unsecured loan" (check `loan_accounts.secured_flag` and `product_type` values: `SELECT product_type, secured_flag, count(*) FROM curated.loan_accounts GROUP BY ALL`), queues, treatments, outcomes, hardship reasons, channel values. Use real distinct values (`SELECT DISTINCT …`) for low-cardinality columns the agent filters on.
- `codes`: from `curated.code_lookups`; include only codes for columns the agent filters on.
- Column descriptions: from `curated._column_docs`.

Re-run `pytest tests/layer2 -q -m "not live"`.

### Step 6 — Load and inspect the real benchmark questions

```bash
python - <<'EOF'
import duckdb; from c360 import config
con = duckdb.connect(str(config.C360_DB), read_only=True)
df = con.execute("SELECT * FROM curated.benchmark_questions").fetchdf()
print(df.columns.tolist()); print(df.to_string()[:6000])
EOF
```
- Make `eval/loaders.load_benchmark()` map the real columns (question text, expected answer, gold SQL, category) correctly. Expected-answer parsing must handle the real formats you see (numbers, percentages, lists, small tables). If there's no machine-readable expected answer, use `manual` scoring.
- If gold SQL exists, check it runs read-only on the source DB. The harness computes expected results from it; it must **not** be shown to the agent.
- Classify the 35 questions (governed metric / custom SQL / single customer / text / refusal) in a short table in the changelog. This shows which metrics and terms matter most.

### Step 7 — Live evaluation and improvement loop

```bash
python -m c360.layer2.eval --set benchmark
python -m c360.layer2.eval --set refusal
```
Read each failure in `outputs/layer2/eval_benchmark_*.md`. Classify the cause:

| Cause | Fix |
|---|---|
| wrong/missing metric definition | `metrics.yaml` |
| unknown business term or value | `semantic.yaml` `terms` / value lists |
| wrong join or grain | `semantic.yaml` `joins`; consider a trap rule |
| wrong date anchoring | `periods.py` (code) or `rules` |
| recurring SQL mistake | new trap rule in `sql_guard.TRAPS` + unit test |
| expected-answer parsing problem | `eval/loaders.py` / `compare.py` |
| wrong tool chosen | routing guide in `prompts/system.md` |
| question needs a table/column not in L2 | add it to `l2_views.yaml` (respecting the deny-list), rebuild with `--only publish_l2` |
| benchmark answer itself seems inconsistent with the data | note it; don't distort the system to match |

Change one cause at a time and re-run. Stop iterating when two consecutive rounds bring no improvement, or the targets are met.

Then write **real held-out questions**: ≥ 15 new questions about the real data that aren't paraphrases of benchmark questions, with expected answers computed by hand-written SQL that you check. Save them as `eval/questions/heldout_real.yaml`. Don't tune on them. Run:

```bash
python -m c360.layer2.eval --set heldout_real
python -m c360.layer2.eval --set paraphrase     # update paraphrase.yaml to real-data rewordings of benchmark questions
```

**Targets:** benchmark ≥ 85% of auto-scorable questions; held-out-real ≥ 75%; refusal = 100%; zero answers containing phone numbers, emails or deny-listed fields; median latency reported (no fixed target). If benchmark rises while held-out falls, you're over-fitting: revert the last changes.

### Step 8 — Performance check

For the 5 slowest questions in the benchmark report:
- look at their SQL in the trace and time it directly against the serving DB;
- if a governed metric is slow because of a large join or scan (e.g. roll rate over 12.6M snapshot rows), add a **pre-aggregated table** to `l2_views.yaml` (e.g. monthly bucket counts per product), point the metric at it, and confirm results are identical;
- don't raise `C360_L2_SQL_TIMEOUT` above 60 s.

### Step 9 — Final acceptance

Run everything and write `docs/layer2/acceptance_report.md` containing:

1. Environment: date, machine, Python, DuckDB, model name, serving build (`meta.build`), Layer 1 `lineage.pipeline_version`.
2. Doctor: final exit code and remaining warnings with explanations.
3. Serving DB: tables, row counts, `c360_id` coverage, build time, size, text-search mode.
4. Metrics: each official metric → status `verified`, plus the hand-check result.
5. Eval results for benchmark, held-out-real, paraphrase and refusal: accuracy, failures with a one-line cause each.
6. Safety: deny-list check result; number of answers with redactions; refusal results; a demonstration that `run_sql` rejects `SELECT * FROM curated.customers` and `read_csv(...)`.
7. Five example Q&As (one per route), as printed by `python -m c360.layer2.ask`.
8. Known limitations and recommended next steps (including the out-of-scope items from SPEC Section 12).

```bash
pytest tests/layer2 -q -m "not live"
python -m c360.layer2.doctor
python -m c360.layer2.eval --set all
```

## 4. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `IO Error: Could not set lock on file` | another process has `c360.duckdb` open for writing (Layer 1, or a notebook). Close it. Layer 2 runtime only opens `warehouse/l2/*.duckdb`. |
| `publish_l2` aborts: deny-list | an allow-listed column is personal. Remove it from `l2_views.yaml` (don't edit the deny-list). |
| `INSTALL fts` fails | offline machine; `like` mode is used. Fine. |
| `get_customer` returns nothing for a valid-looking ID | check `c360.store._KEY_RULES` patterns against real IDs; CIFs are zero-padded to 10 digits. Don't change Layer 1's store; wrap or extend in Layer 2 if needed. |
| Gemini errors 429/5xx | the client retries 3 times; for eval runs use `--limit` or add a delay between questions (harness option). |
| Gemini rejects a tool schema | simplify that tool's JSON schema (no `oneOf`, no nested unions); keep the same arguments. |
| Answers quote numbers not in results | the output guard warns; check whether the prompt's "never invent numbers" rule is intact and whether rounding tolerance in the guard matches the display precision. |
| Transcripts text empty | real JSON structure differs; adjust `transcripts.parse()` (Step 3.4). |

## 5. What to report back

When done, or when blocked, produce a short summary for the team:
- final scores per eval set, and the three most common remaining failure causes;
- every config/code change made on this machine (from the changelog);
- anything that needs a human decision (e.g. a column whose sensitivity is unclear, a benchmark answer that looks wrong, a metric definition that's ambiguous).
