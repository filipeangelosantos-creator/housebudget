"""SQLite access layer: connection helper, schema and tiny migration runner."""
import sqlite3
from datetime import datetime, timezone

from . import config

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'checking'
        CHECK (type IN ('checking','savings','credit','cash','other')),
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE category_groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'expense' CHECK (kind IN ('income','expense')),
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE categories (
    id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES category_groups(id),
    name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    excluded INTEGER NOT NULL DEFAULT 0,   -- excluded from budget/insights (transfers, CC payments)
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE budgets (
    id INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    month TEXT NOT NULL,                   -- 'YYYY-MM'
    amount_cents INTEGER NOT NULL DEFAULT 0,
    UNIQUE (category_id, month)
);

CREATE TABLE imports (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    filename TEXT NOT NULL,
    file_sha256 TEXT NOT NULL DEFAULT '',
    uploaded_by INTEGER REFERENCES users(id),
    num_added INTEGER NOT NULL DEFAULT 0,
    num_duplicate INTEGER NOT NULL DEFAULT 0,
    num_failed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    import_id INTEGER REFERENCES imports(id),
    date TEXT NOT NULL,                    -- 'YYYY-MM-DD'
    amount_cents INTEGER NOT NULL,         -- negative = money out, positive = money in
    description TEXT NOT NULL,
    normalized_desc TEXT NOT NULL,
    merchant_key TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(id),
    fitid TEXT,
    dedupe_hash TEXT NOT NULL UNIQUE,
    notes TEXT NOT NULL DEFAULT '',
    manual INTEGER NOT NULL DEFAULT 0,
    -- 1 when the app wants you to confirm the category (mixed-basket merchant)
    needs_review INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_txn_date ON transactions(date);
CREATE INDEX idx_txn_account_date ON transactions(account_id, date);
CREATE INDEX idx_txn_category ON transactions(category_id);
CREATE INDEX idx_txn_merchant ON transactions(merchant_key);

-- One receipt, several budget categories (Costco run = groceries + clothes).
-- Splits must sum exactly to the transaction amount.
CREATE TABLE transaction_splits (
    id INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    amount_cents INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_splits_txn ON transaction_splits(transaction_id);

-- Every money aggregate reads this instead of `transactions`: an unsplit
-- transaction yields one row, a split one yields a row per part.
CREATE VIEW txn_allocations AS
SELECT t.id                                        AS txn_id,
       t.account_id                                AS account_id,
       t.date                                      AS date,
       t.description                               AS description,
       t.merchant_key                              AS merchant_key,
       COALESCE(s.category_id, t.category_id)      AS category_id,
       COALESCE(s.amount_cents, t.amount_cents)    AS amount_cents,
       CASE WHEN s.id IS NULL THEN 0 ELSE 1 END    AS is_split
FROM transactions t
LEFT JOIN transaction_splits s ON s.transaction_id = t.id;

CREATE TABLE rules (
    id INTEGER PRIMARY KEY,
    pattern TEXT NOT NULL,
    match_type TEXT NOT NULL DEFAULT 'contains'
        CHECK (match_type IN ('contains','exact','regex')),
    category_id INTEGER NOT NULL REFERENCES categories(id),
    priority INTEGER NOT NULL DEFAULT 100, -- lower wins
    source TEXT NOT NULL DEFAULT 'user' CHECK (source IN ('seed','user','learned')),
    created_at TEXT NOT NULL
);

CREATE TABLE import_profiles (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    header_sig TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (account_id, header_sig)
);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Future schema changes: append (version, sql) pairs; each runs once in order.
# A freshly created database is stamped at SCHEMA_VERSION, so these only run
# for databases created by an older release.
MIGRATIONS: list[tuple[int, str]] = [
    (2, """
    ALTER TABLE transactions ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0;

    CREATE TABLE transaction_splits (
        id INTEGER PRIMARY KEY,
        transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
        category_id INTEGER NOT NULL REFERENCES categories(id),
        amount_cents INTEGER NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        sort_order INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_splits_txn ON transaction_splits(transaction_id);

    CREATE VIEW txn_allocations AS
    SELECT t.id                                     AS txn_id,
           t.account_id                             AS account_id,
           t.date                                   AS date,
           t.description                            AS description,
           t.merchant_key                           AS merchant_key,
           COALESCE(s.category_id, t.category_id)   AS category_id,
           COALESCE(s.amount_cents, t.amount_cents) AS amount_cents,
           CASE WHEN s.id IS NULL THEN 0 ELSE 1 END AS is_split
    FROM transactions t
    LEFT JOIN transaction_splits s ON s.transaction_id = t.id;
    """),
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path=None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    # check_same_thread=False: FastAPI may open the connection in a threadpool
    # worker and use it from an async route; access is sequential per request.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        version = SCHEMA_VERSION
    for target, sql in MIGRATIONS:
        if version < target:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {target}")
            conn.commit()
            version = target


def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
