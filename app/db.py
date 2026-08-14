"""SQLite access layer: connection helper, schema and tiny migration runner."""
import sqlite3
from datetime import datetime, timezone

from . import config

SCHEMA_VERSION = 8

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
    -- How the category was decided: '' none yet, 'rule' a rule matched,
    -- 'guess' the app copied what you did with this merchant before,
    -- 'user' you chose it. Lets you audit only what the app decided.
    classified_by TEXT NOT NULL DEFAULT '',
    -- SET NULL, not cascade: deleting a rule must never delete your
    -- transactions. The row keeps its category and shows as needing review.
    rule_id INTEGER REFERENCES rules(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_txn_date ON transactions(date);
CREATE INDEX idx_txn_account_date ON transactions(account_id, date);
CREATE INDEX idx_txn_category ON transactions(category_id);
CREATE INDEX idx_txn_merchant ON transactions(merchant_key);
CREATE INDEX idx_txn_classified ON transactions(classified_by);
CREATE INDEX idx_txn_rule ON transactions(rule_id);

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

-- Two sides of the same internal movement (savings -> checking, checking ->
-- credit card). Linked transactions are money you already had, so they count
-- as neither income nor spending.
CREATE TABLE transfer_links (
    id INTEGER PRIMARY KEY,
    out_txn_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id) ON DELETE CASCADE,
    in_txn_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'auto' CHECK (source IN ('auto','manual')),
    created_at TEXT NOT NULL
);

-- Pairs you told us are not a transfer, so we stop suggesting them.
CREATE TABLE transfer_dismissals (
    out_txn_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    in_txn_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    PRIMARY KEY (out_txn_id, in_txn_id)
);

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
       CASE WHEN s.id IS NULL THEN 0 ELSE 1 END    AS is_split,
       CASE WHEN EXISTS (SELECT 1 FROM transfer_links l
                         WHERE l.out_txn_id = t.id OR l.in_txn_id = t.id)
            THEN 1 ELSE 0 END                      AS is_transfer
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

-- What you have told the app about a category, rather than what it guessed.
-- A mortgage on the 1st, pay every other Friday, water every quarter: the app
-- can infer these from enough statements, but only you know them for certain,
-- and a wrong guess shows up as a category swinging for no reason.
--
-- Many per category, because one is a coincidence: two salaries land on the
-- same day this year and on different cadences the next, and a household with
-- one row for "Salary" can only describe that by adding the two up.
CREATE TABLE schedules (
    id           INTEGER PRIMARY KEY,
    category_id  INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    name         TEXT NOT NULL DEFAULT '',     -- whose, or which bill
    cadence      TEXT NOT NULL,
    amount_cents INTEGER NOT NULL DEFAULT 0,   -- per occurrence, not per month
    anchor_date  TEXT NOT NULL,                -- one date it lands on
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX idx_schedules_category ON schedules(category_id);

-- What an account was actually worth on a given day, as the bank shows it.
-- Statements say what moved, never what is there now: the balance is the one
-- fact an import cannot supply, and without it a forecast has no starting
-- point. Signed as money you have, so a card you owe on is negative.
CREATE TABLE account_balances (
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    as_of         TEXT NOT NULL,
    balance_cents INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (account_id, as_of)
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
    (3, """
    CREATE TABLE transfer_links (
        id INTEGER PRIMARY KEY,
        out_txn_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id) ON DELETE CASCADE,
        in_txn_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id) ON DELETE CASCADE,
        source TEXT NOT NULL DEFAULT 'auto' CHECK (source IN ('auto','manual')),
        created_at TEXT NOT NULL
    );

    CREATE TABLE transfer_dismissals (
        out_txn_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
        in_txn_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
        PRIMARY KEY (out_txn_id, in_txn_id)
    );

    DROP VIEW IF EXISTS txn_allocations;
    CREATE VIEW txn_allocations AS
    SELECT t.id                                     AS txn_id,
           t.account_id                             AS account_id,
           t.date                                   AS date,
           t.description                            AS description,
           t.merchant_key                           AS merchant_key,
           COALESCE(s.category_id, t.category_id)   AS category_id,
           COALESCE(s.amount_cents, t.amount_cents) AS amount_cents,
           CASE WHEN s.id IS NULL THEN 0 ELSE 1 END AS is_split,
           CASE WHEN EXISTS (SELECT 1 FROM transfer_links l
                             WHERE l.out_txn_id = t.id OR l.in_txn_id = t.id)
                THEN 1 ELSE 0 END                   AS is_transfer
    FROM transactions t
    LEFT JOIN transaction_splits s ON s.transaction_id = t.id;
    """),
    (4, """
    ALTER TABLE transactions ADD COLUMN classified_by TEXT NOT NULL DEFAULT '';
    ALTER TABLE transactions ADD COLUMN rule_id INTEGER REFERENCES rules(id) ON DELETE SET NULL;
    CREATE INDEX idx_txn_classified ON transactions(classified_by);
    """),
    (5, """
    CREATE INDEX IF NOT EXISTS idx_txn_rule ON transactions(rule_id);
    """),
    (6, """
    CREATE TABLE IF NOT EXISTS schedules (
        category_id  INTEGER PRIMARY KEY REFERENCES categories(id) ON DELETE CASCADE,
        cadence      TEXT NOT NULL,
        amount_cents INTEGER NOT NULL DEFAULT 0,
        anchor_date  TEXT NOT NULL,
        note         TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL
    );
    """),
    (7, """
    ALTER TABLE schedules RENAME TO schedules_v6;
    CREATE TABLE schedules (
        id           INTEGER PRIMARY KEY,
        category_id  INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
        name         TEXT NOT NULL DEFAULT '',
        cadence      TEXT NOT NULL,
        amount_cents INTEGER NOT NULL DEFAULT 0,
        anchor_date  TEXT NOT NULL,
        note         TEXT NOT NULL DEFAULT '',
        created_at   TEXT NOT NULL
    );
    INSERT INTO schedules (category_id, name, cadence, amount_cents, anchor_date,
                           note, created_at)
        SELECT category_id, '', cadence, amount_cents, anchor_date, note, created_at
        FROM schedules_v6;
    DROP TABLE schedules_v6;
    CREATE INDEX idx_schedules_category ON schedules(category_id);
    """),
    (8, """
    CREATE TABLE IF NOT EXISTS account_balances (
        account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        as_of         TEXT NOT NULL,
        balance_cents INTEGER NOT NULL,
        created_at    TEXT NOT NULL,
        PRIMARY KEY (account_id, as_of)
    );
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
    crossed = set()
    for target, sql in MIGRATIONS:
        if version < target:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {target}")
            conn.commit()
            version = target
            crossed.add(target)
    if 4 in crossed:
        # Rows imported before this column existed carry no record of who chose
        # their category. Label them by what the rules say today: if a rule
        # produces the category a row already has, that rule owns it — which is
        # exactly what you would change to re-file it.
        from .services.classify import label_existing_classifications
        label_existing_classifications(conn)
    if 5 in crossed:
        # Teaching the same rule twice used to add a second copy, and only the
        # first of any identical set is ever consulted. Clearing the copies is
        # provably invisible: every one of them is already unreachable. Rules
        # that name a *different* category are left for a human to decide on.
        from .services.classify import remove_duplicate_rules
        remove_duplicate_rules(conn)


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
