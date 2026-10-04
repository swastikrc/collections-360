"""Layer 3 settings, all read from the environment at call time."""
import datetime as dt
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# The dataset's snapshot date. "Today" for every business rule in Layer 3.
SNAPSHOT_DATE = dt.date(2026, 9, 28)


def maple_data() -> Path:
    """Root of the Maple release (holds data/, files/, labels/)."""
    env = os.environ.get("MAPLE_DATA")
    if env:
        return Path(env)
    for candidate in (ROOT / "maple_collections_release", ROOT / "maple_data" / "maple_collections_release"):
        if (candidate / "data").exists():          # this repo keeps the release at the root (as Layer 1 does)
            return candidate
    return ROOT / "maple_data" / "maple_collections_release"


def l3_dir() -> Path:
    return Path(os.environ.get("C360_L3_DIR", ROOT / "warehouse_l3"))


def l3_db() -> Path:
    return l3_dir() / "l3.duckdb"


def out_dir() -> Path:
    p = Path(os.environ.get("C360_L3_OUT", ROOT / "outputs" / "layer3"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def actions_db() -> Path:
    return out_dir() / "actions.sqlite"


def source() -> str:
    """`mongo` (Layer 1's Collections 360 in MongoDB, enriched from the local store), `local` (DuckDB over the
    Maple dataset) or `api` (a Layer 2 HTTP API)."""
    return os.environ.get("C360_L3_SOURCE", "local").lower()


def api_base_url() -> str:
    return os.environ.get("C360_API_BASE_URL", "http://127.0.0.1:8000/api/v1").rstrip("/")
