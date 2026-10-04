# Merging Layer 3 into the repo

Unzip at the **repo root** (the folder that holds `c360/`, `run_layer1.py`, `requirements-layer2.txt`):

```bash
unzip layer3.zip -d <repo root>
```

It only adds new files and changes nothing that exists:

```
c360/layer3/            engines, API, web apps (terminal + customer app)
tests/layer3/           24 tests
docs/layer3/README.md   what it is, how to run it, accuracy, safeguards
requirements-layer3.txt fastapi, uvicorn, httpx
LAYER3_MERGE.md         this file
```

Layer 3 doesn't import any Layer 1 or Layer 2 module (the optional MongoDB write-back uses
`c360.store`, and only when `C360_L3_WRITEBACK=mongo`).

## One manual step

Add two lines to `.gitignore` so the local store (~500 MB) and demo outputs aren't committed:

```
warehouse_l3/
outputs/layer3/
```

## Run

```bash
pip install -r requirements-layer3.txt
export MAPLE_DATA=$PWD/maple_data/maple_collections_release    # the folder with data/, files/, labels/
python -m c360.layer3.data.build_local      # ~30 s, builds warehouse_l3/l3.duckdb
python -m c360.layer3.postcall --eval       # accuracy report used by the terminal's QA panel
uvicorn c360.layer3.api:app --port 8300     # http://127.0.0.1:8300  (/terminal, /app)
pytest tests/layer3 -q
```

Port 8300 is used so it doesn't clash with the Layer 2 API on 8000.

## Pointing it at the Layer 2 API

Layer 3 reads the Collections 360 customer document. By default it builds that document itself from the
CSVs. Once the Layer 2 API serves `GET /api/v1/customers/{key}` (SPEC_FRONTEND.md §4.4):

```bash
export C360_L3_SOURCE=api
export C360_API_BASE_URL=http://127.0.0.1:8000/api/v1
```

Customer documents then come from Layer 2 (with Layer 1's identity resolution); the portfolio monitor and
worklist still use the local store. This path hasn't been tested against the real API yet, so check
`/terminal` with `CS-2026-614370` after switching.
