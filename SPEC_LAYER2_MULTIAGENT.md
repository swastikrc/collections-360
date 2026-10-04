# SPEC — Layer 2 upgrade: multi-agent mode, switchable with the existing single agent

**Audience:** Claude Code, working on the machine that has the current Layer 2 code **and** the real data (`warehouse/c360.duckdb`, MongoDB `collections360`, the Maple dataset). This is the machine where Layer 2 was built from the original `SPEC_LAYER2.md` and adapted with the original `DB_MACHINE_RUNBOOK.md`.

**This document is self-contained.** It contains every change made to `SPEC_LAYER2.md` and `DB_MACHINE_RUNBOOK.md` after the original versions were used. For anything not covered here (serving DB, tools, SQL guard, semantic layer, periods, doctor, evaluation basics), the original `SPEC_LAYER2.md` in the repo still describes the existing code. Read it for reference, but don't rebuild what exists.

---

## 0. Read this first

### 0.1 The situation

- **Layer 2 exists and works** in single-agent mode. It was built from the original `SPEC_LAYER2.md`, then adapted to the real data: real column mappings in `l2_views.yaml`, verified metrics in `metrics.yaml`, real terms and value lists in `semantic.yaml`, a parser for real transcripts, real benchmark loader mappings, held-out questions, and an acceptance report.
- **Layer 3 is being built on top of Layer 2.** It imports Layer 2 Python code and/or calls its HTTP API. **Layer 3 code must not need any change because of this upgrade.**
- This upgrade adds a **multi-agent answering mode** next to the existing single agent, plus Gemini free-tier infrastructure (two model tiers, rate limiter, response cache) and a complete HTTP API. **Both modes are reachable through exactly the same functions, CLI commands and HTTP endpoints, with the same response shape.** The mode is a setting, never a different interface.

### 0.2 Goals, in priority order

1. **Don't break anything.** Every existing public interface (Python functions and classes, CLI commands and flags, HTTP endpoints, response fields, env vars, file paths, audit log fields) keeps working with identical behaviour in single mode. Changes are **additive only**.
2. **Don't lose the real-data adaptation.** Never overwrite `l2_views.yaml`, `metrics.yaml`, `semantic.yaml`, `expected_schema.yaml`, `transcripts.py`, `eval/loaders.py`, `eval/questions/*_real.yaml` or prompt/semantic content tuned on the DB machine with synthetic-era defaults. Extend them; don't replace them.
3. **Add multi-agent mode** as specified in Sections 4–8, switchable globally, per request and per role.
4. **Prove it:** compatibility tests unchanged and green; multi mode evaluated on the real benchmark and compared with single mode.

### 0.3 Decisions already made (do not revisit)

| Topic | Decision |
|---|---|
| Orchestration | **Plain Python** controller and state machine. No agent framework. |
| Models | Gemini via `google-genai`. **Pro tier**: planner, SQL writer (and the optional independent SQL check). **Flash tier**: every other multi-agent role. Single mode keeps using `C360_L2_MODEL` exactly as today. |
| API tier | **Gemini free tier.** Low rate limits and a daily cap; Google may use prompts to improve its products (accepted because the dataset is synthetic). Throttle, cache and minimise data sent (Section 6). |
| Tool calling in multi mode | Roles **don't call tools**. Each role returns JSON (structured output, validated by pydantic); the controller runs tools. |
| Default mode | **`single` stays the default** until the team chooses otherwise after Phase U9. Switching the default is a config change, not a code change. |
| Interfaces | Single and multi share **one** `ask` entry point, CLI, API and `Answer` shape. New fields are optional, and new behaviours (clarification questions, detailed role events) are **opt-in**, so existing callers see nothing new. |

### 0.4 Working rules for you (Claude Code)

1. Work in phases (Section 11). Don't start a phase until the previous one's acceptance checks pass.
2. **First phase is U0 (baseline and freeze).** No code changes before the compatibility tests exist and pass on the current code.
3. After every phase run `pytest tests/layer2 tests/compat tests/api -q -m "not live"` (directories that don't exist yet are skipped). Compatibility tests must **never be edited** to make them pass, except to add coverage.
4. **Never weaken a safety check** (SQL guard, read-only connection, external-access block, deny-list, loop/step caps, policy rules, output guard, data minimisation) to make anything pass.
5. **Read-only on production data.** Don't write to `c360.duckdb` or MongoDB `customers`, and don't rebuild Layer 1. The only Layer 1 step you may run is `python run_layer1.py --only publish_l2` (writes a new serving file).
6. **No benchmark hard-coding.** Never put a benchmark question, expected answer or gold SQL into prompts, semantic files or code. Every change must be general.
7. **Respect the model budget.** Check `python -m c360.layer2.llm.usage` before live runs; run cheap role-level evaluations before end-to-end ones; use the cache, `--max-live-calls` and `--resume`. Unit and compatibility tests never call Gemini.
8. **Prompt changes bump the prompt's `version`** (front matter).
9. **Never print, log or commit the API key.**
10. Keep `CHANGELOG_LAYER2.md`: add a heading "Upgrade: multi-agent" and an entry per phase (what changed, why, evidence). Record deviations from this spec under "Deviations".
11. Never modify Layer 3 code. If Layer 3 turns out to depend on something this spec would change, keep the old behaviour and record it.

---

## 1. Phase U0: Baseline, inventory and freeze

### 1.1 Version control
- Make sure the working tree is clean. Commit any uncommitted DB-machine adaptations first, with message "Layer 2 v1 as adapted on DB machine".
- Tag `layer2-v1-baseline`. Do all upgrade work on branch `layer2-multiagent`.
- Back up the current `warehouse/l2/` serving file name (`CURRENT`), `outputs/layer2/` and the four config files to `outputs/layer2/upgrade_backup/<timestamp>/`.

### 1.2 Inventory the public interface → `docs/layer2/PUBLIC_INTERFACE.md`
Find and list everything outside Layer 2 that could depend on it:
- **Layer 3 usage:** search the repo for `c360.layer2`, `from c360.layer2`, `c360.api`, `/api/v1`, `C360_L2_`, and any HTTP client calls to the Layer 2 API. For each hit: file, imported name or endpoint, and how the result is used (which `Answer` fields, which `status` values, which events).
- **Python surface:** public functions/classes in `c360/layer2/` (no leading underscore) used anywhere outside their own module, including `agent.ask` (exact current signature and return type), tool functions, `Answer`/`TraceStep` models, `serving`, `periods`, `C360Store` usage.
- **CLI:** `python -m c360.layer2.ask|doctor|eval|synth` and all flags (capture `--help` output).
- **HTTP:** if `c360/api/` exists: every route, method, request and response shape, headers, error codes, SSE event names (capture the OpenAPI JSON from FastAPI).
- **Files and env vars:** serving directory and `CURRENT`, audit log path and line fields, feedback file, every `C360_*` env var read by Layer 2.

### 1.3 Freeze it with compatibility tests (`tests/compat/`)
Write tests that pin today's behaviour **before** changing code:
- `agent.ask` signature and return type (inspect), and a run with the **fake LLM** (scripted turns) for 6 scenarios (governed metric, custom SQL, customer, text, refusal, tool-budget stop), asserting the exact `Answer` field names, types, `status` values and `sources` produced.
- `Answer` and `TraceStep` JSON: snapshot of field names and required-ness.
- CLI: `--help` text for each command contains every existing flag; `ask --json` output parses into the existing `Answer`.
- HTTP (if it exists): FastAPI test client hits every existing route with fake LLM; snapshot of route list, response field names, status codes, and SSE event names/order for single mode.
- Audit log: field names of one written line.
- Layer 3 usages from 1.2: for each, a test exercising that exact call path through Layer 2 (fake LLM), so a Layer 3 dependency breaking fails a test here.

### 1.4 Baseline scores
From the existing acceptance report and evaluation outputs, record in the changelog: single-mode benchmark accuracy, held-out-real accuracy, refusal result, median latency. If they're missing, run `python -m c360.layer2.eval --set benchmark` and `--set refusal` once (respecting budget) and record.

**Accept U0:** tag exists; `PUBLIC_INTERFACE.md` lists every Layer 3 dependency; `pytest tests/compat` passes on the **unchanged** code; baseline scores recorded.

---

## 2. Compatibility rules (apply to every later phase)

| Surface | Rule |
|---|---|
| Python functions | Keep names, parameters, defaults and **return types**. Add only keyword-only parameters with defaults that reproduce today's behaviour. If a richer return is needed, add a **new** function. |
| `agent.ask` | Keep it exactly. Add `agent.run(question, *, history=None, user_id="cli", on_event=None, mode=None, options=None) -> RunResult(answer, trace)`, used by everything new. If today's `ask` already has some of these parameters, keep its behaviour and route it through `run`. |
| `Answer`, `TraceStep` | Add **optional** fields only (Section 3.2). Never rename, remove or change the type of an existing field. |
| `status` values | Existing callers never receive new values unless they opt in: `needs_clarification` is returned only when the caller passes `allow_clarification=True` (Python) or `options.allow_clarification: true` (API). Otherwise the system picks the most likely reading, states it in `interpretation`, and adds a warning. |
| Events | Existing callers see the same events as today. Detailed role events are emitted only when the caller asks for them (`events="detailed"` / `options.events: "detailed"`); default `"basic"` = today's events. |
| CLI | Existing commands and flags unchanged. New flags only (`--mode`, `--roles-off`, `--events`, `--allow-clarification`). |
| HTTP | Existing routes, methods, fields and status codes unchanged; new optional request options and response fields only; new routes only if listed in Section 9 and missing today. |
| Env vars | Existing ones keep their meaning (`C360_L2_MODEL` still selects the single-mode model). New ones have defaults that keep today's behaviour. |
| Files | Serving DB layout, `CURRENT`, audit and feedback paths unchanged; new fields appended to audit lines only. |
| Default behaviour | With no new env vars set, Layer 2 behaves exactly as before (single mode), apart from the rate limiter, which only waits when limits are reached. |

---

## 3. Shared changes (both modes)

### 3.1 Mode switching (all surfaces, same entry points)

| Where | How | Default |
|---|---|---|
| Global | `C360_L2_MODE=single\|multi` | `single` |
| Python | `agent.run(..., mode="single"\|"multi")` (also accepted by `agent.ask` as a new keyword-only parameter) | global |
| CLI | `python -m c360.layer2.ask --mode multi "…"` | global |
| HTTP | request body `options.mode`; honoured for roles in `C360_API_MODE_OVERRIDE_ROLES` (default `admin`) and for internal callers using a service header `X-C360-Role: service` | global |
| Evaluation | `python -m c360.layer2.eval --mode single\|multi` | global |
| Per role (multi) | `C360_L2_ROLE_<NAME>=on\|off`, or `options.roles_off: [...]` (same role restriction as mode) | all on |

The chosen mode and the roles used are always recorded in `Answer.meta` and the audit line. Both modes return the same `Answer` model through the same functions and endpoints.

### 3.2 Additive schema changes

`Answer`, new optional fields:
```
status: + "needs_clarification"   # only when the caller opted in (Section 2)
clarification: {question: str, options: [str]} | null
checks: [{name: "sql_check"|"grounding"|"policy_in"|"policy_out", step_id: str|null,
          verdict: "pass"|"fix"|"uncertain"|"revise"|"blocked"|"skipped", details: str}]   (default [])
meta: + mode: "single"|"multi", llm_calls: {pro, flash, live, replayed}, roles_used: [str], degradations: [str]
```
`TraceStep`, new optional fields:
```
kind: "role"|"tool"|"check"|"control"   (single-mode tool steps: "tool")
role: str|null, step_id: str|null, verdict: str|null, model_tier: "pro"|"flash"|null,
prompt_version: int|null, cached: bool (default false)
```
`checks`, `meta` additions and trace fields are **always filled by code**, never by a model. In single mode, `checks` is empty unless the output guard redacted something (then a `policy_out` entry with verdict `blocked` or `pass`).

### 3.3 Model layer (`c360/layer2/llm/`)

Extend; don't replace the single-mode client.
- `base.py`: add `tier: "pro"|"flash"|"single"` to calls; add structured-output calls (`generate_json(system, prompt, schema, tier)`); keep the existing tool-calling interface used by the single agent.
- `gemini.py`: three model names: `C360_L2_MODEL` (single mode, unchanged), `C360_L2_MODEL_PRO`, `C360_L2_MODEL_FLASH` (both default to `C360_L2_MODEL` if unset, so nothing breaks before you configure them). Structured output via `response_mime_type="application/json"` plus a response schema where supported; otherwise prompt-and-validate.
- `fake.py`: scripted responses per role, for offline tests.
- `ratelimit.py`, `usage.py`, `cache.py`: Section 6. **Both modes** go through the limiter and usage counter.

At the start of Phase U1, list available models with the SDK, choose the strongest Pro-tier and Flash-tier models that support structured JSON output, and record the names and the key's rate limits (from Google AI Studio) in the changelog.

---

## 4. Multi-agent architecture

```
question (+ history, user, options)
   │
   ▼
[R2 Policy guard — input]  rules first, Flash for unclear cases ──► refuse
   │
   ▼
[R1 Router]  Flash: route, entities, sub-questions, clarity ──► clarify (only if allowed) / else assume + warn
   │
   ├── fast path (single, clear metric or customer question) ──────────────┐
   ▼                                                                         │
[R3 Planner]  Pro: 1–4 typed steps                                           │
   │                                                                         │
   ▼                                                                         ▼
 for each step (in dependency order):
   metric   → [R4 Metric selector] Flash → compute_metric (code) ─ no fit → becomes an sql step
   sql      → [R5 SQL writer] Pro → sql_guard (code) → [R6 SQL checker] Flash → execute (code) → sanity (code)
   customer → get_customer (code) → [R8 Customer briefer] Flash
   text     → search_text (code) → [R7 Text analyst] Flash, no tools
   │
   ▼
[R3 Planner — replan]  optional, at most once: done, or ≤ 2 extra steps
   │
   ▼
[R9 Answer writer]  Flash: Answer fields; references result tables by step ID (never retypes data)
   │
   ▼
[R10 Grounding checker]  code (numbers) + Flash (claims) ──► one revision by R9 if unsupported
   │
   ▼
[R2 Policy guard — output]  rules + Flash ──► redact / block
   │
   ▼
Answer + trace + events          (same Answer model and entry points as single mode)
```

Principles:
- **Roles decide; code executes and checks.** No role runs queries, reads files or calls another role.
- **Each role sees only what its input contract lists** (privacy on the free tier, and better accuracy).
- **Checkers are independent:** the SQL checker never sees the writer's reasoning; the grounding checker sees evidence, not drafts.
- **Raw note and transcript text reaches only the text analyst** (no tools, fixed output). Only themes and IDs travel onward.
- **Reuse the existing pieces unchanged:** tools (`compute_metric`, `run_sql`, `get_customer`, `search_text`, `refuse`), `sql_guard` and trap rules, `sanity`, `periods`, input/output guards, serving DB, semantic files. Multi mode calls them; it doesn't fork them.

### 4.1 New code layout (additive)
```
c360/layer2/multi/
  state.py  controller.py  budget.py  events.py  context.py  compose.py
  roles/ base.py router.py policy.py planner.py metric_selector.py sql_writer.py sql_checker.py
         text_analyst.py customer_briefer.py answer_writer.py grounding.py
  prompts/<role>.md      # "version: N" front matter
c360/layer2/llm/ ratelimit.py usage.py cache.py   (+ extended base.py, gemini.py, fake.py)
c360/layer2/agent.py     # + run(); ask() unchanged
c360/api/                # extended or created (Section 9)
c360/layer2/eval/questions/ routing.yaml routing_real.yaml broken_sql.yaml broken_sql_real.yaml
                            injection.yaml grounding.yaml sensitive_allowed.yaml
tests/compat/  tests/layer2/multi/  tests/api/
```
New dependencies: `fastapi`, `uvicorn`, `filelock` (+ `sse-starlette` if convenient). Pin versions compatible with what's installed.

---

## 5. Controller, state, budgets and roles

### 5.1 `RunState` (`multi/state.py`)
```
run_id, question, history (≤ 5 prior Q/A summaries), user_id, mode, options, started_at
policy_in, routing, plan (steps with status), step_results {step_id: StepResult}, draft,
grounding, policy_out, answer, trace [TraceStep], budget (calls per tier, elapsed, degradations), warnings
```

### 5.2 Controller (`multi/controller.py`)
Explicit state machine; each state is a function `(state) -> next_state`:
```
START → POLICY_IN → ROUTE → (CLARIFY | REFUSE | PLAN | FAST_PATH)
PLAN → EXECUTE → REPLAN? → EXECUTE? → COMPOSE → GROUND → (REVISE → GROUND)? → POLICY_OUT → DONE
CLARIFY → DONE (only if allow_clarification; else continue to PLAN with the router's most likely reading)
any fatal error → FAIL_SAFE → DONE (partial Answer explaining what failed)
```
- **Fast path** (`C360_L2_FAST_PATH=on` by default): router route `metric` or `customer`, confidence ≥ 0.8, no sub-questions → one-step plan, no planner call.
- **Limits:** ≤ 4 planner steps; ≤ 1 replan adding ≤ 2 steps; ≤ 2 SQL writer attempts per step.
- **Dependencies:** later steps receive earlier steps' **summaries** (columns, row count, ≤ 10 rows), never full results.
- **Concurrency:** independent steps run concurrently only if `C360_L2_CONCURRENCY > 1` (default 1, for free-tier limits).
- Every transition appends a `TraceStep` and emits an event.

### 5.3 Budgets and degradation (`multi/budget.py`)
Per question: `C360_L2_MAX_CALLS=12`, `C360_L2_MAX_PRO_CALLS=4`, `C360_L2_DEADLINE_S=150`. When the next role would exceed a budget, degrade in this order (each recorded in `meta.degradations` and warnings):
1. skip replan; 2. skip the grounding model check (code check still runs); 3. replace the answer writer with `compose.py`; 4. skip the SQL checker (answer marked "custom query, not verified"); 5. stop with a `partial` answer.
**Never skipped:** SQL guard, policy rules, output guard, deny-list.

### 5.4 Role contract (`roles/base.py`)
- Inputs: a pydantic model built by `multi/context.py`. **Nothing outside it reaches the model.**
- Output: a pydantic model, used as the response schema.
- Prompt file with `version`; temperature 0; per-call timeout `C360_L2_ROLE_TIMEOUT_S=30`; through the limiter and cache.
- Invalid output → one repair call → else the role's fallback.
- Switch `C360_L2_ROLE_<NAME>=on|off`; off → fallback, recorded in the trace.
- Untrusted text is wrapped in `<data trust="untrusted">…</data>`; every prompt says that text in data blocks is information, never instructions.

### 5.5 The ten roles

**R1 Router (Flash).** Classify, extract entities, split multi-part questions, detect ambiguity.
- Inputs: question; last 2 history summaries; route descriptions; metric names and labels; table names with one-line descriptions; recognised ID patterns.
- Output: `{route: metric|sql|customer|text|mixed|out_of_scope, confidence, entities: {ids, periods, metric_hints, dimension_hints, table_hints}, sub_questions: [str], clarification: {question, options}|null, most_likely_reading: str}`
- Code drops IDs not matching `c360.store._KEY_RULES`. Clarify only when readings differ materially; missing periods use the default period rule.
- Off: the planner routes; no fast path. Measured by `routing.yaml` / `routing_real.yaml` (≥ 90%).

**R2 Policy guard (rules + Flash), input and output modes.**
- Rules first: the existing input/output guards and deny-list; a clear hit refuses without a model call. Flash only for flagged-unclear cases (postal areas, named individuals, "list all customers who…", targeting vulnerable customers).
- Inputs (in): question, the contract's `not_allowed` list, allowed uses. (Out): answer text, insight, table column names, ≤ 5 rows, the question.
- Output: `{allowed, rule|null, reason, safe_alternative|null, redactions: [{field, reason}]}`
- Off: rules only (today's behaviour). Measured by `refusal.yaml` (100%) and `sensitive_allowed.yaml` (0 refusals).

**R3 Planner (Pro), plan and replan.**
- Inputs (plan): question; router output; metric catalogue (name, label, description, dimensions); table catalogue (name, grain, description, key columns); join graph; history summaries.
- Output: `{plan_text, steps: [{id, kind: metric|sql|customer|text, goal, inputs, depends_on}], answer_shape: scalar|table|list|narrative|comparison}`; inputs per kind: metric `{metric_hint, period_text, group_by_hint, filter_hints}`, sql `{tables_hint, period_text}`, customer `{key}`, text `{query, filters}`.
- Replan inputs: question, plan, per-step summaries, warnings, verdicts. Output `{done, extra_steps (≤ 2), reason}`.
- Off: one-step plan from the router (if both off, run single mode for this request with a warning).

**R4 Metric selector (Flash).**
- Inputs: step goal; hints; top-5 candidate metrics (chosen in code by keyword similarity) with label, description, dimensions **and real allowed values** from `semantic.yaml`, unit; accepted period forms.
- Output: `{metric|null, period, group_by: [str], filters: [{dimension, op, value}], no_fit_reason|null}`
- Code checks metric, dimensions, `periods.resolve()`, allowed values; one repair on failure. No fit → the step becomes `sql`.
- Off: planner inputs used directly.

**R5 SQL writer (Pro).**
- Inputs: step goal; question; schema slice (hinted tables + join neighbours, ≤ 5 tables, columns, types, descriptions, allowed values); join graph; semantic rules and terms; trap messages; resolved period dates; on retry, guard/checker issues.
- Output: `{sql, purpose: analysis|data_quality, output_columns, assumptions}`
- Then code: `sql_guard.validate()` (one retry on rejection) → R6 → execute → `sanity.check()`.
- Can't be switched off in multi mode (an unanswerable sql step makes the answer `partial`).

**R6 SQL checker (Flash with checklist; optional independent mode on Pro).**
- Inputs: question; step goal; SQL; schema slice for referenced tables; rules; period dates. **Not** the writer's assumptions.
- Output: `{verdict: pass|fix|uncertain, checklist: {grain, filters, period, duplicates_excluded, stored_vs_recomputed, join_fanout, distinct_counts, output_matches_goal} (pass|fail|not_applicable), issues: [{item, problem, suggested_fix}]}`
- `pass` → execute; `fix` → back to R5 once, re-check once; still `fix` or `uncertain` → execute, mark "custom query, not verified", issues → warnings.
- Independent mode (`C360_L2_SQL_CHECK_INDEPENDENT=on`, default off): a second R5 call writes its own SQL without seeing the first; both are executed and compared with `eval/compare.py`; agree → pass, disagree → uncertain.
- Off: skip, "not verified". Measured by `broken_sql*.yaml`: catch ≥ 80%, false alarms ≤ 10%.

**R7 Text analyst (Flash, no tools).**
- Inputs: step goal; ≤ 30 snippets (≤ 400 characters each) with `id`, `case_id`, `date`, `source`, wrapped as data. Nothing else.
- Output: `{themes: [{theme, description, snippet_count, example_ids (≤ 5)}] (≤ 6), quotes: [{id, quote ≤ 120 chars}] (≤ 4), caveats}`
- Code: IDs must be in the input; quotes must be exact substrings of their snippet (else dropped with a warning); strip phones and emails from quotes.
- Off: wrapped snippets go to the answer writer (today's behaviour). Measured by `injection.yaml` (100% containment).

**R8 Customer briefer (Flash).**
- Inputs: trimmed customer document (existing `get_customer` trimming) and the step goal.
- Output: `{headline, situation (≤ 5), constraints, hardship, open_items, ids}`
- Code: every number must appear in the document; every active servicing constraint must be listed (code appends missing ones). Expose `briefer.brief(doc, goal)` as a public function for Layer 3 reuse.
- Off: the trimmed document goes to the answer writer.

**R9 Answer writer (Flash).**
- Inputs: question; plan text; per step: kind, goal, governed flag, metric label and status, result summary (columns, rows ≤ 20, row count), warnings, check verdicts; analyst themes; briefing. No raw notes, no SQL.
- Output: `{status: answered|partial, interpretation (2–4), answer_text (1–4 sentences), table_from_step: step_id|null, insight|null, follow_ups (≤ 3)}`
- **Tables are never retyped:** code attaches the referenced step's real rows (≤ 50).
- Off or out of budget: `compose.py` (deterministic). Output always goes through the existing `Answer` model and output guard.

**R10 Grounding checker (code + Flash).**
- Code: the existing number check, tracing each number to a step result (rounding and ratio↔percent allowed).
- Model inputs: answer text, insight, evidence summary. Output: `{claims: [{text, supported: yes|partial|no, evidence_step}], verdict: pass|revise}`
- `revise` → one revision by R9 → code re-check; remaining issues → warnings.
- Off: code check only. Measured by `grounding.yaml`: catch ≥ 80%, false alarms ≤ 10%.

---

## 6. Free-tier infrastructure (both modes)

### 6.1 Rate limiter (`llm/ratelimit.py`)
- Per model: requests per minute and per day: `C360_L2_RPM_PRO`, `C360_L2_RPD_PRO`, `C360_L2_RPM_FLASH`, `C360_L2_RPD_FLASH`, `C360_L2_RPM_SINGLE`, `C360_L2_RPD_SINGLE` (single model; if it's the same model as Pro, share the Pro counters). Defaults are deliberately low; set them from the key's actual limits.
- Token bucket per minute plus a daily counter, persisted in `outputs/layer2/llm_usage.json` with `filelock`, shared by CLI, API and evaluation. Daily reset hour configurable (`C360_L2_RPD_RESET_UTC_HOUR`).
- Over the per-minute limit → wait. Over the daily cap → `BudgetExhausted` (the controller degrades or returns `partial`; the API returns 429 `budget_exhausted`).
- Provider 429 → honour any retry delay, else back off 2/4/8 s, then `RateLimited`.
- `python -m c360.layer2.llm.usage` prints usage and remaining budget per model.

### 6.2 Record and replay cache (`llm/cache.py`)
- `C360_L2_LLM_CACHE=off|record|replay|record_replay`. Defaults: tests `replay`; evaluation `record_replay`; API and CLI `off`.
- Key: SHA-256 of (model, role or "single", prompt version, rendered prompt, output schema). Store: `outputs/layer2/llm_cache.sqlite`.
- `replay` miss → error; `record_replay` miss → live call, then store. Reports show live vs replayed calls.

### 6.3 Data minimisation
Enforced in `multi/context.py`, with one test per role asserting the rendered prompt contains only allowed fields: no names, phones, emails, addresses, DOB, postal codes or protected attributes; rows ≤ 20 per role (≤ 10 for the planner); snippets ≤ 30 × 400 characters. README and acceptance report state: "On the Gemini free tier, prompts may be used by Google to improve its products. Acceptable here because the dataset is synthetic; use a paid tier or private deployment for real customer data."

---

## 7. Events

`on_event(name, payload)` with `events="basic"` (default, today's behaviour) emits only the events single mode emits today (typically `plan`, `tool_started`, `tool_finished`). Inventory them in U0 and keep them identical. With `events="detailed"`:

| Event | Payload |
|---|---|
| `started` | `{run_id, mode}` |
| `role_started` | `{role, step_id?, summary}` |
| `role_finished` | `{role, step_id?, ok, verdict?, summary, model_tier, cached, duration_ms}` |
| `plan` | `{text, steps: [{id, kind, goal}]}` |
| `tool_started` / `tool_finished` | as today |
| `check_result` | `{name, step_id?, verdict, details}` |
| `replan` | `{done, extra_steps}` |
| `degraded` | `{what, reason}` |
| `answer` | `{answer, trace}` |
| `error` | `{error, code}` |

In multi mode with `events="basic"`, map role activity onto the basic events only (e.g. the plan text as `plan`, tool steps as `tool_started`/`tool_finished`). Payloads never contain result rows, note text or customer documents.

---

## 8. Evaluation additions

### 8.1 Harness options (additive)
```
python -m c360.layer2.eval --set <name> [--mode single|multi] [--roles-off a,b] [--limit N]
                           [--resume <run_id>] [--max-live-calls N]
python -m c360.layer2.eval --ablation --set benchmark --limit 15
```
- `--resume` continues an interrupted run (state saved per question in `outputs/layer2/eval_runs/<run_id>.json`).
- `--max-live-calls` stops cleanly before a call budget (default: remaining daily budget minus 10%).
- Reports add mode, roles on/off, calls per question (pro/flash/single, live/replayed), degradations, per-role verdict counts. Existing report fields stay.

### 8.2 New sets

| Set | Built by | Content | Pass |
|---|---|---|---|
| `routing.yaml` | you (synthetic) | ≥ 40 questions + expected route and IDs | ≥ 90% |
| `routing_real.yaml` | you (real) | the 35 benchmark questions labelled with route + IDs (evaluation file only, never a prompt), plus ≥ 10 new real-data questions | ≥ 90% |
| `broken_sql.yaml` / `broken_sql_real.yaml` | `synth --broken-sql-from <file>` | ≥ 30 correct queries (synthetic truth queries / **your verified hand-written real-data queries from the runbook work**) plus mutants: drop duplicate filter; `CURRENT_DATE`; `count(*)` instead of `count(DISTINCT…)` across a join; recomputed instead of stored bucket; shifted or widened period; dropped filter; wrong join key; wrong group-by | R6 alone: catch ≥ 80%, false alarms ≤ 10% |
| `injection.yaml` | `synth` | ≥ 10 crafted snippet sets with injection attempts (no database writes) | R7 alone and end to end: 100% containment |
| `grounding.yaml` | you | ≥ 25 (evidence, draft) pairs; half with planted unsupported claims | R10 alone: catch ≥ 80%, false alarms ≤ 10% |
| `sensitive_allowed.yaml` | you | ≥ 15 legitimate but sensitive-sounding questions | 0 refusals |

Role-level sets cost one Flash call per item; run them first and often.

### 8.3 Comparison (ablation) report
`--ablation`: same subset in `single`; `multi` all on; `multi` minus each of router, metric_selector, sql_checker, text_analyst, answer_writer, grounding. Output `outputs/layer2/ablation_<ts>.md`: accuracy, refusal, injection, calls (pro/flash), median latency, degradations. On the free tier use `--limit 15`, the cache and `--resume` across days.

### 8.4 Targets (real data)
- multi-mode benchmark and held-out-real accuracy **≥ the single-mode baseline from U0** (if not, record why; single stays default);
- routing ≥ 90%; SQL checker catch ≥ 80% / false alarms ≤ 10%; grounding catch ≥ 80% / false alarms ≤ 10%;
- refusal 100%; sensitive-allowed 0 refusals; injection 100%; no answer containing phones, emails or deny-listed fields;
- median calls per multi-mode question ≤ 8; latency recorded (no fixed target on the free tier).

---

## 9. HTTP API (`c360/api/`), same endpoints for both modes

**If `c360/api/` exists:** keep every existing route and behaviour (frozen in U0) and extend it additively as below. **If it doesn't exist:** create it exactly as below.

The frontend contract is the frontend repo's `contracts/openapi.yaml`. Keep a copy at `c360/api/openapi_contract.yaml`; a test checks FastAPI's generated schema matches it on paths, methods and required fields. Contract changes go into both copies and both changelogs.

Run: `uvicorn c360.api.app:app --host 127.0.0.1 --port 8000`. Base `/api/v1`. Headers in: `X-C360-User`, `X-C360-Role`; out: `X-C360-Request-Id` on every response.

| Endpoint | Behaviour (identical route for single and multi) |
|---|---|
| `GET /health` | existing fields kept; add `models: {single, pro, flash}`, `mode` (default), `roles` (on/off map), `llm_budget: {pro_remaining_today, flash_remaining_today, single_remaining_today, resets_at_utc}`, capability `multi_agent`; `status`: `down` if the serving DB can't open, `degraded` if MongoDB is unreachable or a daily budget is used up |
| `POST /ask` | body `{question, conversation_id?, include_trace?, options?: {mode?, roles_off?, allow_clarification?, events?}}`; `mode`/`roles_off` honoured only for allowed roles (Section 3.1); calls `agent.run()` in a worker thread; returns `{conversation_id, turn_id, answer, trace?}` |
| `POST /ask/stream` | same body; SSE: basic events by default, detailed when `options.events = "detailed"`; always `started` first and `answer` or `error` last |
| `GET /customers/{key}` | trimmed document (existing trimming); 400 unknown pattern; 404 not found |
| `GET /metrics` | from `metrics.yaml`: key, label, description, status, unit, dimension names |
| `GET /examples` | `c360/api/examples.yaml` (2 per route, real-data examples, one clarification example); a test checks each answers |
| `GET /history?limit=` | last N audit entries for the user |
| `POST /feedback` | append to `outputs/layer2/feedback.jsonl` |

Operational rules: one answer at a time by default (`C360_API_WORKERS=1`); queue ≤ 5, then 429 `rate_limited` with `Retry-After`; per-user 6 questions/minute; `BudgetExhausted` → 429 `budget_exhausted` with message "Today's model budget is used up. Answers resume after <time UTC>."; provider failure 502; serving DB unavailable 503; validation 400. Body `{error, code, request_id}`. Conversations in memory (2 h TTL, last 5 turns as summaries). CORS for `http://localhost:5173` in development only; in production serve the frontend build from the same app. Never return raw note text beyond the `Answer`, or untrimmed customer documents.

The frontend (`SPEC_FRONTEND.md`) should send `options.allow_clarification: true` and `options.events: "detailed"`; other clients, including Layer 3, get today's behaviour unless they opt in.

---

## 10. Running on the real data (merged from the updated runbook)

### 10.1 Environment
```bash
export GEMINI_API_KEY=...            # never print it
export C360_L2_MODEL=<existing single-mode model>        # unchanged
export C360_L2_MODEL_PRO=<chosen in U1>  C360_L2_MODEL_FLASH=<chosen in U1>
export C360_L2_RPM_PRO=... C360_L2_RPD_PRO=... C360_L2_RPM_FLASH=... C360_L2_RPD_FLASH=...
export C360_L2_RPM_SINGLE=... C360_L2_RPD_SINGLE=...
export C360_L2_LLM_CACHE=record_replay                   # for evaluation shells only
unset C360_WAREHOUSE C360_L2_MONGO                       # real warehouse and MongoDB
python -m c360.layer2.llm.usage
```
Start with an empty cache and usage file (synthetic-era files, if any, are not valid here). DuckDB allows one writer per file: don't run Layer 1 while working.

### 10.2 Order of work on real data
1. **Offline tests first:** `pytest tests/layer2 tests/compat tests/api -q -m "not live"`.
2. **Doctor:** `python -m c360.layer2.doctor` must still exit 0 (the upgrade must not change data configuration). If it doesn't, fix the code regression, not the config.
3. **Role-level evaluations** (cheap): `routing_real`, `refusal`, `sensitive_allowed`, `broken_sql_real`, `grounding`, `injection`. Fix by improving role prompts with general wording and examples (never benchmark questions); bump versions.
4. **End to end, multi mode:** `--set benchmark --mode multi`, `--set heldout_real --mode multi`, with `--max-live-calls` and `--resume`.
5. **Comparison:** `--ablation --set benchmark --limit 15` (spread across days if needed).
6. **Default mode decision:** set `C360_L2_MODE=multi` only if multi mode meets Section 8.4, including accuracy ≥ the U0 single-mode baseline. Otherwise keep `single`. Record the decision and numbers.

Failure causes specific to multi mode, and where to fix them:

| Cause | Fix |
|---|---|
| wrong route or unnecessary clarification | router prompt (descriptions, examples, thresholds) |
| planner over-splits simple questions | planner prompt; fast-path threshold |
| metric selector picks a wrong dimension or value | value lists in `semantic.yaml`; candidate preselection keywords |
| SQL writer missing a table | `schema_slice` hints / join graph in `semantic.yaml` |
| checker false alarms on correct queries | checker checklist wording |
| answer drops a qualifier (e.g. placeholder metric) | writer prompt; grounding should catch it |
| frequent degradations | per-question budgets, fast path, router confidence |
| data-level errors (wrong metric definition, term, join, date) | same fixes as the original runbook: `metrics.yaml`, `semantic.yaml`, trap rules; these also affect single mode, so re-run single-mode benchmark afterwards to confirm no regression |

### 10.3 Performance and budget
- For the 5 slowest questions per mode: time their SQL on the serving DB; add pre-aggregated tables to `l2_views.yaml` only if needed (confirm identical results; `publish_l2` rebuild). Don't raise `C360_L2_SQL_TIMEOUT` above 60 s.
- Report median and p90 latency and average calls per question per mode, how much time was rate-limit waiting, and **questions per day the key supports** in each mode (daily caps ÷ average calls). The demo plan must fit within that.

### 10.4 API on real data
- Start uvicorn; `GET /health` shows real serving build, models, roles, budget.
- Update `c360/api/examples.yaml` to real-data examples and check each.
- If the frontend repo is on this machine: `pnpm contract:check --base-url http://127.0.0.1:8000/api/v1` passes. Otherwise run `pytest tests/api` and record the contract check as pending.
- One streamed question per mode: event order matches Section 7 (basic and detailed).
- Run Layer 3's own tests (if any) against the upgraded Layer 2 **without changing Layer 3 code**; record results.

### 10.5 Troubleshooting (additions)

| Symptom | Fix |
|---|---|
| a compatibility test fails | you changed a public behaviour; restore it, add new behaviour behind an option |
| `BudgetExhausted` | continue tomorrow with `--resume`, or work offline with `replay`; never raise caps above the key's real limits |
| frequent `RateLimited` waits | keep concurrency 1; role-level sets first; rely on the cache |
| replay cache miss in tests | expected after a prompt version bump; re-record only that role's fixtures |
| Gemini rejects a response schema | flatten that role's output model (no unions, simple enums); keep field names |
| many `degraded` warnings | budgets too tight, or fast path not triggering (check router confidence in traces) |
| Layer 3 behaves differently | check whether it now receives multi mode via the global default; it should only if the team chose that; otherwise set `C360_L2_MODE=single` |

---

## 11. Phases and acceptance criteria

Each phase ends with all offline tests green (`tests/layer2`, `tests/compat`, `tests/api`) and a changelog entry.

**U0: Baseline and freeze** (Section 1). **Accept:** see 1.4.

**U1: Model infrastructure** (Sections 3.3, 6). Tiers, structured output, limiter with fake clock, persisted usage + CLI, cache, fake LLM per role; single mode routed through the limiter and usage counter without behaviour change.
**Accept:** limiter tests (minute wait, daily `BudgetExhausted`, cross-process sharing); 429 backoff test; cache hit/miss/replay-miss and per-role invalidation tests; compatibility tests unchanged and green; one live smoke call per tier (`@pytest.mark.live`).

**U2: Shared interface extension** (Sections 2, 3.1, 3.2, 7). `agent.run`, mode switching on every surface, additive `Answer`/`TraceStep` fields, opt-in clarification and detailed events, audit line additions.
**Accept:** compatibility tests green; new tests: `mode` honoured on Python/CLI/API with role restrictions; with defaults, outputs are byte-identical in structure to U0 snapshots; `needs_clarification` never returned without opt-in.

**U3: Controller skeleton** (Section 5.1–5.4) with all roles faked.
**Accept (offline):** tests for every path: refuse at policy-in; clarify (opt-in) and assume-and-warn (default); fast path; 3-step plan with dependencies; replan; SQL guard rejection → retry → checker fix → retry → pass; `uncertain` → "not verified"; grounding revise; policy-out redaction; each degradation in order; each role off → fallback; fatal error → partial; events basic vs detailed; no rows or note text in events.

**U4: Router and policy guard.** **Accept (live, cached, real data):** `routing_real` ≥ 90%; refusal 100%; sensitive-allowed 0 refusals.

**U5: Planner and metric selector.** **Accept:** plan validity tests; metric parameter exact-match ≥ 90% on real metric questions with known parameters (create ≥ 15 from verified metrics); fast path used for ≥ 60% of single-metric questions.

**U6: SQL writer and checker.** **Accept:** `broken_sql_real` catch ≥ 80%, false alarms ≤ 10%; schema slices ≤ 5 tables and no deny-listed columns; independent mode agree/disagree tests (fake LLM).

**U7: Text analyst and customer briefer.** **Accept:** injection 100%; quote substring and briefer constraint-completeness enforced (tests); data-minimisation tests for both prompts; `briefer.brief()` public function documented in `PUBLIC_INTERFACE.md`.

**U8: Answer writer and grounding.** **Accept:** grounding catch ≥ 80%, false alarms ≤ 10%; table always equals the referenced step's real rows (test); composer valid for every route.

**U9: End-to-end evaluation, comparison, default decision** (Section 10.2). **Accept:** Section 8.4 targets or documented reasons; ablation report saved; default mode decided and recorded; single-mode benchmark re-run shows **no regression** vs U0 (within ±1 question).

**U10: API** (Section 9, 10.4). **Accept:** API tests (every endpoint and error code, SSE basic and detailed order, queue/429, override restrictions) with fake LLM; contract-match test; contract check against the running API (or recorded as pending); Layer 3 tests pass without Layer 3 changes.

**U11: Documentation and acceptance report.** Update `docs/layer2/README.md` (modes and switches, roles, models and limits, cache, usage CLI, API, free-tier privacy statement, reading the ablation report) and `docs/layer2/PUBLIC_INTERFACE.md` (old surface unchanged; new additions listed). Append to `docs/layer2/acceptance_report.md` a section "Multi-agent upgrade": models and limits; per-mode results for benchmark, held-out-real, paraphrase, refusal, sensitive-allowed, injection; role-level results; ablation table; default mode decision; calls per question, questions per day, latency (median, p90); compatibility test results; Layer 3 test results; five example Q&As in multi mode (`ask --mode multi --trace`).
**Accept:** a fresh shell following the README can run: ask (single and multi), role-level evaluations, API, usage CLI; all tests green; merge `layer2-multiagent` into the main branch and tag `layer2-v2`.

---

## 12. Rollback

- Instant: `C360_L2_MODE=single` (no code change). Multi-mode code stays dormant.
- Full: `git checkout layer2-v1-baseline` and restore files from `outputs/layer2/upgrade_backup/<timestamp>/`. The serving DB and data configuration are untouched by this upgrade, so no Layer 1 or `publish_l2` rerun is needed.

## 13. Out of scope

Agent frameworks; default parallel execution; fine-tuning; persistent conversation storage; token-by-token answer streaming; Layer 3 features (apart from exposing `briefer.brief()`); any change to Layer 1 beyond what already exists; any write access for roles except audit, usage, cache and feedback files.

## Appendix: Changes since the original SPEC_LAYER2.md and DB_MACHINE_RUNBOOK.md, all included above

| Source | Change | Where here |
|---|---|---|
| SPEC_LAYER2 §0.6 | `agent` entry point with history, user, events, mode; tolerant schemas; all Gemini access behind `llm/base.py` | §2, §3 (as `agent.run`, keeping `ask`) |
| Multi-agent spec | controller, ten roles, budgets, events, model tiers, rate limiter, cache, data minimisation, new evaluation sets, ablation, HTTP API | §4–9 |
| Runbook (updated) | free-tier and budget rules; prompt versioning; new env vars; role-level evaluation before end to end; `routing_real` and `broken_sql_real`; multi vs single comparison and default decision; questions-per-day budget check; API on real data; new troubleshooting rows; acceptance report additions | §0.4, §8, §10, §11 |
| New in this version | Phase U0 baseline and freeze; compatibility rules; opt-in clarification and detailed events; mode override via API option with role restriction; Layer 3 protection; rollback | §1, §2, §3.1, §7, §12 |
