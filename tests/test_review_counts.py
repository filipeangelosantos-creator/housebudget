"""The dashboard's counts and the review queue must agree."""
from datetime import date

from app.services import review, transfers


def add_txn(conn, acc_id, date_str, cents, desc, category_id=None):
    from app.services.classify import merchant_key, normalize_desc
    conn.execute("INSERT OR IGNORE INTO accounts (id, name, type, created_at) "
                 "VALUES (?, ?, 'checking', 'now')", (acc_id, f"Acct {acc_id}"))
    cur = conn.execute(
        "INSERT INTO transactions (account_id, date, amount_cents, description, "
        "normalized_desc, merchant_key, category_id, dedupe_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'now')",
        (acc_id, date_str, cents, desc, normalize_desc(desc), merchant_key(desc),
         category_id, f"{acc_id}|{date_str}|{cents}|{desc}"))
    conn.commit()
    return cur.lastrowid


def test_paired_transfers_are_not_counted_as_needing_a_category(conn):
    """Regression: the dashboard said "6 still uncategorized" and the review
    queue said everything was done, because only the queue skipped pairs."""
    add_txn(conn, 1, "2026-08-02", -1200, "MYSTERY SHOP")          # genuinely open
    out = add_txn(conn, 1, "2026-08-05", -50000, "OPERACAO 8842")
    inn = add_txn(conn, 2, "2026-08-05", 50000, "CREDITO CONTA")

    assert review.needs_category_count(conn) == 3
    assert len(review.needs_category_rows(conn)) == 3

    transfers.link(conn, out, inn, source="manual")

    # both sides drop out of the count and the queue together
    assert review.needs_category_count(conn) == 1
    rows = review.needs_category_rows(conn)
    assert len(rows) == 1
    assert rows[0]["description"] == "MYSTERY SHOP"


def test_dashboard_banner_and_review_page_agree(web):
    """Whatever number the dashboard shows, the queue must have that many."""
    import re

    from app import config, db

    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    conn = db.connect(config.DB_PATH)
    add_txn(conn, 1, "2026-08-02", -1200, "MYSTERY SHOP")
    out = add_txn(conn, 1, "2026-08-05", -50000, "OPERACAO 8842")
    inn = add_txn(conn, 2, "2026-08-05", 50000, "CREDITO CONTA")
    transfers.link(conn, out, inn, source="manual")
    conn.close()

    r = web.get("/?month=2026-08")
    banner = re.search(r"<strong>(\d+) transaction", r.text)
    shown = int(banner.group(1)) if banner else 0
    assert shown == 1, "dashboard should count exactly the open one"

    r = web.get("/review")
    assert "Needs a category (1)" in r.text
    assert "MYSTERY SHOP" in r.text
    assert "OPERACAO" not in r.text


def test_future_dated_transactions_are_reported(conn):
    add_txn(conn, 1, "2026-08-02", -1200, "NORMAL")
    add_txn(conn, 1, "2026-12-30", -1000, "MISDATED ONE")
    add_txn(conn, 1, "2026-12-26", -2000, "MISDATED TWO")

    ahead = review.future_dated(conn, today=date(2026, 8, 13))
    assert len(ahead) == 1
    assert ahead[0]["month"] == "2026-12"
    assert ahead[0]["n"] == 2
    assert ahead[0]["first_date"] == "2026-12-26"

    assert review.future_dated(conn, today=date(2027, 1, 1)) == []


def test_dashboard_activity_is_scoped_to_the_month(web):
    """A future-dated row was appearing under "latest activity" on every
    month's dashboard, which is how the misdating stayed invisible."""
    from app import config, db

    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    conn = db.connect(config.DB_PATH)
    add_txn(conn, 1, "2026-08-02", -1200, "AUGUST PURCHASE")
    add_txn(conn, 1, "2026-12-30", -1000, "MISDATED DECEMBER")
    conn.close()

    r = web.get("/?month=2026-08")
    assert "AUGUST PURCHASE" in r.text
    assert "MISDATED DECEMBER" not in r.text     # belongs to December, not here
    assert "dated in the future" in r.text       # but it is flagged

    r = web.get("/?month=2026-12")
    assert "MISDATED DECEMBER" in r.text
