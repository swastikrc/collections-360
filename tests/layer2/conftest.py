"""Shared fixtures: one synthetic world per test session.

The environment is pointed at a temporary synthetic warehouse *before* any c360 module is
imported, so Layer 1's config and Layer 2's settings both see it.
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Live credentials are kept only for tests marked `live`; everything else runs offline.
_TMP = Path(tempfile.mkdtemp(prefix="c360_l2_test_"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
WORLD = _TMP / "warehouse_synth"
os.environ["C360_WAREHOUSE"] = str(WORLD)
os.environ["MAPLE_DATA"] = str(WORLD / "maple_data")
os.environ["C360_L2_MONGO"] = f"mock:{WORLD / 'customers.jsonl'}"
os.environ["C360_L2_AUDIT"] = str(_TMP / "audit.jsonl")
os.environ["C360_L2_OUT"] = str(_TMP / "outputs")       # reports written by tests stay out of the repo
os.environ.pop("C360_L2_TEXT_MODE", None)
os.environ.pop("C360_L2_DIR", None)
for _k in ("C360_L2_MAX_TOOL_CALLS", "C360_L2_MAX_ROWS", "C360_L2_SQL_TIMEOUT", "C360_L2_TEMPERATURE"):
    os.environ.pop(_k, None)

import pytest  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "live: calls the real Gemini API (needs GEMINI_API_KEY and C360_L2_MODEL)")


def pytest_collection_modifyitems(config, items):
    if os.environ.get("GEMINI_API_KEY") and os.environ.get("C360_L2_MODEL"):
        return
    skip = pytest.mark.skip(reason="live test: set GEMINI_API_KEY and C360_L2_MODEL")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def tmp_root():
    return _TMP


@pytest.fixture(scope="session")
def world():
    """Synthetic c360.duckdb + customers.jsonl + transcript files (generated once per session)."""
    from c360.layer2.synth.generate import generate
    return generate(out=WORLD, people=2000, seed=7, questions_out=_TMP / "synthetic.yaml", quiet=True)


@pytest.fixture(scope="session")
def serving_path(world):
    """A published serving database for the synthetic world."""
    from c360.layer2.publish import publish_l2
    return publish_l2(quiet=True)


@pytest.fixture(scope="session")
def serving(serving_path):
    from c360.layer2 import serving as s
    return s.open_current()


@pytest.fixture(scope="session")
def serving_like(world, tmp_root):
    """A second serving database built without the full-text index (keyword search mode)."""
    from c360.layer2 import serving as s
    from c360.layer2.publish import publish_l2
    return s.Serving(publish_l2(quiet=True, text_mode="like", l2_dir=tmp_root / "l2_like_mode"))


@pytest.fixture(scope="session")
def mock_db(world):
    from c360.layer2 import config
    return config.mongo_db()


@pytest.fixture(scope="session")
def truth(world):
    from c360.layer2.synth.truth import Truth
    return Truth.from_db(world["db"])


@pytest.fixture(scope="session")
def sample_case(truth):
    """An open hardship case with a linked customer (deterministic for the session's seed)."""
    return truth.sample_case("open_hardship")


@pytest.fixture()
def ctx(serving, mock_db):
    """A fresh tool context per test (trace state is per question)."""
    from c360.layer2.tools.registry import ToolContext
    return ToolContext.build(serving=serving, mongo=mock_db)
