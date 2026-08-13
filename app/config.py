"""Application configuration.

The database, uploaded statements and the cookie-signing key all live under
DATA_DIR. It deliberately defaults to a directory *outside* the code checkout:
your financial history should survive deleting, re-cloning or `git clean`-ing
the source. Set HB_DATA_DIR to put it anywhere else.
"""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LEGACY_DATA_DIR = BASE_DIR / "data"


def _default_data_dir() -> Path:
    # An install that already keeps data in the checkout keeps working; a fresh
    # one gets the safer location.
    if LEGACY_DATA_DIR.exists():
        return LEGACY_DATA_DIR
    return Path.home() / ".housebudget"


DATA_DIR = Path(os.environ.get("HB_DATA_DIR") or _default_data_dir())
UPLOADS_DIR = DATA_DIR / "uploads"
PENDING_DIR = UPLOADS_DIR / "pending"
DB_PATH = Path(os.environ.get("HB_DB_PATH", DATA_DIR / "budget.db"))

SESSION_COOKIE = "hb_session"
SESSION_MAX_AGE = int(os.environ.get("HB_SESSION_DAYS", "30")) * 24 * 3600

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB per statement file (PDFs are bigger)

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
        return key_file.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    key_file.write_text(key, encoding="utf-8")
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return key
