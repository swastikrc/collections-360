# Collections 360 — CD Projekt Blaxk

CIBC Collections Hackathon, build phase. Team: Aarav Muni, Shubham Gupta, Divyansh Goyal, Swastik Roy Chowdhury.

Our design: **L1** build one record per customer → **L2** ask it in plain English → **L3** act on it
(next best action, agent assist, transcripts written back). All layers read from and write to the same record.

> Status: **Layer 1 (Data Product Factory)** built; **Layer 2 (plain-English questions)** adapted to the real data
> (see `docs/layer2/README.md`, `CHANGELOG_LAYER2.md`); **Layer 3 (act on the record: agent terminal, next best
> action, post-call QA, customer app)** merged and reading the golden record from MongoDB (see `docs/layer3/README.md`);
> **feature store + promise risk model** built: one feature definition for training and live scoring, structured and
> text features, out-of-time AUC 0.888, plain-English reasons inside the next best action, fairness check
> (see `docs/features/README.md`).

---

## How to run

Requirements: Python 3.11+, ~25 GB free disk, 16 GB+ RAM.

```bash
pip install duckdb pandas pyyaml pydantic pymongo anthropic   # anthropic only for the optional LLM review step

# the Collections 360 NoSQL store: MongoDB 7 (podman or docker)
podman run -d --name c360-mongo -p 127.0.0.1:27017:27017 \
  -v $PWD/warehouse/mongo:/data/db:Z docker.io/library/mongo:7 --wiredTigerCacheSizeGB 6
# (override with C360_MONGO_URI / C360_MONGO_DB)

# data (see the build-phase instructions)
hf download nuxsh/maple-collections-hackathon --repo-type dataset --local-dir maple_data
cd maple_data && unzip maple_collections_release.zip && cd ..
export MAPLE_DATA=$PWD/maple_data/maple_collections_release   # default: ./maple_collections_release

python run_layer1.py                 # full build, ~8 min on a 22-core laptop
python run_layer1.py --from identity # resume from a step: bronze|curated|identity|linkage|golden|publish|dq
python run_layer1.py --export-json   # also dump all MongoDB documents as JSON Lines
python run_layer1.py --from linkage --llm-review   # optional: LLM adjudicates the review queue (needs ANTHROPIC_API_KEY)
```

Look up any customer by **any key from any system** (case id, account id, CRM id, card ref, borrower ref, CIF):

```bash
python -m c360.store CS-2026-614370
```

```python
from c360.store import C360Store
store = C360Store()
doc = store.get("C-818030")            # JSON document for the card's owner
store.append_interaction(doc["_id"], "call_transcript", {...}, source="layer3.agent_assist")
# or straight from MongoDB:  db.customers.find_one({"collections.cases.case_id": "CS-2026-614370"})
```

Layer 3 apps (agent terminal and customer app) on the golden record:

```bash
pip install -r requirements-layer3.txt
python -m c360.layer3.data.build_local                 # ~20 s: Layer 3's operational store
export C360_L3_SOURCE=mongo                            # customer documents from Layer 1's MongoDB record
uvicorn c360.layer3.api:app --port 8300                # http://127.0.0.1:8300/terminal and /app
```

Feature store and promise risk model (one definition for training and live scoring; details in `docs/features/README.md`):

```bash
pip install -r requirements-features.txt
python -m c360.features build              # every promise's features (2 s) -> warehouse/features/features.duckdb
python -m c360.models.ptp_risk train       # out-of-time evaluation + production fit -> docs/features/model_card_ptp_risk.md
python -m c360.models.fairness             # measurement only -> docs/features/fairness_report.md
python -m c360.models.ptp_risk score PTP-7011743   # live: same feature SQL, probability + plain-English reasons
```

Human review of uncertain identity links:

```bash
python -m c360.layer1.review list --system cards
python -m c360.layer1.review show cards CX0606700
python -m c360.layer1.review accept cards CX0606700 --reviewer jdoe --note "same DOB and surname; moved"
python run_layer1.py --from linkage    # apply decisions
```

## Layer 1 — Data Product Factory

```
 raw files ──► BRONZE (warehouse/bronze.duckdb)  every file as delivered, all text, + lineage columns
              │
              ▼
           CURATED (schema curated)              typed per schema.json; every row and column kept;
              │                                   fixes added as *_std / *_recomputed / dq_* columns
              ▼
           IDENTITY RESOLUTION                    CRM dedup → bridges → pre-filter → score → decide
              │                                   → review queue (nothing deleted)
              ▼
           GOLDEN assembly (DuckDB staging)       one document per person, assembled from all sources
              │
              ▼
           MongoDB  collections360                customers (1 doc / person, indexed on every source key,
                                                  schema-validated), interactions (write-back),
                                                  match_review_queue / _decisions, match_llm_verdicts,
                                                  dq_results, restricted_protected_attributes
```

DuckDB is the processing engine (like Spark in a larger deployment). The served Collections 360 is
MongoDB only: the pipeline builds into `customers_build` and atomically swaps it in, so readers never
see a half-built store, and interactions written back by Layers 3–4 are re-attached on every rebuild.

| Piece | File |
|---|---|
| Raw landing | [c360/layer1/bronze.py](c360/layer1/bronze.py) |
| Standardisation macros (names, dates, phones, postal, province, CIF, buckets, comparison levels) | [c360/layer1/normalize.py](c360/layer1/normalize.py) |
| Curated layer (schema-driven typing + DQ flag columns) | [c360/layer1/curated.py](c360/layer1/curated.py) |
| Per-system identity profiles | [c360/layer1/profiles.py](c360/layer1/profiles.py) |
| CRM dedup, bridges, pre-filter, Fellegi–Sunter scoring, decisions | [c360/layer1/identity.py](c360/layer1/identity.py) |
| Final links, account crosswalk, collections linking | [c360/layer1/linkage.py](c360/layer1/linkage.py) |
| Golden C360 document assembly | [c360/layer1/golden.py](c360/layer1/golden.py) |
| Publish to MongoDB (validated, indexed, atomic swap) | [c360/layer1/load_mongo.py](c360/layer1/load_mongo.py) |
| MongoDB collections, indexes, schema validator | [c360/mongo.py](c360/mongo.py) |
| Data quality checks + report generator | [c360/layer1/dq.py](c360/layer1/dq.py) |
| Optional LLM adjudication of the review queue | [c360/layer1/llm_adjudicator.py](c360/layer1/llm_adjudicator.py) |
| Human review CLI | [c360/layer1/review.py](c360/layer1/review.py) |
| Document store API on MongoDB (read by any key, write back) | [c360/store.py](c360/store.py) |
| Data contract | [docs/data_contract_c360.yaml](docs/data_contract_c360.yaml) |
| Data quality report (generated) | [docs/data_quality_report.md](docs/data_quality_report.md) |

### Customer ID matching across the five systems

| System | Key | How it is linked |
|---|---|---|
| CRM | `crm_customer_id` (1,020,000 records) | Deduplicated into **1,000,008 people**: shared national-ID hash, or name + DOB + phone/email rules (a different national ID vetoes). 99.85% recall on CRM's own labelled duplicates. |
| Cards | `src_customer_ref` (550,000) | Probabilistic match, 99.96% auto-linked |
| Lending | borrower `src_customer_ref` (340,000) | Probabilistic match, 99.97% auto-linked |
| Core banking | 10-digit CIF (983,567; deposits + bureau) | Leading zeros restored, then probabilistic match, 99.95% auto-linked |
| Collections | `coll_customer_ref` on 367,229 cases | Exact CRM id; 10,349 orphan ids repaired from account ownership + one-digit edit; 605 typos that landed on another real customer caught and corrected. 14 left for review. |

**Match confidence.** Each shortlisted pair is compared on surname, given name, DOB, phone, postal
code and account history ("bridge"). A Fellegi–Sunter model turns the comparison into a probability.
There are no labels, so the model is **cross-trained**: name/DOB/phone/postal weights are learned
from pairs the account history confirms, bridge weights from pairs identity data confirms, and chance
agreement from random pairs. Confidence is the posterior across the whole shortlist, so a relative at
the same address lowers it instead of being silently picked.

Every link stores its evidence, e.g. `sur:3:10.1;given:3:9.1;dob:3:14.4;phone:2:17.7;postal:2:18.0;bridge:3:14.1`
= *surname exact (+10.1), given name agrees (+9.1), DOB exact (+14.4), phone exact (+17.7), postal exact (+18.0),
account history names this customer (+14.1)*.

**Independent check:** choosing the person from identity data alone (bridge removed) agrees with the
account history for 99.96% of card keys, 99.95% of loan keys and 99.99% of CIFs.

### The golden record

One MongoDB document per person in `collections360.customers` (`_id` = `C360-<master CRM id>`): `identity` (every key in every system,
with confidence), `profile`, `contact`, `servicing_constraints` (consent, cease-contact, insolvency, POA…),
`financial_profile`, `accounts` (cards / loans / deposits), `exposure`, `bureau`, `collections` (cases with
promises and offers, contact aggregates, recent notes, transcript pointers), `hardship_indicators` (flag + plain-English
reasons), `model_scores`, `data_quality_notes`, `lineage`. Sample: [outputs/layer1/sample_document_CS-2026-614370.json](outputs/layer1/sample_document_CS-2026-614370.json).

**Protected attributes** (gender, marital status, citizenship, household, newcomer, accessibility, vulnerability)
are kept out of the document entirely, in the separate `restricted_protected_attributes` collection, for
fairness testing only. A DQ check scans the stored documents and fails if one leaks back in.

### Data quality

39 checks run on every build; results go to MongoDB `dq_results` and [docs/data_quality_report.md](docs/data_quality_report.md).
Highlights: 19,992 duplicate CRM records; 328,891 snapshot rows and 10,365 cases with mistyped CRM ids; 156,837 CIFs
missing leading zeros; 127,543 contacts loaded twice; 119,208 conflicting duplicate model scores; ~8% missing DPD and
~3.7% stored buckets that disagree with DPD; month-end dates in four formats; `channel` renamed to `channel_v2`;
9,918 bureau rows with a shifted column (DFI-0812); province written six ways.

## What changed from our design, and why

| Design (idea phase) | Build | Why |
|---|---|---|
| NoSQL database, one JSON document per customer | **As designed:** MongoDB 7, one document per customer, indexed on every source key, schema-validated by the server. DuckDB is used only as the batch processing engine that cleans and matches the data before publishing. | The raw data is ~30M+ rows of CSV/Parquet; a columnar engine does the heavy joins in minutes, and MongoDB serves the record. |
| LLM scores every shortlisted pair | Probabilistic (Fellegi–Sunter) scorer for all pairs; LLM only adjudicates the gray zone (optional) | 1.9M source keys × ~12 candidates is ~22M pairs. The statistical scorer settles 99.96% in about a minute with an explanation per field, and the LLM is kept for the ~800 hard cases where its judgement adds value. Matches the design's cost argument, taken one step further. |
| Pre-filter: exact customer-ID match, then shortlist by name/phone/email/DOB | Same, plus account-history **bridges** as exact-key evidence and one-edit **ID repair** | The data has no shared customer id, but snapshot, salary and case tables pair account ids with (sometimes mistyped) CRM ids. |
| Review table for unmatched rows | MongoDB `match_review_queue` + review CLI; decisions stored in MongoDB persist across rebuilds | Adds the human override point required by the rules. |
| Rows attach to customer entries | Raw → curated → golden zones; curated keeps every row with DQ flags | Required by the build brief, and keeps benchmark metrics reconcilable with source systems. |

## AI tools used

- **Claude Code (Claude Opus 5.5)**: pair-programming the Layer 1 pipeline (profiling the data, writing the
  DuckDB SQL and Python modules, debugging), and drafting the README, data contract and report generator.
  All outputs were run against the real dataset and checked by the team.
- **Claude API (`claude-opus-5-5`)**: optional runtime component, `llm_adjudicator.py`, which gives a match
  percentage for review-queue candidates.
