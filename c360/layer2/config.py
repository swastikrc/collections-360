"""Layer 2 settings: environment overrides and limits.

Everything is read at call time (not import time) so tests and the synthetic environment can
point Layer 2 at a different warehouse by changing environment variables.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from c360 import config as base

L2_VERSION = "layer2-1.0.0"

PKG_DIR = Path(__file__).resolve().parent
EXPECTED_SCHEMA_YAML = PKG_DIR / "schema" / "expected_schema.yaml"
VIEWS_YAML = PKG_DIR / "l2_views.yaml"
SEMANTIC_YAML = PKG_DIR / "semantic" / "semantic.yaml"
METRICS_YAML = PKG_DIR / "semantic" / "metrics.yaml"
SYSTEM_PROMPT_MD = PKG_DIR / "prompts" / "system.md"
QUESTIONS_DIR = PKG_DIR / "eval" / "questions"

MAX_QUESTION_CHARS = 2000


@dataclass(frozen=True)
class Settings:
    warehouse: Path
    l2_dir: Path            # serving databases + CURRENT pointer
    source_db: Path         # Layer 1's c360.duckdb (only publish_l2 and the doctor open it)
    data_root: Path         # dataset root; transcript JSON files live under it
    mongo_mode: str         # "real" or "mock:<customers.jsonl>"
    model: str | None
    temperature: float
    max_tool_calls: int
    max_rows: int
    sql_timeout: int
    snapshot_date: str      # fallback; the serving DB's meta.build overrides it at runtime
    audit_path: Path
    out_dir: Path


def settings() -> Settings:
    env = os.environ
    warehouse = Path(env["C360_WAREHOUSE"]) if env.get("C360_WAREHOUSE") else base.WAREHOUSE
    out_dir = Path(env["C360_L2_OUT"]) if env.get("C360_L2_OUT") else base.ROOT / "outputs" / "layer2"
    return Settings(
        warehouse=warehouse,
        l2_dir=Path(env["C360_L2_DIR"]) if env.get("C360_L2_DIR") else warehouse / "l2",
        source_db=warehouse / base.C360_DB.name,
        data_root=Path(env["MAPLE_DATA"]) if env.get("MAPLE_DATA") else base.DATA_ROOT,
        mongo_mode=env.get("C360_L2_MONGO", "real"),
        model=env.get("C360_L2_MODEL") or None,
        temperature=float(env.get("C360_L2_TEMPERATURE", "0")),
        max_tool_calls=int(env.get("C360_L2_MAX_TOOL_CALLS", "5")),
        max_rows=int(env.get("C360_L2_MAX_ROWS", "50")),
        sql_timeout=int(env.get("C360_L2_SQL_TIMEOUT", "30")),
        snapshot_date=base.SNAPSHOT_DATE,
        audit_path=Path(env["C360_L2_AUDIT"]) if env.get("C360_L2_AUDIT") else out_dir / "audit.jsonl",
        out_dir=out_dir,
    )


def api_key() -> str | None:
    """The Gemini API key. Never print, log or store the returned value."""
    return os.environ.get("GEMINI_API_KEY") or None


_YAML_CACHE: dict = {}


def load_yaml(path):
    """Load a YAML config file, cached until the file changes."""
    import yaml
    path = Path(path)
    key = (str(path), path.stat().st_mtime_ns)
    if key not in _YAML_CACHE:
        _YAML_CACHE[key] = yaml.safe_load(path.read_text()) or {}
    return _YAML_CACHE[key]


_MOCK_CACHE: dict = {}


def mongo_db(mode: str | None = None):
    """The MongoDB database holding `customers`.

    "real" -> c360.mongo.db() (pymongo). "mock:<path>" -> an in-memory mongomock database loaded
    from a JSON Lines file of customer documents (development and tests).
    """
    mode = mode or settings().mongo_mode
    if mode == "real":
        from c360 import mongo
        return mongo.db()
    if mode.startswith("mock:"):
        path = Path(mode[len("mock:"):])
        if not path.exists():
            raise FileNotFoundError(f"C360_L2_MONGO points at {path}, which does not exist. "
                                    "Run `python -m c360.layer2.synth` first.")
        key = (str(path), path.stat().st_mtime_ns)
        if key not in _MOCK_CACHE:
            import mongomock
            from bson import json_util
            db = mongomock.MongoClient()["collections360"]
            batch = []
            with open(path) as f:
                for line in f:
                    if line.strip():
                        batch.append(json_util.loads(line))
                    if len(batch) >= 1000:
                        db.customers.insert_many(batch)
                        batch = []
            if batch:
                db.customers.insert_many(batch)
            _MOCK_CACHE.clear()
            _MOCK_CACHE[key] = db
        return _MOCK_CACHE[key]
    raise ValueError(f"C360_L2_MONGO must be 'real' or 'mock:<path to customers.jsonl>', got {mode!r}")
