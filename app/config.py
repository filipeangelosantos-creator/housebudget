"""Application configuration.

Everything lives under DATA_DIR (default ./data) so a single Docker volume
holds the database, uploaded statement files and the auto-generated secret key.
"""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("HB_DATA_DIR", BASE_DIR / "data"))
UPLOADS_DIR = DATA_DIR / "uploads"
PENDING_DIR = UPLOADS_DIR / "pending"
DB_PATH = Path(os.environ.get("HB_DB_PATH", DATA_DIR / "budget.db"))

SESSION_COOKIE = "hb_session"
SESSION_MAX_AGE = int(os.environ.get("HB_SESSION_DAYS", "30")) * 24 * 3600

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB per statement file

LOGIN_MAX_FAILURES = 10
LOGIN_LOCKOUT_SECONDS = 300


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)


def get_secret_key() -> str:
    """Signing key for session cookies: env var, or generated once and kept in DATA_DIR."""
    env = os.environ.get("HB_SECRET_KEY")
    if env:
        return env
    ensure_dirs()
    key_file = DATA_DIR / "secret.key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(32)
    key_file.write_text(key)
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return key
