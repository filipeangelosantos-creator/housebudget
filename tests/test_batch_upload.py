"""Queuing many statements in one upload."""
import re

from app import config, db
from app.services import importer
from tests.conftest import SAMPLES


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_a_large_batch_keeps_every_upload(tmp_path, monkeypatch):
    """Regression: tidying up after each stashed file deleted earlier files of
    the same batch, which surfaced as "Upload expired" partway through."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "PENDING_DIR", tmp_path / "uploads" / "pending")
    config.ensure_dirs()

    files = [(f"HSBC-{i:02d}.pdf", b"%PDF-1.4 statement " + str(i).encode())
             for i in range(30)]
    batch = importer.stash_batch(files, account_id=1)
    state = importer.load_batch(batch)

    assert len(state["tokens"]) == 30
    for i, token in enumerate(state["tokens"]):
        pending = importer.load_pending(token)
        assert pending is not None, f"upload {i} was tidied away"
        assert pending[0] == files[i][0]


def test_stale_uploads_are_cleared_but_queued_ones_are_not(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "PENDING_DIR", tmp_path / "uploads" / "pending")
    config.ensure_dirs()

    import os

    stale = importer.stash_pending("old.csv", b"x", 1)
    old = 1_600_000_000
    for suffix in (".bin", ".json"):                  # only this one is aged
        os.utime(config.PENDING_DIR / f"{stale}{suffix}", (old, old))
    batch = importer.stash_batch([("a.pdf", b"a"), ("b.pdf", b"b")], 1)

    importer._cleanup_pending()
    assert importer.load_pending(stale) is None       # abandoned, cleared
    state = importer.load_batch(batch)
    assert state is not None                          # queued batch untouched
    for token in state["tokens"]:
        assert importer.load_pending(token) is not None


def test_an_abandoned_batch_is_dropped_whole(tmp_path, monkeypatch):
    """Otherwise its uploads sit forever, protected by a batch nobody will
    come back to."""
    import os

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "PENDING_DIR", tmp_path / "uploads" / "pending")
    config.ensure_dirs()

    batch = importer.stash_batch([("a.pdf", b"a"), ("b.pdf", b"b")], 1)
    tokens = importer.load_batch(batch)["tokens"]
    old = 1_600_000_000
    for path in config.PENDING_DIR.glob("*"):
        os.utime(path, (old, old))

    importer._cleanup_pending()
    assert importer.load_batch(batch) is None
    assert all(importer.load_pending(t) is None for t in tokens)
    assert list(config.PENDING_DIR.glob("*")) == []    # nothing orphaned


def test_batch_token_cannot_collide_with_an_upload_token(tmp_path, monkeypatch):
    """Tokens are hex, so one starting with 'b' would have collided with the
    old b<token>.json batch naming."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "PENDING_DIR", tmp_path / "uploads" / "pending")
    config.ensure_dirs()
    batch = importer.stash_batch([("a.pdf", b"a")], 1)
    names = [p.name for p in config.PENDING_DIR.glob("*")]
    assert any(n.startswith("batch-") for n in names)
    assert importer.load_batch(batch) is not None


def test_expired_upload_shows_a_page_not_a_raw_error(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    r = web.get("/import")
    with open(SAMPLES / "sample-checking.csv", "rb") as f:
        r = web.post("/import/upload",
                     data={"csrf": csrf_of(r.text), "account_id": "new",
                           "new_account_name": "Bank", "new_account_type": "checking"},
                     files={"files": ("s.csv", f, "text/csv")})
    token = re.search(r'name="token" value="([a-f0-9]+)"', r.text).group(1)
    importer.drop_pending(token)                      # simulate an expiry

    r = web.post("/import/commit", data={
        "csrf": csrf_of(r.text), "token": token, "action": "confirm",
        "header_row": "0", "date_col": "0", "desc_col1": "1",
        "amount_mode": "single", "amount_col": "2"})
    assert r.status_code == 200
    assert "no longer held" in r.text
    assert "Upload again" in r.text
    assert "detail" not in r.text[:200]               # not the raw JSON error


def test_one_lost_file_does_not_lose_the_rest_of_the_batch(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    r = web.get("/import")
    with open(SAMPLES / "sample-checking.csv", "rb") as f1, \
            open(SAMPLES / "sample-visa.csv", "rb") as f2:
        r = web.post("/import/upload",
                     data={"csrf": csrf_of(r.text), "account_id": "new",
                           "new_account_name": "Bank", "new_account_type": "checking"},
                     files=[("files", ("a.csv", f1, "text/csv")),
                            ("files", ("b.csv", f2, "text/csv"))])
    assert "File 1 of 2" in r.text
    batch = re.search(r'name="batch" value="([a-f0-9]+)"', r.text).group(1)
    token = re.search(r'name="token" value="([a-f0-9]+)"', r.text).group(1)
    importer.drop_pending(token)                      # first file vanishes

    r = web.post("/import/commit", data={
        "csrf": csrf_of(r.text), "token": token, "batch": batch,
        "action": "confirm", "header_row": "0", "date_col": "0",
        "desc_col1": "1", "amount_mode": "single", "amount_col": "2"})
    # it moves on to file 2 rather than abandoning the batch
    assert "File 2 of 2" in r.text
