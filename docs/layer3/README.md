# Layer 3: act on the Collections 360 record

Layer 1 builds one record per customer and Layer 2 answers questions about it. Layer 3 acts on it,
with a person approving anything that affects a customer.

| App | For | What it does |
|---|---|---|
| **C360 Terminal** (`/terminal`) | Agents, hardship specialists, team leads | Portfolio monitor, prioritised worklist, customer 360 with a next best action (approve / edit / reject), post-call summary and QA scoring, customer requests waiting for a person, policy search |
| **Payment help** (`/app`) | Customers | What I owe, hardship options for my account, choose a payment date, pay, contact preferences or stop-contact request, dispute, assistant |

Both run on the real Maple dataset and share one set of engines. Nothing needs a model key.

## Run it

```bash
pip install -r requirements-layer2.txt -r requirements-layer3.txt      # Python 3.11+
# In this repo the release sits at ./maple_collections_release (found automatically; or export MAPLE_DATA)
export C360_L3_SOURCE=mongo                   # integrated mode: Layer 1's golden record (needs MongoDB running)
export C360_L3_WRITEBACK=mongo                # optional: also append decisions to the customer's MongoDB record

# the dataset: unzip maple_collections_release.zip so that maple_data/maple_collections_release/data exists
# (or export MAPLE_DATA=<folder with data/, files/, labels/>)

python -m c360.layer3.data.build_local       # ~30 s: builds warehouse_l3/l3.duckdb (collections customers only)
python -m c360.layer3.postcall --eval        # post-call accuracy against the 500 labelled calls
uvicorn c360.layer3.api:app --port 8300      # then open http://127.0.0.1:8300
pytest tests/layer3 -q                       # 24 tests
```

Demo: terminal command `CS-2026-614370` (Zoe Ayers, illness hardship, promise due 14 Oct, no SMS consent);
customer app sign-in `CS-2026-614370` / `Ayers`, or `CS-2026-855160` / `Marshall`.

Terminal commands: `MON`, `WORK [QUEUE] [HARDSHIP|PROMISE|BROKEN|DISPUTE|VULNERABLE|HOLD]`, any customer / case /
account id, `CALL <transcript id>`, `APPR`, `QA`, `POL <question>`, `ROLE TEAM_LEAD`. F1–F8 switch panels; `/` or
Ctrl+K focuses the command line.

## How it fits with Layers 1 and 2

The engines read the **Collections 360 customer document**: the shape Layer 1 publishes to MongoDB and Layer 2
serves at `GET /customers/{key}` (SPEC_FRONTEND.md §4.4). Where that document comes from is one setting:

| `C360_L3_SOURCE` | Customer document from | Use |
|---|---|---|
| `mongo` (**integrated mode, recommended**) | Layer 1's golden Collections 360 record in MongoDB (`c360.store.C360Store`), enriched from the local store by case and account id (promise history, recent contacts, assigned agent, full case columns). The golden record wins wherever both have a value: Layer 1's identity (merged CRM records, card / loan / CIF links), recomputed buckets and accounts. Accepts every Layer 1 key: case, account, CRM id, `C360-…`, card ref, borrower ref, CIF | With MongoDB running (`podman start c360-mongo`) |
| `local` (default) | `c360/layer3/data/source.py` assembles it from the Maple CSVs in DuckDB, linking records inside the collections domain by CRM id and case account ids | Today, while the Layer 2 API is being built |
| `api` | `C360_API_BASE_URL` (default `http://127.0.0.1:8000/api/v1`), the Layer 2 HTTP API, so identity resolution and the golden record come from Layer 1 | Once the Layer 2 API is up |

Portfolio aggregates and the worklist always come from the local store (they aren't in the Layer 2 contract).
Every decision and customer request is written to `outputs/layer3/actions.sqlite`; with `C360_L3_WRITEBACK=mongo`
it is also appended to the customer's MongoDB document through `C360Store.append_interaction`, the write-back
path Layer 1 provides.

## The engines

| Module | What | Grounded in |
|---|---|---|
| `nba.py` | Next best action per case: action, channel, timing window, amount, programme, script, approver level, reasons with citations, blocked channels, safeguards. `check()` lists conflicts; Approve is refused (UI and server) while any exist | POL-COLL-001 (hours, 3 calls in 7 days, consent, cease, representatives, insolvency stay), POL-COLL-004 (hardship first, plan reviews, vulnerable customers), PRC-COLL-010 (promises), PRC-COLL-012 (disputes) |
| `hardship.py` | Eligibility for each programme in force, with the reason when not eligible, and an estimated payment | `hardship_programs.csv` (current versions only), HPT-2026, POL-COLL-004 §4.2–4.3 |
| `postcall.py` | Transcript to fields (hardship, promise amount and date, dispute, cease request, vulnerability, insolvency, legal threat, sentiment, next step), a summary in the gold-label style, and the 10-item QA checklist with evidence (second and quote) | `qa_checklist.csv`; English and French calls |
| `policy.py` | Section-level search over the reference documents. Superseded versions are never returned; FAQ and call scripts rank behind the policy that governs the answer | `reference_documents.csv`, `files/docs/` |
| `assistant.py` | Customer assistant: deterministic intents over the customer's own record and current policy, never invents numbers, never asks for payment after hardship is mentioned | the above |

### Post-call accuracy (500 human-labelled calls, `labels/call_transcripts_labels_public_500.csv`)

| Field | Accuracy | Field | Accuracy |
|---|---:|---|---:|
| Promise mentioned | 100% | QA checklist items (5,000) | 98.7% |
| Dispute and type | 100% | QA total score, exact | 95.6% |
| Complaint, third party, insolvency | 100% | Coaching required | 96.4% |
| Cease request | 99.8% | Promise amount / date | 96.0% / 92.5% |
| Hardship reason | 91.0% | Next step | 92.4% |
| Vulnerability | 91.0% | Summary, exact string match | 81.4% |
| Hardship level | 88.6% | Sentiment | 73.0% |

The QA score rule (100 minus the weight of each failed item) and the coaching rule (any critical item failed)
reproduce the gold labels exactly; remaining QA misses are item-level judgement calls. Rules only: the held-out
labels are untouched, and a model can be added for the weaker fields (sentiment, hardship nuance) once a key is set.

## Safeguards built in

- **Human in the loop.** Recommendations and customer requests are proposals. Approve needs the right role
  (agent, hardship specialist, team lead per programme), edit and reject need a reason, and the server re-checks
  conflicts on every approval. Approve is never the default focus.
- **Fair treatment.** Protected attributes are never loaded into the Layer 3 store; model scores that use them are
  dropped at build time. Vulnerability only adds safeguards (certified agent, vulnerable-customer script) and never
  lowers priority. Constraints and hardship are always visible at the top of the customer view.
- **Policy-correct contact.** Permitted local hours, statutory holidays (30 Sep), Sunday limits, the 3-calls-in-7-days
  cap, consent per channel, cease-communication (letters only), representatives, and the insolvency stay.
- **Customers see only their own record**, and never notes, scores, agent ids or full account numbers.
- **No HTML injection.** Both web apps build the DOM from text nodes; no server text is rendered as HTML.

## Agent terminal: groups, similar customers, charts, plain wording (4 Oct)

- **Groups (F9, `GROUPS SECTOR|INDUSTRY|EMPLOYER|AREA|CITY|PROVINCE`)** compare open cases by job sector, industry (NAICS),
  employer, postal area, city or province. For each group the screen shows customers, open cases, the amount overdue,
  average days late, the share with hardship, the share who lost work income, the share who broke a promise, the
  promise-kept rate, and cases opened in the last 60 days, with bar charts and rings. Click a group to open its
  worklist.
- **Postal area search (`AREA L4W 2A1`):** type a full postal code or just its first 3 characters (the FSA). Only the
  area is stored, never the full postal code.
- **"Possible layoffs" alert:** flags employers where several customers recently fell behind after losing work
  income, so hardship options can be offered early. It prompts a person and decides nothing.
- **Similar customers (customer screen):**
  - Shows others at the same employer, in the same job sector within the same postal area, and in the same postal
    area.
  - Gives figures and case ids only, never other customers' names.
  - Adds a layoff warning when it applies.
- **New charts:**
  - Overview: hardship reasons, open cases by job sector, outcomes of September calls, promises due over the next 14
    days, and open cases by product.
  - Customer screen: a 24-month card payment-history strip and a promise timeline.
  - Groups: bars and rings.
- **Plain wording:**
  - Jargon replaced: DPD → days late, RPC → reached the customer, PTP → promise, bucket → days-late band,
    queue → team, NDNCL → national do-not-call list.
  - Raw codes replaced by readable labels: action names, call-script names, hardship programme names and reasons.
  - Function keys renamed: OVERVIEW, WORKLIST, CUSTOMER, CALL REVIEW, APPROVALS, CALL QUALITY, POLICY, GROUPS.
  - The help screen has a glossary. The old commands (MON, WORK, APPR, QA, POL) still work.
- **Data and API:**
  - The store gained `fsa`, `occupation_category`, `industry_naics2` and `employer_name` (rebuild with
    `python -m c360.layer3.data.build_local`).
  - New endpoints: `GET /api/l3/groups` and `GET /api/l3/cases/{id}/peers`.
  - `worklist` takes `group_by`, `group` and `area`.
- **Groups never feed a decision or the risk model.** They include no protected attributes. Tests:
  `tests/layer3/test_groups.py`.

## ASK screen: plain-English questions answered by Gemini (F10)

- **Typing a question:** type a question in the terminal (F10, or `ASK <question>` in the command bar). It goes to
  the Layer 2 agent through the Layer 2 HTTP API, which is mounted in this server at `/api/v1`.
- **What comes back:**
  - the answer;
  - how the question was read;
  - the table, with an automatic bar chart;
  - **the SQL, or the policy sections, notes, transcripts and records used**;
  - the checks and the AI's steps;
  - follow-up questions you can click.
- **Model, timing, feedback, refusals:** each answer shows which Gemini model answered and how long it took, and has
  thumbs up/down feedback. Refusals are shown clearly. Follow-up questions keep the conversation's context (5 turns).
- **Model rotation:** set `C360_L2_MODELS` in `~/.config/c360/layer2.env` and the rotation keeps answering under
  per-minute limits.
- **Staff only:** the screen is for staff; it is not in the customer app.

Run: `. ~/.config/c360/layer2.env && uvicorn c360.layer3.api:app --port 8300`, then open
http://127.0.0.1:8300/terminal and press F10. The Layer 2 API can also run alone:
`uvicorn c360.api.app:app --port 8000`. See `c360/api/app.py` for the routes.

## Log a conversation (4 Oct)

Paste a transcript or notes on a customer's page. The system extracts hardship, promises, disputes, stop-contact
requests and more, each with the line it came from. The agent corrects and confirms, and follow-ups go to the
approvals queue. Protective findings (stop contact, bankruptcy mention) hold contact immediately. Guide:
[CONVERSATION_LOG.md](CONVERSATION_LOG.md). Spec: `SPEC_L3_CONVERSATION_EXTRACT.md` (built, core scope).

## AI next best step buttons (4 Oct)

Spec: `SPEC_L3_AI_NBA.md` (this folder). Code: `c360/layer3/ai_nba.py`, `prompts/ai_nba_agent.md`,
`prompts/ai_nba_customer.md`. Tests: `tests/layer3/test_ai_nba.py`.

- **Agent terminal:** on a customer page, a highlighted strip near the top holds **ASK AI FOR THE NEXT BEST ACTION**
  (and **+ LOG A CONVERSATION**). The AI draft opens inside that strip; the rules-based panel below is unchanged.
  - **What it shows:** the suggested step, whether it agrees with the rules, why (with policy citations), talking
    points and things to watch out for.
  - **What gets flagged:** any conflict with the contact rules (shown in red), and anything removed because the data
    did not support it.
  - **USE AS EDIT:** pre-fills the existing CHANGE IT form. The agent still gives a reason and approves through the
    normal flow, so the contact rules and role rules still apply.
  - **ASK AGAIN:** team leads can ask for a fresh draft.
- **Customer app:** on the home page, a highlighted **AI next best step** card (**Show my AI next best step**), placed above the other options, shows one suggested step in
  plain words. Its button opens an existing flow: apply for help, choose a payment date, pay, contact preferences,
  question a charge, or ask for a call. Every request still lands as "pending review".

**The AI suggests; a person decides.** Nothing the model returns is executed, sent or saved as a decision. The model is
called only when someone clicks.

**What the model sees:** a compact facts object.
- **Agent facts:** case figures, promises, offers, contact constraints, hardship and eligible programmes, the
  rules-based recommendation, up to 5 masked notes, and 3 policy excerpts.
- **Customer facts:** only what the app already shows the customer.
- **Never sent:** names, contact details, protected attributes, employer, agent ids or other customers.

**Checks in code after every call:**
- **Grounded numbers:** every number must appear in the facts, otherwise the sentence is removed.
- **Citations:** each must be one the facts contain, otherwise it is dropped.
- **Masking:** phone numbers and emails are masked.
- **Banned wording:** threats, legal action, credit-score warnings and pressure are removed.
- **Amount and programme:** the amount must be within the amount overdue, and the programme must be an eligible one.
- **Contact rules:** the suggestion is run through `nba.check`, and conflicts are shown, never hidden.
- **Customer-side rules:**
  - hardship first: never "pay" or "promise" when hardship is on file;
  - a stop-contact request is respected: no callback suggestion;
  - any internal term (risk, score, band …) sends the reply to the standard suggestion instead.

**Works without the model.** With AI off, out of quota, timed out or giving invalid output, both buttons show the
standard rules-based suggestion, clearly labelled.

**Configuration:**

| Setting | Default | Meaning |
|---|---|---|
| `C360_L3_AI` | `off` | `on` enables model calls (needs `GEMINI_API_KEY`) |
| `C360_L3_AI_MODEL` | `C360_L2_MODEL` | Gemini model; several can be listed, comma-separated, tried in order |
| `C360_L3_AI_TIMEOUT_S` | 25 | per call |
| `C360_L3_AI_DAILY_CAP` | 15 | live calls per day; cache hits do not count |
| `C360_L3_AI_CACHE` | `outputs/layer3/ai_nba.sqlite` | response cache and daily usage |

To switch it off: `export C360_L3_AI=off`. `GET /api/l3/health` reports
`ai_nba: {enabled, model, calls_today, daily_cap}`.

**Audit:** every click writes an `ai_nba_suggestion` record with the audience, source (ai or fallback), model, prompt
version, cached flag and the validated suggestion. It never stores the prompt or notes. **USE AS EDIT** writes
`ai_nba_used`.

**Live check (4 Oct, case CS-2026-614370):**
- **Agent:** the AI suggested a call to discuss hardship options and offer "Skip a payment", differing from the rules'
  payment reminder. It gave cited reasons and a warning that SMS is blocked.
- **Customer:** it suggested applying to skip a payment.
- **Screenshot:** `docs/screenshots/ai_1_agent_suggestion.png`.

**Not done:** a streaming UI; French prompts; a browser click-through of the customer card (the API and the fallback
are tested).

## Not done yet

- `C360_L3_SOURCE=api` is written against the SPEC_FRONTEND contract but untested (Layer 2 is a CLI in this repo, with no HTTP API). Use `mongo` for the integrated mode.
- Customer sign-in is a demo (reference number and last name, in-memory sessions); real auth is out of scope.
- No model in the loop yet. Natural next steps with a Gemini key: rewrite summaries for agents, a second opinion on
  sentiment and hardship, and free-form assistant answers grounded in `policy.py` hits.
- Voice: the 2,000 WAV files aren't used; the transcripts already cover the calls.
