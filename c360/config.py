"""Paths and tunables shared by every layer."""
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("MAPLE_DATA", ROOT / "maple_collections_release"))
SCHEMA_JSON = DATA_ROOT / "schema" / "schema.json"

WAREHOUSE = Path(os.environ.get("C360_WAREHOUSE", ROOT / "warehouse"))
BRONZE_DB = WAREHOUSE / "bronze.duckdb"      # raw, every column as text (lossless)
C360_DB = WAREHOUSE / "c360.duckdb"          # curated + golden layers
DOCS_DIR = WAREHOUSE / "c360_documents"      # one JSON document per customer, sharded JSONL

OUT_DIR = ROOT / "outputs" / "layer1"

SNAPSHOT_DATE = "2026-09-28"
THREADS = int(os.environ.get("C360_THREADS", max(os.cpu_count() - 2, 1)))
MEMORY_LIMIT = os.environ.get("C360_MEMORY", "18GB")

# Identity resolution thresholds (match probability, 0-1).
AUTO_LINK = 0.90       # at or above: linked automatically
REVIEW_FLOOR = 0.50    # between REVIEW_FLOOR and AUTO_LINK: human review queue
AMBIGUITY_MARGIN = 0.10  # best candidate must beat the runner-up by this much to auto-link
MAX_CANDIDATES = 3     # shortlist size per source record, as in the design

# Optional LLM adjudication of the review queue (off unless a key is present).
LLM_MODEL = os.environ.get("C360_LLM_MODEL", "claude-opus-5-5")
LLM_MAX_CALLS = int(os.environ.get("C360_LLM_MAX_CALLS", "500"))


def connect(db=C360_DB, read_only=False):
    import duckdb
    WAREHOUSE.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db), read_only=read_only)
    con.execute(f"SET threads={THREADS}")
    con.execute(f"SET memory_limit='{MEMORY_LIMIT}'")
    con.execute("SET preserve_insertion_order=false")
    return con
