"""An existing v1 database must upgrade in place without losing data."""
from app import db

V1_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '', password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL);
CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'checking', archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL);
CREATE TABLE category_groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'expense', sort_order INTEGER NOT NULL DEFAULT 0);
CREATE TABLE categories (id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES category_groups(id), name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0, excluded INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE budgets (id INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id), month TEXT NOT NULL,
    amount_cents INTEGER NOT NULL DEFAULT 0, UNIQUE (category_id, month));
CREATE TABLE imports (id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id), filename TEXT NOT NULL,
    file_sha256 TEXT NOT NULL DEFAULT '', uploaded_by INTEGER REFERENCES users(id),
    num_added INTEGER NOT NULL DEFAULT 0, num_duplicate INTEGER NOT NULL DEFAULT 0,
    num_failed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE transactions (id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    import_id INTEGER REFERENCES imports(id), date TEXT NOT NULL,
    amount_cents INTEGER NOT NULL, description TEXT NOT NULL,
    normalized_desc TEXT NOT NULL, merchant_key TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(id), fitid TEXT,
    dedupe_hash TEXT NOT NULL UNIQUE, notes TEXT NOT NULL DEFAULT '',
    manual INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE rules (id INTEGER PRIMARY KEY, pattern TEXT NOT NULL,
    match_type TEXT NOT NULL DEFAULT 'contains',
    category_id INTEGER NOT NULL REFERENCES categories(id),
    priority INTEGER NOT NULL DEFAULT 100, source TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL);
CREATE TABLE import_profiles (id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id), header_sig TEXT NOT NULL,
    config_json TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (account_id, header_sig));
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def test_v1_database_upgrades_to_v2():
    conn = db.connect(":memory:")
    conn.executescript(V1_SCHEMA)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO accounts (id, name, type, created_at) "
                 "VALUES (1, 'Checking', 'checking', 'now')")
    conn.execute("INSERT INTO category_groups (id, name, kind) VALUES (1, 'Food', 'expense')")
    conn.execute("INSERT INTO categories (id, group_id, name) VALUES (1, 1, 'Groceries')")
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount_cents, description, "
        "normalized_desc, merchant_key, category_id, dedupe_hash, created_at) "
        "VALUES (1, '2026-08-01', -5000, 'SHOP', 'SHOP', 'SHOP', 1, 'h1', 'now')")
    conn.commit()

    db.init_db(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    # existing data survived
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    # new column, table and view are present and usable
    assert conn.execute("SELECT needs_review FROM transactions").fetchone()[0] == 0
    conn.execute("INSERT INTO transaction_splits (transaction_id, category_id, "
                 "amount_cents) VALUES (1, 1, -5000)")
    conn.commit()
    row = conn.execute(
        "SELECT category_id, amount_cents, is_split FROM txn_allocations").fetchone()
    assert (row["category_id"], row["amount_cents"], row["is_split"]) == (1, -5000, 1)
    conn.close()


def test_init_is_idempotent():
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.init_db(conn)   # must not fail on re-run
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()
