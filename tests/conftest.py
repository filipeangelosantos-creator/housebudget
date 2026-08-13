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


@pytest.fixture()
def web(tmp_path, monkeypatch):
    """A signed-out TestClient with a database of its own.

    Web tests that create users would otherwise leak into other modules — the
    first-run setup flow can only be exercised against an empty database.
    """
    from fastapi.testclient import TestClient

    from app import config as cfg
    from app.main import app

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(cfg, "PENDING_DIR", tmp_path / "uploads" / "pending")
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "budget.db")
    with TestClient(app) as client:      # startup creates and seeds it
        yield client


SAMPLES = Path(__file__).resolve().parent.parent / "samples"
