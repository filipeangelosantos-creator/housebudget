import os
import tempfile
from pathlib import Path

# Point the app at a throwaway data dir BEFORE any app module is imported.
_tmp = tempfile.mkdtemp(prefix="hb-test-")
os.environ["HB_DATA_DIR"] = _tmp
os.environ["HB_DB_PATH"] = str(Path(_tmp) / "test.db")

import pytest  # noqa: E402

from app import db  # noqa: E402
from app.services.seed import seed_defaults  # noqa: E402


@pytest.fixture()
def conn():
    """Fresh seeded in-memory database per test."""
    c = db.connect(":memory:")
    db.init_db(c)
    seed_defaults(c)
    yield c
    c.close()


SAMPLES = Path(__file__).resolve().parent.parent / "samples"
