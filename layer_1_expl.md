# Layer 1 — How the Collections 360 is built

This document explains how Layer 1 (the Data Product Factory) is implemented and how it works,
step by step, with real sample rows from the Maple Bank dataset. It follows one real customer,
**Zoe Ayers (`C360-ZOA-3450`)**, through the whole pipeline.

Run it with:

```bash
python run_layer1.py                 # full build (~10 min on one 22-core laptop)
python run_layer1.py --from identity # resume from a step
```

---

## 1. The problem Layer 1 solves

Maple Bank has five source systems and **no shared customer ID**:

| System | Table / column | Key |
|---|---|---|
| CRM | `customers.crm_customer_id` | one per CRM record (a person can have two) |
| Cards | `card_accounts.src_customer_ref` | card-system customer number (`CX9956611`) |
| Lending | `loan_accounts.src_customer_ref` | borrower number (`B36087301`) |
| Core banking | `deposit_accounts.src_customer_ref`, `external.bank_subject_ref` | 10-digit CIF (`0046768730`) |
| Collections | `collections_cases.coll_customer_ref` | CRM id copied at case open |

Each system stores names, dates of birth, phones and postal codes in its own format, and the data
has deliberate errors: duplicates, missing values, mixed date formats, mistyped IDs and schema drift.

Layer 1 turns this into **one trusted record per person**, stored in MongoDB, where every link
between systems carries a confidence score and a plain-English reason.

---

## 2. The pipeline at a glance

```
raw files ─► 1 BRONZE ─► 2 CURATED ─► 3 IDENTITY ─► 4 LINKAGE ─► 5 GOLDEN ─► 6 PUBLISH ─► 7 DQ
            (DuckDB)     (DuckDB)      (DuckDB)      (DuckDB)     (DuckDB)     (MongoDB)    (both)
```

| Step | File | What it does |
|---|---|---|
| 1 Bronze | `c360/layer1/bronze.py` | Lands every raw file in DuckDB exactly as delivered |
| 2 Curated | `c360/layer1/curated.py`, `normalize.py` | Types every column; adds fixes as new columns and flags |
| 3 Identity | `c360/layer1/profiles.py`, `identity.py` | Works out which records belong to the same person |
| 4 Linkage | `c360/layer1/linkage.py` | Final link decisions, crosswalks, review queue |
| 5 Golden | `c360/layer1/golden.py` | Assembles one document per person (staging) |
| 6 Publish | `c360/layer1/load_mongo.py`, `c360/mongo.py` | Loads the documents into MongoDB |
| 7 DQ | `c360/layer1/dq.py` | Runs 39 data quality checks and writes the report |

Supporting pieces:

| File | Purpose |
|---|---|
| `run_layer1.py` | Runs the steps in order; `--from`, `--only`, `--llm-review`, `--export-json` |
| `c360/config.py` | Paths and tunables (thresholds 0.90 / 0.50, lead 0.10, top-3 candidates shown) |
| `c360/store.py` | Read a customer by any key; write interactions back |
| `c360/layer1/review.py` | Human review CLI (accept / reject / relink) |
| `c360/layer1/llm_adjudicator.py` | Optional: an LLM judges uncertain matches |

---

## 3. Step 1 — Bronze: land the raw files (`bronze.py`)

**What it does.** Copies all 31 tables into `warehouse/bronze.duckdb` exactly as delivered. Every
CSV column is kept as **text**, and each row gets `_src_file` and `_load_ts` lineage columns.

**Why keep everything as text?** If the database guesses types on load, it destroys evidence:

- CIF `0046768730` would become the number `46768730`.
- The outcome code `NA` ("no answer") would become a missing value.
- Mixed date formats would be half-parsed silently.

**Sample — the same customer, as three systems store her:**

| System | Key | Name | DOB | Phone | Postal | Province |
|---|---|---|---|---|---|---|
| CRM | `ZOA-3450` | Zoe / Ayers | `1993-11-23` | `289-573-0430` | `L4W 4T0` | `Ont.` |
| Cards | `CX9956611` | `AYERS/ZOE` | `23-11-1993` | `2895730430` | `L4W4T0` | — |
| Bureau | `0046768730` | `AYERS, ZOE LATASHA` | `1993-11-23` | — | in address | — |

**Other problems already visible in bronze:**

| Table | Raw value | Problem |
|---|---|---|
| deposit_accounts | `src_customer_ref = 31873919` | CIF lost its two leading zeros |
| account_monthly_snapshot | `C-921480 → NAS-4380` (10 months), `NAS-4383` (1 month) | CRM ID mistyped in one month |
| collections_cases | `account_ids = L-6979417` | should be a JSON list `["L-6979417"]` |

---

## 4. Step 2 — Curated: typed and flagged, never changed (`curated.py`, `normalize.py`)

**What it does.**

- Converts every column to the type declared in `schema.json` (integer, date, boolean, list).
  Values that fail to convert become null and are counted in `curated._cast_failures`.
- **Keeps every row and every original column.** Fixes are added as new columns:
  - `*_std` — standardised values, e.g. `channel_std = coalesce(channel_v2, channel)`
  - `*_recomputed` — e.g. the delinquency bucket recalculated from days past due
  - `dq_*` — flags, e.g. `dq_duplicate_of`, `dq_shifted_row`, `dq_cif_leading_zeros_lost`

**Why flag instead of fixing in place?** The benchmark gold answers are computed on the stored
values. Keeping both the stored and the corrected value lets every metric reconcile with the source
systems, and lets Layer 2 choose which one to use.

**`normalize.py`** holds the reusable SQL functions:

| Function | Example |
|---|---|
| `norm_name` | `"St-Jean"` → `stjean`, `"Aurélie"` → `aurelie` |
| `parse_date_any` | `23-11-1993`, `19931123`, `Nov 23 1993` → `1993-11-23` |
| `phone10`, `phone_mask_key` | `+1 (418) 757 2321` → `4187572321`; masked `672993xxx02` → `67299302` |
| `postal6` | `l4w 4t0` → `L4W4T0` |
| `province_std` | `Ont.`, `Alta.`, `PQ` → `ON`, `AB`, `QC` (falls back to the postal code) |
| `cif10` | `31873919` → `0031873919` |
| `dpd_bucket_from_dpd` | `33` → `31-60` |
| `cmp_surname`, `cmp_dob`, … | comparison levels used in Step 3 |

**Samples:**

`curated.card_accounts`

| card_account_id | dpd | dpd_bucket (stored) | dpd_bucket_recomputed | dq_bucket_mismatch |
|---|---|---|---|---|
| C-818030 (Zoe) | 33 | 31-60 | 31-60 | false |
| C-105897 | 0 | **1-30** | **current** | **true** |

`curated.customers`

| crm_customer_id | province_code | province_code_std | postal_code_std | dq_province_nonstandard |
|---|---|---|---|---|
| CHF-6185 | `Alta.` | `AB` | `T3H0T6` | true |

`curated.deposit_accounts`

| deposit_account_id | src_customer_ref | cif | dq_cif_leading_zeros_lost |
|---|---|---|---|
| CHQ-487877 | `63940717` | `0063940717` | true |

`curated.contact_history` — the same call loaded twice

| contact_id | case_id | contact_ts_utc | channel_std | dq_duplicate_of |
|---|---|---|---|---|
| CT-1588136 | CS-2026-442203 | 2026-07-28 01:12:24 | call | — |
| CT-1631514 | CS-2026-442203 | 2026-07-28 01:12:24 | call | **CT-1588136** |

---

## 5. Step 3 — Identity: who is who (`profiles.py`, `identity.py`)

This is the core of Layer 1. It has five parts.

### 5.1 Profiles — one standard identity row per key (`profiles.py`)

Each system stores names differently, so each gets its own parser:

| System | Stored as | Parsed as |
|---|---|---|
| Cards | `L. GARCIA`, `HUANG/JASON`, `KYLE MANN` | initial + surname; surname/given; given surname |
| Lending | `Rachael Dennis`, `ST-LAURENT PHILIPPE` | given surname; surname first when in capitals |
| Deposits | `WANG T J` | surname + initials |
| Bureau | `YANG, WAYNE JACK` | surname, given middle |

Core banking combines deposits and bureau data under one CIF. Every profile ends up with the same
columns:

| sys | src_ref | sur | given | dob | phone key | postal |
|---|---|---|---|---|---|---|
| crm | ZOA-3450 | ayers | zoe | 1993-11-23 | 28957330 | L4W4T0 |
| cards | CX9956611 | ayers | zoe | 1993-11-23 | 28957330 | L4W4T0 |
| core | 0046768730 | ayers | zoe | 1993-11-23 | — | L4W4T0 |

The phone key is the first 6 plus the last 2 digits, so masked card phones (`672993xxx02`) can
still be compared. Checked against the account history, masked phones agree with the CRM phone
99.998% of the time.

### 5.2 CRM dedup — two CRM records, one person

1. Find pairs of CRM records that might be the same person: same national-ID hash, same DOB and
   initials, same email, or same phone.
2. Decide with conservative rules (e.g. same national ID + same DOB + similar surname → 0.995).
   A **different national ID always blocks a merge**, because twins and spouses share surname,
   address and phone.
3. Group linked records with union-find into people.
4. Pick a **master record** per person: not soft-deleted, most complete, oldest.
   The person's ID is `C360-<master CRM id>`.

`golden.crm_person_map`

| crm_customer_id | c360_id | is_master | group_size | match_confidence |
|---|---|---|---|---|
| DMP-7945 | C360-DMP-7945 | true | 2 | 1.000 |
| DMP-6805 | C360-DMP-7945 | false | 2 | 0.995 |

**Result:** 1,020,000 CRM records → **1,000,008 people**. CRM's own (incomplete) duplicate pointer
labels 5,950 pairs; 5,941 of them are merged (99.85% recall).

### 5.3 Bridges — exact-key evidence, with ID repair

Three tables pair account IDs with CRM IDs: `account_monthly_snapshot`, `salary_credit_history` and
`collections_cases`. That gives exact-key evidence linking an account (and so its card / borrower /
CIF key) to a CRM record. About 2.6% of those CRM IDs are mistyped (one digit changed). They are
repaired:

1. by the account's own majority vote among valid IDs one edit away, or
2. by the only real CRM ID one digit away.

`golden._bridge`

| account_id | votes | crm_raw | crm_customer_id | id_status |
|---|---|---|---|---|
| C-921480 | 10 | NAS-4380 | NAS-4380 | valid |
| C-921480 | 1 | NAS-4383 | NAS-4380 | repaired_same_account |

### 5.4 Pre-filter — how the shortlist is formed

Comparing every card key with every CRM record would be 550,000 × 1,020,000 ≈ **560 billion pairs**.
Instead, a CRM record becomes a candidate only if it passes **at least one** of six cheap rules.
Each is a simple equality join in SQL. **No weights are used here** — only yes/no rules.

| # | Rule | Catches |
|---|---|---|
| 1 | **Bridge:** the account history names this CRM ID | the exact-key route |
| 2 | **Same DOB**, and a first letter of the name matches (either name order) | normal matches, even if phone or postal changed |
| 3 | **DOB with day and month swapped**, and the surname initial matches | `03/11` keyed as `11/03` |
| 4 | **Same phone** (first 6 + last 2 digits) | typos in name or DOB |
| 5 | **Same postal code**, and a first letter matches | DOB errors for people who haven't moved |
| 6 | **Same surname and birth year**, and the given-name initial matches | DOB typos for people who **have** moved |

A record passing several rules appears once (SQL `UNION`). The rules overlap on purpose: a true
match would have to fail all six to be missed.

**Result for cards:** 6.6M pairs for 550,000 keys — about **12 candidates per key**. All of them are
scored. (The "top 3" in `config.py` is only how many are shown in the review queue.)

Zoe's card (`CX9956611`) shortlist:

| Candidate | Who | Got in by |
|---|---|---|
| ZOA-3450 | Zoe Ayers, the real match | rules 1, 2, 4, 5 and 6 |
| ZOA-7235 | another Zoe Ayers, different DOB | rule 6 |
| ANG-2634 | someone with the same DOB | rule 2 |

### 5.5 Scoring — how the weights are calculated

Each shortlisted pair is compared on six fields. Each field gets a **level**:
3 = exact, 2 = near, 1 = partial, 0 = different, −1 = missing.

| Field | Levels |
|---|---|
| Surname | exact / typo / similar / different |
| Given name | agrees / near-identical / first initial agrees / different |
| DOB | exact / day-month swapped / one digit off / different |
| Phone | exact / agrees on visible digits / different |
| Postal | exact / same area (FSA) / different |
| Bridge | names this CRM ID / names it after repair / names a related record / names someone else |

Each level's weight (Fellegi–Sunter) is:

```
weight = log2( m / u )
m = how often TRUE matches show this level
u = how often UNRELATED people show this level by chance
```

Missing (−1) counts as 0, so it neither helps nor hurts.

**Where u comes from — random pairs.** The code pairs 2M random source keys with 2M random CRM
records and counts how often each level happens by chance. For example, strangers share an exact
DOB in about 0.006% of pairs (u ≈ 0.0000585).

**Where m comes from — cross-training, because there are no labels.**

- **Name, DOB, phone, postal:** m is measured on pairs the **bridge confirms**. The bridge comes
  from a different system than the name and DOB on the card, so these pairs are an unbiased sample
  of true matches.
- **Bridge:** m is measured on pairs whose name/DOB/phone/postal alone score ≥ +10; u on pairs
  scoring ≤ −10.
- **Smoothing:** each level count gets +1, so no level is ever 0.
- **Monotone rule:** a closer level never gets less weight than a looser one.

Learned per source system (cards, loans, core). Example numbers from the cards model:

| Field, level | m (true matches) | u (strangers) | weight |
|---|---|---|---|
| Surname exact | 0.986 | 0.00092 | **+10.1** |
| DOB exact | 0.996 | 0.0000585 | **+14.1** |
| Given name different | ≈0.00002 | 0.94 | **−18.8** |
| Postal exact | 0.99 | ≈0.000002 | **+18 to +19** |

Exact values move by a few tenths between builds, because u comes from a fresh random sample.

**Adding it up.** The score starts from a prior for "one specific person out of 1,000,008":
log2(1/1,000,008) ≈ **−19.9**. Then every field's weight is added:

| | Zoe ↔ ZOA-3450 | Zoe ↔ ZOA-7235 |
|---|---|---|
| Prior | −19.9 | −19.9 |
| Surname | exact +10.1 | exact +10.1 |
| Given name | exact +9.2 | exact +9.2 |
| DOB | exact +14.7 | **different −9.8** |
| Phone | exact +21.9 | **different −7.2** |
| Postal | exact +18.0 | **different −6.9** |
| Bridge | names her +14.1 | **names someone else −6.1** |
| **Total** | **+68.2** | **−30.5** |

`golden._scored_cards`

| src_ref | candidate | sur | given | dob | phone | postal | bridge | weight |
|---|---|---|---|---|---|---|---|---|
| CX9956611 | **ZOA-3450** | 3 | 3 | 3 | 2 | 2 | 3 | **+68.2** |
| CX9956611 | ZOA-7235 | 3 | 3 | 0 | 0 | 0 | 0 | −30.5 |
| CX9956611 | ANG-2634 | 0 | 0 | 3 | 0 | 0 | 0 | −50.5 |

Every link stores its evidence, e.g.
`sur:3:10.1;given:3:9.2;dob:3:14.7;phone:2:21.9;postal:2:18.0;bridge:3:14.1`, which reads as
*surname exact (+10.1), given name agrees (+9.2), DOB exact (+14.7), phone exact (+21.9), postal
exact (+18.0), account history names this customer (+14.1)*.

### 5.6 Decision — from weights to confidence

Candidates are first grouped by person (two CRM records of the same person are not competitors).
Then each person's score becomes a probability across the whole shortlist:

```
confidence(person i) = 2^w_i / (1 + Σ 2^w_j)    over all candidates j
```

The "1" stands for "the right person is not in the shortlist" (weight 0, and 2^0 = 1). This way a
strong runner-up — e.g. a relative at the same address — lowers the confidence instead of being
silently ignored.

**Decision rules** (from `config.py`):

```
confidence ≥ 0.90 and lead over runner-up ≥ 0.10  → auto_link
confidence ≥ 0.50                                 → review     (best candidate attached provisionally)
otherwise                                         → unmatched  (no candidate attached; in the review queue)
```

**Zoe:** +68.2 against tiny alternatives → confidence 1.000 → auto-link.

#### Worked example: two candidates both scoring +20

```
2^w_A = 2^20 = 1,048,576
2^w_B = 2^20 = 1,048,576

denominator   = 1 + 1,048,576 + 1,048,576 = 2,097,153

confidence(A) = 1,048,576 / 2,097,153 = 0.4999998
confidence(B) = 1,048,576 / 2,097,153 = 0.4999998
P(neither)    =         1 / 2,097,153 = 0.0000005
                                  sum = 1.0000000
```

The code first subtracts the largest score (mx = 20) so 2^w never overflows; the result is the same:

```
confidence(A) = 2^(20−20) / (2^(−20) + 2^(20−20) + 2^(20−20)) = 1 / 2.00000095 = 0.4999998
```

| Rule | Required | A has | Passes? |
|---|---|---|---|
| Auto-link threshold | ≥ 0.90 | 0.4999998 | ✗ |
| Lead over runner-up | ≥ 0.10 | 0 | ✗ |

Not auto-linked. Because 0.4999998 is just below the 0.50 review floor, the current code labels an
**exact tie** `unmatched`: it still goes to the review queue with both candidates, but no candidate
is attached provisionally, and the queue reason reads "best candidate below review threshold"
rather than "two plausible people". (Planned fix: always treat a runner-up ≥ 0.10 as `review` with
the reason "two plausible people".)

#### Worked example: a near-tie, +21 vs +20

```
denominator   = 1 + 2^21 + 2^20 = 1 + 2,097,152 + 1,048,576 = 3,145,729
confidence(A) = 2,097,152 / 3,145,729 = 0.667
confidence(B) = 1,048,576 / 3,145,729 = 0.333
lead          = 0.334
```

Passes the lead rule but fails 0.90 → `review`: A is attached provisionally and the queue reason
reads "two plausible people".

### 5.7 How good is the matching?

| Source system | Keys | Auto-linked |
|---|---|---|
| Cards | 550,000 | 99.96% |
| Lending | 340,000 | 99.97% |
| Core banking (CIF) | 983,567 | 99.95% |

**Independent check.** For keys that have account history, the person chosen from identity data
alone (bridge removed) is compared with the person the account history names. They agree for
99.96% of card keys, 99.95% of loan keys and 99.99% of CIFs.

---

## 6. Step 4 — Linkage: final decisions and crosswalks (`linkage.py`)

**Precedence for every source key:**

1. A person's review decision (from MongoDB `match_review_decisions`) always wins.
2. Auto-link — confident, clearly ahead, and **not contradicted by the account history**.
3. LLM verdict ≥ 90% (only if the optional LLM step was run).
4. Provisional link to the best candidate, flagged for review.
5. Unmatched — kept in the review queue with its candidates, never deleted.

If identity data and account history point to different people, the key goes to review even if its
score is high.

`golden.source_customer_xref` — every card, borrower and CIF key → person

| src_system | src_ref | c360_id | link_status | confidence | evidence |
|---|---|---|---|---|---|
| cards | CX9956611 | C360-ZOA-3450 | auto_link | 1.000 | `sur:3:10.1;…;bridge:3:14.1` |
| core | 0046768730 | C360-ZOA-3450 | auto_link | 1.000 | `sur:3;given:3;dob:3;postal:2` |
| cards | CX0606700 | C360-STS-1579 | **provisional** | 0.686 | postal disagrees, only an initial |

`golden.account_xref` — every account → person and role (primary, joint, co_borrower)

| account_id | product | role | c360_id | link_status |
|---|---|---|---|---|
| C-818030 | TRV-INFINITE | primary | C360-ZOA-3450 | auto_link |

`golden.case_xref` — every collections case → person

| Situation | Link method | Cases |
|---|---|---|
| CRM ID valid and consistent with the case's accounts | crm_id_exact | 356,259 |
| CRM ID doesn't exist; repaired from the accounts' owner + one-digit edit | repaired_account_owner_and_id_edit | 10,349 |
| CRM ID is real but the wrong person (typo landed on another customer) | repaired_id_collision | 605 |
| Repaired from a one-digit edit only | repaired_id_edit_only | 2 |
| Could not link | unmatched | 14 |

Example repair:

| case_id | coll_customer_ref | c360_id | link_method | confidence |
|---|---|---|---|---|
| CS-2025-919039 | ADD-4679 (doesn't exist) | C360-ADD-6679 | repaired_account_owner_and_id_edit | 0.999 |

**Review queue** (MongoDB `match_review_queue`) — about 800 items, each with a reason and its top
candidates:

| src_system | src_ref | review_reason | proposed |
|---|---|---|---|
| cards | CX1839953 | identity data and account history point to different people | C360-PAP-8012 |

People review with:

```bash
python -m c360.layer1.review list --system cards
python -m c360.layer1.review show cards CX0606700
python -m c360.layer1.review accept cards CX0606700 --reviewer jdoe --note "same DOB and surname; moved"
python run_layer1.py --from linkage    # apply decisions
```

---

## 7. Step 5 — Golden: assemble one document per person (`golden.py`)

About 16 queries each build one section, and they are joined into one nested row per person in the
DuckDB staging table `golden.c360_customer`:

| Section | Contents |
|---|---|
| `identity` | every key in every system, with link status, confidence and evidence |
| `profile` | name, DOB, age, tenure, segment |
| `contact` | phone, email, address, province, preferred and permitted contact windows |
| `servicing_constraints` | consent, cease-contact, bankruptcy, consumer proposal, POA, third party… |
| `financial_profile` | employment, income, housing cost, salary trend |
| `accounts` | cards, loans, deposits — with recomputed buckets and per-account DQ flags |
| `exposure` | balances, past due, max DPD, worst bucket |
| `bureau` | latest pull + last 6 scores |
| `collections` | cases (with promises and offers), contact aggregates, recent notes, transcript pointers |
| `hardship_indicators` | flag + plain-English reasons |
| `model_scores`, `data_quality_notes`, `lineage` | |

**Survivorship rules** (when a person has two CRM records):

- identity details come from the **master** record;
- contact details come from the **most recently updated** record;
- consent and cease-contact take the **strictest** value across all records.

**Protected attributes** (gender, marital status, citizenship, household, newcomer, accessibility,
vulnerability) go to a separate table and **never** enter the document.

This table is **staging only**. It is deleted at the end of Step 6.

---

## 8. Step 6 — Publish to MongoDB (`load_mongo.py`, `mongo.py`)

1. 12 worker processes stream the documents into a fresh collection `customers_build`
   (dates converted to BSON).
2. The server checks each document against the contract's `$jsonSchema` validator.
3. Interactions written back by Layers 3–4 are re-attached.
4. 17 indexes are built (every source key + common filters).
5. `customers_build` is **atomically renamed** to `customers` — readers never see a half-built store.
6. The review queue and the restricted protected-attributes collection are loaded; the DuckDB
   staging copies are dropped.

### What `collections360` is

`collections360` is the **MongoDB database**. The Collections 360 record itself is the
**`customers` collection** inside it, written in Step 6.

| | Step 5 (golden) | Step 6 (publish) |
|---|---|---|
| Where | DuckDB | MongoDB |
| Name | `golden.c360_customer` | `collections360.customers` |
| What it is | temporary staging table | the real Collections 360 |
| Lifetime | deleted at the end of Step 6 | permanent; Layers 2–4 read and write it |

| Collection | Contents |
|---|---|
| **`customers`** | **the Collections 360 record:** 1,000,008 documents |
| `interactions` | transcripts and notes written back by Layers 3–4 |
| `match_review_queue` / `match_review_decisions` | uncertain matches and people's decisions |
| `match_llm_verdicts` | optional LLM match judgements |
| `dq_results` | data quality check results |
| `restricted_protected_attributes` | protected fields, fairness testing only |

### The final record (abridged)

```json
{
  "_id": "C360-ZOA-3450",
  "identity":  { "crm_ids": ["ZOA-3450"], "card_customer_refs": ["CX9956611"], "cifs": ["0046768730"],
                 "source_keys": [{ "src_system": "cards", "link_status": "auto_link", "match_confidence": 1.0,
                                   "evidence": "sur:3:10.1;given:3:9.2;dob:3:14.7;..." }] },
  "profile":   { "first_name": "Zoe", "last_name": "Ayers", "date_of_birth": "1993-11-23", "age": 32 },
  "contact":   { "province_code": "ON", "postal_code": "L4W4T0", "permitted_call_window_local": ["07:00","21:00"] },
  "servicing_constraints": { "consent_call": true, "consent_sms": false, "cease_communication": false },
  "accounts":  { "cards": [{ "account_id": "C-818030", "dpd": 33, "dpd_bucket": "31-60", "past_due_amount": 164.77,
                             "hardship_program_type": "fixed_payment_plan" }], "loans": [], "deposits": [] },
  "bureau":    { "risk_score": 601, "risk_score_delta_90d": -60 },
  "collections": { "cases": [{ "case_id": "CS-2026-614370", "queue": "hardship_specialist", "hardship_reason": "illness",
                   "promises": { "kept": 1, "broken": 1, "latest": { "amount": 95.0, "due_date": "2026-10-14" } } }],
                   "recent_notes": ["off work sick since Jul. promised 95 by the 14th ..."] },
  "hardship_indicators": { "financial_hardship_flag": true,
                           "reasons": ["open collections case is coded as hardship", "an account is on a hardship programme"] },
  "interactions": [],
  "data_quality_notes": []
}
```

The `ON` province, the recomputed bucket and the merged identity are all Step 2–4 fixes, now in one
place.

### Reading and writing the record

```bash
python -m c360.store CS-2026-614370      # any key: case, account, CRM id, card ref, borrower ref, CIF
```

```python
from c360.store import C360Store
store = C360Store()
doc = store.get("C-818030")
store.append_interaction(doc["_id"], "call_transcript", {...}, source="layer3.agent_assist")
```

Lookups by any key take 1–12 ms using the indexes. `append_interaction` writes to `interactions`
(survives rebuilds) and pushes the item onto the customer's document.

---

## 9. Step 7 — Data quality (`dq.py`)

**DQ means data quality:** checking whether the data is correct, complete and consistent. Step 7
doesn't build or change the Collections 360 — it **measures and proves** how good the data is.

### Why it's needed

1. **The hackathon requires it** — data quality checks in Layer 1, and a data quality report as a
   submission. Step 7 generates the report automatically.
2. **Steps 2–4 fix problems quietly; Step 7 makes them visible** — how many problems existed and
   whether the fixes worked.
3. **Safety net on every rebuild** — if a new extract arrives broken (CIFs losing zeros again, a
   renamed column, protected attributes leaking into documents), the counts jump and it's caught.
4. **It checks the data contract's promises** — e.g. DQ-GOV-01 scans MongoDB documents and must
   find 0 protected fields.
5. **Trust** — agents and judges can see what was wrong, how much, and what was done.

### What one check looks like

| Question | Example (DQ-ID-10) |
|---|---|
| What's the problem? | CIF lost its leading zeros |
| Where? | `deposit_accounts` |
| How many records? | 156,837 of 780,000 (20.1%) |
| How was it handled? | padded back to 10 digits so deposits link to bureau data |

39 checks in 7 categories:

| Category | Example problems |
|---|---|
| Identity | duplicate CRM records, mistyped customer IDs, orphan cases |
| Format | dates in four formats, provinces written six ways, masked phones |
| Schema drift | `channel` renamed to `channel_v2` |
| Values | missing DPD, buckets that disagree with DPD, the shifted bureau column |
| Duplicates | contacts loaded twice, conflicting model scores |
| Governance | protected attributes inside customer documents (must be 0) |
| Operational | dispute spike after the app outage, call drop during the dialer outage |

**Outputs:** `dq_results` in MongoDB (one row per check per build, so trends are visible) and
`docs/data_quality_report.md`, regenerated every build — no number in it is typed by hand.

Sample `dq_results`:

| check_id | table | records_affected | of | % |
|---|---|---|---|---|
| DQ-DUP-01 | contact_history | 127,543 | 2,688,337 | 4.74 |
| DQ-FMT-02 | account_monthly_snapshot | 1,255,152 | 12,569,692 | 9.99 |
| DQ-GOV-01 | mongo customers | 0 | 304 fields | 0.0 |

---

## 10. Why SQL, DuckDB and MongoDB — and not the others

### SQL as the language (Steps 1–5)

- The core of Layer 1 is joins, group-bys and window functions over tens of millions of rows.
  SQL expresses that compactly — the whole matcher is a few hundred lines.
- It is auditable: a reviewer or judge can read exactly how a bucket was recalculated or a match
  scored.
- Cleaning rules are written once as SQL functions (`normalize.py`) and reused everywhere.
- Python handles what SQL is weak at: union-find for dedup, the small model-fitting step, and
  orchestration.

### DuckDB for processing (Steps 1–5)

| Alternative | Why not |
|---|---|
| pandas | The brief warns against it; 12M+ row tables don't fit comfortably in memory; single core. |
| PostgreSQL | Row-oriented, so slow for full-table analytics; needs a server and slow bulk loads. Built for transactions, not batch transformation. |
| Spark | Right at 100× this size, but a JVM cluster to manage with slow start-up for no gain here. DuckDB plays the same role on one machine. |
| MongoDB for processing | `$lookup` joins over ~22M candidate pairs with fuzzy matching would be far slower and much harder to read. |
| Polars | Also suggested by the brief and fast. DuckDB won because it reads CSV and Parquet directly, has fuzzy matching built in (`jaro_winkler_similarity`, `levenshtein`, `damerau_levenshtein`), and keeps the logic in readable SQL. |

The full raw-to-matched pipeline runs in about 7 minutes on one laptop, with no server.

### MongoDB for serving the record (Step 6 onwards)

| Need | Why MongoDB fits |
|---|---|
| One record per customer, as in our design | A document *is* the customer: nested accounts, cases, promises and notes, no joins at read time. |
| Customers differ in shape (0–5 cards, 0–10 cases) | Documents don't need fixed columns or null-filled tables. |
| Find a customer by any key from any system | Multikey indexes on array fields; lookups in 1–12 ms. |
| Layers 3–4 write transcripts back | `$push` onto the document is a single atomic write. |
| Enforce the data contract's shape | `$jsonSchema` validator rejects malformed documents. |
| Layer 2 plain-English questions | Its find and aggregate query language works well for an LLM to generate, and can be shown with each answer. |

| Alternative | Why not |
|---|---|
| PostgreSQL JSONB | A relational database with JSON added on; nested-array indexing and validation are clumsier; not the NoSQL store in our design. |
| Cassandra / DynamoDB | Built for huge write volumes with queries fixed in advance; poor for "find by any of nine keys" and ad-hoc questions. DynamoDB is cloud-only. |
| Redis | In-memory key-value store: 2.6 GB of documents in RAM, no rich queries, weaker durability. |
| Elasticsearch | Great for text search, but not a system of record. Could be added later for searching notes. |
| Couchbase / CouchDB | Viable, but smaller ecosystems; MongoDB's drivers and aggregation framework suit Layer 2 better. |

**In short:** DuckDB with SQL is the factory that cleans and matches the data in minutes, and
MongoDB is the store that holds one record per customer for the other layers.

---

## 11. Changes from the idea-phase design

| Design | Build | Why |
|---|---|---|
| NoSQL database, one JSON document per customer | **As designed:** MongoDB 7, one document per customer, indexed on every key, schema-validated. DuckDB is only the batch engine before publishing. | Columnar engine for the heavy joins; MongoDB serves the record. |
| LLM scores every shortlisted pair | Fellegi–Sunter scorer for all pairs; LLM only for the uncertain ones (optional) | ~22M pairs: the scorer settles 99.96% in about a minute with a reason per field; the LLM is kept for the ~800 hard cases. |
| Pre-filter: exact ID match, then shortlist by name/phone/email/DOB | Same, plus account-history bridges and one-digit ID repair | The data pairs account IDs with (sometimes mistyped) CRM IDs. |
| Review table for unmatched rows | MongoDB review queue + review CLI; decisions persist across rebuilds | The human override point required by the rules. |
| Rows attach to customer entries | Raw → curated → golden zones; curated keeps every row with DQ flags | Required by the brief; keeps benchmark metrics reconcilable. |
