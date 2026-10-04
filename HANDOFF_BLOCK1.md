# Handoff: Layer 2 "Block 1" (finish the single-agent Layer 2) — state at 2026-10-04 ~15:40 IST

For the next Claude Code session (or a teammate). Read this first, then `CHANGELOG_LAYER2.md` (section "DB machine").

> **Update 2026-10-04 ~16:00 IST: all Block 1 code is done** (items 1–6 below; details in `CHANGELOG_LAYER2.md`,
> "Block 1" entries). Tests: Layer 2 559 passed, Layer 3 30 passed; doctor 0 errors / 0 warnings; serving build
> `l2_20261004T100107.duckdb`. **Only the live run is left:** `bash scripts/layer2_live.sh` (add `--extra FILE` for the
> questions released at 19:00 IST). It stops by itself on HTTP 429.

## Where things stand

| Area | State |
|---|---|
| Layer 1 | Done. C360 in MongoDB `collections360.customers` (podman container `c360-mongo`; `podman start c360-mongo`). DuckDB `warehouse/c360.duckdb` is the processing store. Do not rebuild Layer 1. |
| Layer 2 runbook Steps 1–6 | Done (see changelog). Doctor 0 errors / 0 warnings; serving DB `warehouse/l2/` (CURRENT); 15 official metrics verified; benchmark loader reads `labels/benchmark_dev_answers.csv`. |
| Layer 2 Step 7+ (live) | **Not run.** Gemini free tier: 20 requests/day/model, 0 for Pro. User approved `gemini-3.8-flash`; user is switching to a billing-enabled key. Key + model live in `~/.config/c360/layer2.env` (load with `. ~/.config/c360/layer2.env`; never print the key). |
| Layer 3 | Merged from `layer3.zip`, integrated via `C360_L3_SOURCE=mongo` (reads the golden record from MongoDB). 30 tests pass. |
| Multi-agent spec (`SPEC_LAYER2_MULTIAGENT.md`) | Not started; deferred until after submission (needs Pro quota). |
| Git | Files staged, **no commit yet** (user said: ignore git for now). |

Environment for every Layer 2 command:
```bash
cd ~/Documents/cibc
. ~/.config/c360/layer2.env
export MAPLE_DATA=$PWD/maple_collections_release
unset C360_WAREHOUSE C360_L2_MONGO
```

## Block 1 — what was being built (code only, no API key needed)

Goal: everything ready so that when the key is in place, the evaluations run directly.

### 1. Policy documents in Layer 2 — **in progress (code written, not finished)**
Why: 9 of the 35 benchmark questions are policy/procedure/incident questions (BQ-001, 002, 005, 009, 019, 020, 026, 030, 033); Layer 2 had no document text.

Done in this session:
- `c360/layer2/policies.py` (new): loads `data/reference_documents.csv`, resolves files under `files/`, skips superseded docs, splits into sections (`§x.y` / FAQ `Qn`), BM25 search with authority rule (policy outranks FAQ / call scripts). Same logic as Layer 3's `policy.py`, independent copy.
- `c360/layer2/publish.py`: `build_policy_table()` writes `l2.policy_sections` (always created, empty if no documents) and logs counts; import of `policies` added.
- `c360/layer2/tools/search_policy.py` (new): tool `search_policy(query, limit=4, language="EN")` reading `l2.policy_sections`, cached per serving build; returns sections with `cite` (e.g. `POL-COLL-001 §5.2`).
- `c360/layer2/tools/registry.py`: tool registered + JSON schema.
- `c360/layer2/answer.py`: `SourceRef.kind` gains `"policy"`; `guards.sources_from_trace` adds policy sources; `ask.py` label "policy documents".
- `c360/layer2/prompts/system.md`: routing row for rules/policy/incident questions → `search_policy`; refusal rule mentions documents.
- New serving tables `ops_events` and `qa_checklist` (real columns) in `l2_views.yaml`, `expected_schema.yaml`; descriptions for them and `policy_sections` in `semantic.yaml`.
- Synthetic world (`synth/generate.py`): real column names for `ops_events` / `qa_checklist`; `_write_reference_documents()` writes 4 synthetic docs (a current policy, a superseded v1 that must never be returned, a disagreeing FAQ, an incident report) under `maple_data/data` and `maple_data/files/docs`.

**Left to do for item 1 (in this order):**
1. Fix the failing test `tests/layer2/test_phase2_serving_db.py::test_the_allow_list_is_the_only_way_in`: `policy_sections` is built by code (like `customer_summary` from MongoDB), not from `l2_views.yaml`. Teach the test that code-built tables are `customer_summary` and `policy_sections` — **do not weaken the deny-list check** (policy_sections columns are not personal; the publish-time deny check still runs on them).
2. Run the full offline suite and fix any other fallout (prompt-size test: the prompt budget is 60,000 chars on synthetic; check `test_system_prompt_contains_the_semantic_layer`; the `SourceRef` kind change; doctor on synthetic must stay 0 errors / 0 warnings).
3. Add tests `tests/layer2/test_policy_search.py`: (a) synthetic serving DB has `l2.policy_sections` and no superseded text ("5 outbound"); (b) `search_policy("call attempts per week")` → first hit `POL-TEST-001 §1.1`, FAQ ranked after it; (c) empty query → error; (d) agent with the fake LLM (`ScriptedLLM`, `tool_turn`, `call`, `final_turn` from `c360.layer2.llm.fake`) calling `search_policy` → `Answer.sources` has kind `policy` with the cites; (e) `policies.load_sections` on a temp dir (missing file counted, superseded skipped).
4. Real data: `python run_layer1.py --only publish_l2` (new serving file; expect `policy_sections` ≈ 17 docs' sections, superseded 2 skipped), then `python -m c360.layer2.doctor` (must exit 0), then a quick direct check: `search_policy` for "call attempts mid-stage week", "grace period promise", "reduced payment programme", "national do not call list", "consumer proposal" — each should return the governing policy section first.
5. Changelog entry under "DB machine" (what, why, evidence).

### 2. Benchmark answers CSV writer — not started
- After `python -m c360.layer2.eval --set benchmark`, write `outputs/layer2/benchmark_answers_<ts>.csv` in the release template format (`labels/benchmark_answers_template.csv`): `question_id, answer, sql_or_sources, refused`.
  - `answer`: answer_text; for a table, a small CSV (header + rows) after it.
  - `sql_or_sources`: SQL from `sources[].sql`, else policy cites / note / transcript ids.
  - `refused`: `true` iff `status == "refused"`.
- Add `--questions-file <csv>` to the eval CLI / `loaders.load_benchmark` so the extra questions released at 19:00 IST (format unknown, likely `question_id, question_text`) can be run without rebuilding Layer 1; merge into one CSV with all 35 + extras, ordered by question id. Never edit answers by hand.

### 3. Real held-out questions — not started
- `c360/layer2/eval/questions/heldout_real.yaml`: ≥ 15 new real-data questions (not paraphrases of benchmark questions) with expected answers from hand-written SQL on the serving DB (literal expected values, `kind: scalar|table`). The 18 verified metric hand-checks (`outputs/layer2/metric_hand_check.json`) are a source of checked numbers. Optional: `paraphrase_real.yaml` (real rewordings of benchmark questions; loader support for referencing benchmark expected answers).

### 4. Performance check (runbook Step 8) — not started, no key needed
- Time every governed metric (and the 18 hand-check queries) against the serving DB; pre-aggregate anything slow via `l2_views.yaml` (results must be identical). Don't raise `C360_L2_SQL_TIMEOUT` above 60 s.

### 5. Acceptance report — not started
- `docs/layer2/acceptance_report.md` per runbook Step 9: environment, doctor, serving DB (tables, row counts, coverage — in changelog Step 4), metrics (verified + hand-check), eval results (after the live run), safety demo (`run_sql` rejects `SELECT * FROM curated.customers` and `read_csv(...)`), 5 example Q&As (one per route), limitations. Best as a small generator script so the live numbers drop in automatically.

### 6. "Go" script for when the key arrives — not started
Suggested sequence (stop on HTTP 429 and report; don't switch models without the user):
```bash
python -m c360.layer2.llm.gemini                        # confirm model access
pytest tests/layer2 -q -m live                          # 5 smoke tests
python -m c360.layer2.eval --set refusal --delay 2
python -m c360.layer2.eval --set benchmark --delay 2    # + write the answers CSV
python -m c360.layer2.eval --set heldout_real --delay 2
```

## Open decisions for the user (do not decide)
1. "Last N days" includes the snapshot day (current: inclusive). Evidence in changelog Step 5 supports keeping it.
2. Customer name and employer name sent to the model via `get_customer`; transcripts also contain customer names in the spoken text (reach the model via `search_text`).
3. Layer 3 uses `vulnerable_customer_flag` / `vulnerability_disclosed_flag` to add safeguards (protected attribute in the brief).

## Rules that still apply
Read-only on Layer 1 outputs and MongoDB (only `run_layer1.py --only publish_l2`); never weaken safety checks or edit the deny-list to pass; never put benchmark questions/answers/gold SQL into prompts, semantic files or code; never print/log/commit the API key; record every change in `CHANGELOG_LAYER2.md`.
