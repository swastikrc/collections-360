import pytest

from c360.features import store


@pytest.fixture()
def real_data():
    """The real serving database and a feature table built from it, or skip (e.g. inside the Layer 2 synthetic world)."""
    if not store.features_db().exists():
        pytest.skip("feature table not built (python -m c360.features build)")
    try:
        serving = store.serving_db()
    except FileNotFoundError:
        pytest.skip("no serving database in this environment")
    if not serving.exists() or store.meta()["serving_file"] != serving.name:
        pytest.skip("feature table was built from another serving database; rebuild it")
    return serving
