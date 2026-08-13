"""Editing many transactions at once, and pairing transfers by hand.

Both exist because the automatic paths leave gaps: the classifier's suggested
pattern is often cut too short, and the transfer matcher deliberately refuses
to guess when the two sides don't line up exactly.
"""
import re

import pytest

from app import config, db
from app.services import transfers


def get_csrf(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


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


@pytest.fixture()
def signed_in(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    return web


def open_db():
    return db.connect(config.DB_PATH)


def category(conn, name):
    return conn.execute("SELECT id FROM categories WHERE name = ?",
                        (name,)).fetchone()["id"]


def categories_of(conn, ids):
    marks = ",".join("?" * len(ids))
    return [r["category_id"] for r in conn.execute(
        f"SELECT id, category_id FROM transactions WHERE id IN ({marks}) ORDER BY id",
        ids).fetchall()]


# --- bulk recategorization ---------------------------------------------------

def test_bulk_applies_to_the_ticked_rows_only(signed_in):
    conn = open_db()
    a = add_txn(conn, 1, "2026-08-02", -1200, "MERCADO ONE")
    b = add_txn(conn, 1, "2026-08-03", -1500, "MERCADO TWO")
    untouched = add_txn(conn, 1, "2026-08-04", -900, "PETROL STATION")
    groceries = category(conn, "Groceries")

    r = signed_in.get("/transactions?month=2026-08")
    signed_in.post("/transactions/bulk", data={
        "csrf": get_csrf(r.text), "scope": "selected",
        "bulk_category": str(groceries), "txn": [str(a), str(b)]})

    assert categories_of(conn, [a, b, untouched]) == [groceries, groceries, None]
    conn.close()


def test_bulk_can_apply_to_everything_matching_the_filters(signed_in):
    """"Apply to all matching" has to mean the same rows the list is showing,
    including the ones on later pages."""
    conn = open_db()
    ids = [add_txn(conn, 1, f"2026-08-{d:02d}", -1000 - d, "MERCADO CENTRAL")
           for d in range(1, 6)]
    other_month = add_txn(conn, 1, "2026-07-04", -500, "MERCADO CENTRAL")
    other_shop = add_txn(conn, 1, "2026-08-06", -700, "HARDWARE STORE")
    groceries = category(conn, "Groceries")

    r = signed_in.get("/transactions?month=2026-08&q=MERCADO")
    signed_in.post("/transactions/bulk", data={
        "csrf": get_csrf(r.text), "scope": "filtered",
        "bulk_category": str(groceries),
        "f_month": "2026-08", "f_account": "", "f_category": "", "f_q": "MERCADO"})

    assert categories_of(conn, ids) == [groceries] * 5
    assert categories_of(conn, [other_month, other_shop]) == [None, None]
    conn.close()


def test_bulk_replaces_a_split_rather_than_leaving_orphan_parts(signed_in):
    conn = open_db()
    txn = add_txn(conn, 1, "2026-08-02", -10000, "COSTCO RUN")
    groceries, clothes = category(conn, "Groceries"), category(conn, "Clothing")
    conn.execute("INSERT INTO transaction_splits (transaction_id, category_id, "
                 "amount_cents) VALUES (?, ?, ?)", (txn, groceries, -6000))
    conn.execute("INSERT INTO transaction_splits (transaction_id, category_id, "
                 "amount_cents) VALUES (?, ?, ?)", (txn, clothes, -4000))
    conn.commit()

    r = signed_in.get("/transactions?month=2026-08")
    signed_in.post("/transactions/bulk", data={
        "csrf": get_csrf(r.text), "scope": "selected",
        "bulk_category": str(groceries), "txn": str(txn)})

    assert conn.execute("SELECT COUNT(*) FROM transaction_splits WHERE "
                        "transaction_id = ?", (txn,)).fetchone()[0] == 0
    # and the money lands once, in the chosen category
    total = conn.execute("SELECT SUM(amount_cents) FROM txn_allocations WHERE "
                         "txn_id = ?", (txn,)).fetchone()[0]
    assert total == -10000
    conn.close()


def test_bulk_without_a_category_changes_nothing(signed_in):
    conn = open_db()
    a = add_txn(conn, 1, "2026-08-02", -1200, "MERCADO ONE")
    r = signed_in.get("/transactions?month=2026-08")
    signed_in.post("/transactions/bulk", data={
        "csrf": get_csrf(r.text), "scope": "selected", "bulk_category": "",
        "txn": str(a)})
    assert categories_of(conn, [a]) == [None]
    conn.close()


def test_bulk_needs_a_csrf_token(signed_in):
    conn = open_db()
    a = add_txn(conn, 1, "2026-08-02", -1200, "MERCADO ONE")
    groceries = category(conn, "Groceries")
    r = signed_in.post("/transactions/bulk",
                       data={"scope": "selected", "bulk_category": str(groceries),
                             "txn": str(a)}, follow_redirects=False)
    assert r.status_code == 403
    assert categories_of(conn, [a]) == [None]
    conn.close()


# --- the pattern you are about to remember -----------------------------------

def test_match_count_says_how_broad_a_pattern_is(signed_in):
    conn = open_db()
    for d in range(1, 4):
        add_txn(conn, 1, f"2026-08-0{d}", -1000, f"MERCADO CENTRAL LISBOA {d}")
    add_txn(conn, 1, "2026-08-09", -2000, "PETROL STATION")
    conn.close()

    narrow = signed_in.get("/rules/match-count?pattern=MERCADO+CENTRAL").json()
    assert narrow == {"count": 3, "total": 4}

    # a pattern cut too short catches things it shouldn't; the count shows it
    broad = signed_in.get("/rules/match-count?pattern=A").json()
    assert broad["count"] == 4

    assert signed_in.get("/rules/match-count?pattern=").json()["count"] == 0
    assert signed_in.get(
        "/rules/match-count?pattern=x&match_type=nonsense").json()["count"] == 0


def test_edited_pattern_is_what_gets_remembered(signed_in):
    """The suggestion is often cut short — whatever you type must win."""
    conn = open_db()
    txn = add_txn(conn, 1, "2026-08-02", -1200, "SQ *MERCADO CENTRAL LISBOA")
    also = add_txn(conn, 1, "2026-08-05", -3400, "SQ *MERCADO CENTRAL PORTO")
    groceries = category(conn, "Groceries")

    r = signed_in.get("/review")
    signed_in.post(f"/review/{txn}", data={
        "csrf": get_csrf(r.text), "category_id": str(groceries),
        "remember": "1", "pattern": "MERCADO CENTRAL"})

    rule = conn.execute("SELECT pattern, category_id FROM rules WHERE pattern = ?",
                        ("MERCADO CENTRAL",)).fetchone()
    assert rule is not None, "the typed pattern, not the suggested one, is stored"
    assert rule["category_id"] == groceries
    # the rule is applied to the backlog straight away
    assert categories_of(conn, [txn, also]) == [groceries, groceries]
    conn.close()


def test_review_page_offers_the_pattern_as_an_editable_field(signed_in):
    conn = open_db()
    add_txn(conn, 1, "2026-08-02", -1200, "SQ *MERCADO CENTRAL LISBOA")
    conn.close()
    r = signed_in.get("/review")
    assert re.search(r'<input type="text" name="pattern_\d+"', r.text)
    assert "Always file transactions matching:" in r.text


# --- manual transfer pairing -------------------------------------------------

def test_manual_pairing_flow_links_two_sides_the_scan_missed(signed_in):
    conn = open_db()
    conn.execute("INSERT OR IGNORE INTO accounts (id, name, type, created_at) "
                 "VALUES (2, 'Savings', 'savings', 'now')")
    out = add_txn(conn, 1, "2026-08-05", -100000, "WIRE OUT")
    inn = add_txn(conn, 2, "2026-08-09", 99500, "WIRE IN LESS FEE")
    conn.commit()
    assert transfers.find_candidates(conn) == []      # amounts differ, no guess

    # the transfers page points at the manual flow
    r = signed_in.get("/transfers")
    assert "/transfers/manual" in r.text

    # step 1: both unpaired rows are offered
    r = signed_in.get("/transfers/manual")
    assert "WIRE OUT" in r.text and "WIRE IN LESS FEE" in r.text

    # step 2: pick a side, and the other one is offered despite the fee
    r = signed_in.get(f"/transfers/manual?side={out}")
    assert "WIRE IN LESS FEE" in r.text
    signed_in.post("/transfers/pair", data={
        "csrf": get_csrf(r.text), "txn_a": str(out), "txn_b": str(inn)})

    partner = transfers.linked_partner(conn, out)
    assert partner["id"] == inn and partner["source"] == "manual"
    conn.close()


def test_manual_pairing_search_narrows_the_pool(signed_in):
    conn = open_db()
    add_txn(conn, 1, "2026-08-05", -100000, "WIRE OUT")
    add_txn(conn, 1, "2026-08-06", -2500, "COFFEE SHOP")
    conn.close()
    r = signed_in.get("/transfers/manual?q=WIRE")
    assert "WIRE OUT" in r.text and "COFFEE SHOP" not in r.text


def test_transaction_page_links_and_unlinks_a_transfer(signed_in):
    conn = open_db()
    conn.execute("INSERT OR IGNORE INTO accounts (id, name, type, created_at) "
                 "VALUES (2, 'Visa', 'credit', 'now')")
    out = add_txn(conn, 1, "2026-08-05", -85000, "MOVED MONEY")
    inn = add_txn(conn, 2, "2026-08-07", 85000, "ARRIVED")
    conn.commit()

    r = signed_in.get(f"/transactions/{out}")
    assert "Pair this with its other side" in r.text
    signed_in.post(f"/transactions/{out}/link", data={
        "csrf": get_csrf(r.text), "other_id": str(inn)})
    assert transfers.linked_partner(conn, out)["id"] == inn

    r = signed_in.get(f"/transactions/{out}")
    assert "Matched as a transfer" in r.text
    signed_in.post(f"/transactions/{out}/link",
                   data={"csrf": get_csrf(r.text), "unlink": "1"})
    assert transfers.linked_partner(conn, out) is None
    conn.close()


def test_bulk_returns_to_the_list_you_were_looking_at(signed_in):
    """The filtered list URL travels through the form; a mis-encoded one sent
    the browser to a 404 instead of back to the rows just edited."""
    conn = open_db()
    txn = add_txn(conn, 1, "2026-08-02", -1200, "MERCADO ONE")
    groceries = category(conn, "Groceries")
    conn.close()

    listing = "/transactions?month=2026-08&q=MERCADO"
    r = signed_in.get(listing)
    # HTML-escaped in the attribute, but a plain path once the browser posts it
    assert 'name="back" value="/transactions?month=2026-08&amp;q=MERCADO"' in r.text

    r = signed_in.post("/transactions/bulk", data={
        "csrf": get_csrf(r.text), "scope": "selected",
        "bulk_category": str(groceries), "txn": str(txn), "back": listing},
        follow_redirects=False)
    assert r.headers["location"] == listing
    assert signed_in.get(r.headers["location"]).status_code == 200


def test_bulk_refuses_an_offsite_return_url(signed_in):
    conn = open_db()
    txn = add_txn(conn, 1, "2026-08-02", -1200, "MERCADO ONE")
    groceries = category(conn, "Groceries")
    conn.close()
    r = signed_in.get("/transactions?month=2026-08")
    r = signed_in.post("/transactions/bulk", data={
        "csrf": get_csrf(r.text), "scope": "selected",
        "bulk_category": str(groceries), "txn": str(txn),
        "back": "//evil.example/steal"}, follow_redirects=False)
    assert r.headers["location"] == "/transactions"


# --- income timing on the dashboard -----------------------------------------

def test_dashboard_says_when_the_next_payday_lands(signed_in):
    """Fortnightly pay makes "am I on budget?" unanswerable without knowing how
    many paydays are still to come."""
    from datetime import date, timedelta

    conn = open_db()
    salary = category(conn, "Salary")
    day = date(2026, 5, 1)
    while day <= date(2026, 8, 7):
        add_txn(conn, 1, day.isoformat(), 200000, "ACME PAYROLL", salary)
        day += timedelta(days=14)
    conn.close()

    r = signed_in.get("/budgets?month=2026-08")
    assert "every 2 weeks" in r.text
    assert "2 paydays" in r.text and "7 Aug, 21 Aug" in r.text
    assert "$4,000.00" in r.text            # two paydays, not a flat month

    r = signed_in.get("/budgets?month=2026-10")
    assert "3 paydays" in r.text and "2 Oct, 16 Oct, 30 Oct" in r.text
    assert "$6,000.00" in r.text            # the month with a third payday

    # a month whose paydays are all behind us reads as settled, not pending
    r = signed_in.get("/?month=2026-06")
    assert "has landed" in r.text
    assert "more payday" not in r.text
