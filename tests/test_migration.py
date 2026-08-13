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


def test_v1_database_gets_its_categories_attributed_on_the_way_to_v4():
    """The audit page needs to know who filed each row. A database that predates
    the column gets labelled by what the rules say today — a row a rule would
    produce is that rule's, anything else stays yours."""
    conn = db.connect(":memory:")
    conn.executescript(V1_SCHEMA)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO accounts (id, name, type, created_at) "
                 "VALUES (1, 'Checking', 'checking', 'now')")
    conn.execute("INSERT INTO category_groups (id, name, kind) VALUES (1, 'Food', 'expense')")
    conn.execute("INSERT INTO categories (id, group_id, name) VALUES (1, 1, 'Groceries')")
    conn.execute("INSERT INTO categories (id, group_id, name) VALUES (2, 1, 'Restaurants')")
    conn.execute("INSERT INTO rules (id, pattern, match_type, category_id, priority, "
                 "source, created_at) VALUES (1, 'MERCADO', 'contains', 1, 10, "
                 "'learned', 'now')")
    rows = [
        (1, "MERCADO CENTRAL", 1, "h1"),    # a rule produces exactly this
        (2, "MERCADO CENTRAL", 2, "h2"),    # same shop, filed elsewhere by hand
        (3, "PIZZA PLACE", 2, "h3"),        # no rule at all
        (4, "UNFILED SHOP", None, "h4"),    # never categorized
    ]
    for tid, desc, cat, h in rows:
        conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount_cents, description, "
            "normalized_desc, merchant_key, category_id, dedupe_hash, created_at) "
            "VALUES (?, 1, '2026-08-01', -5000, ?, ?, ?, ?, ?, 'now')",
            (tid, desc, desc, desc, cat, h))
    conn.commit()

    db.init_db(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 4
    got = {r["id"]: (r["classified_by"], r["rule_id"]) for r in
           conn.execute("SELECT id, classified_by, rule_id FROM transactions")}
    assert got[1] == ("rule", 1)
    assert got[2] == ("user", None)
    assert got[3] == ("user", None)
    assert got[4] == ("", None)          # nothing to attribute
    conn.close()


def test_v3_database_upgrades_to_v4():
    """The realistic path for an already-running install: a database built by
    the earlier migrations, not by today's schema."""
    conn = db.connect(":memory:")
    conn.executescript(V1_SCHEMA)
    for target, sql in db.MIGRATIONS:
        if target > 3:
            break
        conn.executescript(sql)
    conn.execute("PRAGMA user_version = 3")
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
    row = conn.execute("SELECT category_id, classified_by, rule_id "
                       "FROM transactions").fetchone()
    assert row["category_id"] == 1              # the category survived
    assert (row["classified_by"], row["rule_id"]) == ("user", None)
    conn.close()
