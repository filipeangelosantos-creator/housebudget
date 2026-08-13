"""Finding a transaction by what it cost, and tracing where one came from.

A bank line is often just a reference number — "COMENITY PAY BH WEB PYMT
050626 P26126550494090" says nothing about which card it paid. What you do
know is the amount, and which statement it turned up in.
"""
import re

import pytest

from app.routes.transactions import _amount_filter

from test_bulk_and_pairing import add_txn, category, get_csrf, open_db  # noqa: F401


@pytest.fixture()
def signed_in(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    return web


# --- reading the search box as money ------------------------------------------

def test_a_plain_amount_matches_either_direction():
    sql, params = _amount_filter("400.27")
    assert params == [40027]
    assert "ABS(t.amount_cents) =" in sql


def test_a_sign_pins_the_direction():
    assert _amount_filter("-400.27") == ("t.amount_cents = ?", [-40027])
    assert _amount_filter("+895") == ("t.amount_cents = ?", [89500])


def test_comparisons_and_ranges():
    assert _amount_filter(">500") == ("ABS(t.amount_cents) > ?", [50000])
    assert _amount_filter("<=50") == ("ABS(t.amount_cents) <= ?", [5000])
    assert _amount_filter("100-200") == (
        "ABS(t.amount_cents) BETWEEN ? AND ?", [10000, 20000])
    # written the other way round, it still means the same band
    assert _amount_filter("200-100")[1] == [10000, 20000]


def test_thousands_separators_and_a_currency_symbol():
    assert _amount_filter("1,234.56")[1] == [123456]
    assert _amount_filter("$400.27")[1] == [40027]


def test_text_is_left_to_the_text_search():
    assert _amount_filter("STARBUCKS") is None
    assert _amount_filter("") is None
    assert _amount_filter("0") is None
    # a reference number starting with a letter is text, not an amount
    assert _amount_filter("P26126550494090") is None
    # and a month is a date, not a range from 5.00 to 2,026.00
    assert _amount_filter("2026-05") is None
    assert _amount_filter("2026-05-07") is None


# --- searching the list --------------------------------------------------------

def test_searching_by_amount_finds_the_transaction(signed_in):
    conn = open_db()
    wanted = add_txn(conn, 1, "2026-05-07", -40027,
                     "COMENITY PAY BH WEB PYMT 050626 P26126550494090")
    add_txn(conn, 1, "2026-05-09", -1250, "COFFEE")
    conn.close()

    r = signed_in.get("/transactions?month=all&q=400.27")
    assert "COMENITY" in r.text
    assert "COFFEE" not in r.text
    assert "1 transaction" in r.text
    assert f'/transactions/{wanted}' in r.text


def test_the_sign_separates_money_out_from_money_in(signed_in):
    conn = open_db()
    add_txn(conn, 1, "2026-03-19", -89500, "DEBIT ADJUSTMENT")
    add_txn(conn, 1, "2026-03-19", 89500, "CREDIT ADJUSTMENT")
    conn.close()

    both = signed_in.get("/transactions?month=all&q=895")
    assert "DEBIT ADJUSTMENT" in both.text and "CREDIT ADJUSTMENT" in both.text

    out_only = signed_in.get("/transactions?month=all&q=-895")
    assert "DEBIT ADJUSTMENT" in out_only.text
    assert "CREDIT ADJUSTMENT" not in out_only.text


def test_a_range_search_narrows_to_a_band(signed_in):
    conn = open_db()
    add_txn(conn, 1, "2026-05-01", -5000, "SMALL")
    add_txn(conn, 1, "2026-05-02", -15000, "MIDDLE")
    add_txn(conn, 1, "2026-05-03", -95000, "LARGE")
    conn.close()

    r = signed_in.get("/transactions?month=all&q=100-200")
    assert "MIDDLE" in r.text
    assert "SMALL" not in r.text and "LARGE" not in r.text

    r = signed_in.get("/transactions?month=all&q=>500")
    assert "LARGE" in r.text and "MIDDLE" not in r.text


def test_a_number_that_is_also_in_the_text_finds_both(signed_in):
    """A bare number is ambiguous, so neither reading is thrown away."""
    conn = open_db()
    add_txn(conn, 1, "2026-05-01", -40027, "SOME SHOP")
    add_txn(conn, 1, "2026-05-02", -999, "INVOICE 400.27 SETTLEMENT")
    conn.close()

    r = signed_in.get("/transactions?month=all&q=400.27")
    assert "SOME SHOP" in r.text
    assert "INVOICE 400.27 SETTLEMENT" in r.text


def test_bulk_edit_respects_an_amount_search(signed_in):
    """"Apply to all matching these filters" has to mean the same rows."""
    conn = open_db()
    hit = add_txn(conn, 1, "2026-05-01", -40027, "ONE")
    miss = add_txn(conn, 1, "2026-05-02", -999, "TWO")
    groceries = category(conn, "Groceries")

    r = signed_in.get("/transactions?month=all&q=400.27")
    signed_in.post("/transactions/bulk", data={
        "csrf": get_csrf(r.text), "scope": "filtered",
        "bulk_category": str(groceries),
        "f_month": "", "f_account": "", "f_category": "", "f_q": "400.27"})

    cats = {r["id"]: r["category_id"] for r in
            conn.execute("SELECT id, category_id FROM transactions")}
    assert cats[hit] == groceries and cats[miss] is None
    conn.close()


# --- the list that grows as you scroll -----------------------------------------

def test_rows_only_returns_a_fragment_for_the_next_page(signed_in):
    conn = open_db()
    for i in range(150):
        add_txn(conn, 1, f"2026-05-{i % 28 + 1:02d}", -100 - i, f"ROW {i:03d}")
    conn.close()

    first = signed_in.get("/transactions?month=all")
    assert first.text.count('class="txn-check"') == 100
    assert 'id="txn-pager"' in first.text
    assert 'data-pages="2"' in first.text

    second = signed_in.get("/transactions?month=all&rows_only=1&page=2")
    assert second.text.count('class="txn-check"') == 50
    assert "<html" not in second.text          # a fragment, spliced in as-is

    # the two pages are different rows, so appending one to the other is the
    # whole list and nothing is shown twice
    ids = re.compile(r'name="txn" value="(\d+)"')
    page1, page2 = set(ids.findall(first.text)), set(ids.findall(second.text))
    assert len(page1) == 100 and len(page2) == 50
    assert not page1 & page2


def test_a_short_list_has_no_pager_to_scroll_into(signed_in):
    conn = open_db()
    add_txn(conn, 1, "2026-05-01", -100, "ONLY ONE")
    conn.close()
    r = signed_in.get("/transactions?month=all")
    assert 'id="txn-pager"' not in r.text


# --- tracing one transaction ---------------------------------------------------

def test_the_page_says_which_statement_the_row_came_from(signed_in):
    conn = open_db()
    txn = add_txn(conn, 1, "2026-05-07", -40027,
                  "COMENITY PAY BH WEB PYMT 050626 P26126550494090")
    conn.execute("INSERT INTO imports (id, account_id, filename, uploaded_by, "
                 "created_at) VALUES (1, 1, 'citizens-may-2026.pdf', 1, "
                 "'2026-06-01T10:00:00Z')")
    conn.execute("UPDATE transactions SET import_id = 1 WHERE id = ?", (txn,))
    conn.commit()
    conn.close()

    r = signed_in.get(f"/transactions/{txn}")
    assert "Where this came from" in r.text
    assert "citizens-may-2026.pdf" in r.text
    assert "2026-06-01" in r.text
    assert "Acct 1" in r.text


def test_the_page_lists_the_rest_of_that_merchant_to_identify_it(signed_in):
    """A repeating amount is usually what tells you which card a payment is."""
    conn = open_db()
    txn = add_txn(conn, 1, "2026-05-07", -40027, "COMENITY PAY BH WEB PYMT 050626 P2")
    add_txn(conn, 1, "2026-04-07", -39811, "COMENITY PAY BH WEB PYMT 040626 P9")
    add_txn(conn, 1, "2026-03-07", -41250, "COMENITY PAY BH WEB PYMT 030626 P4")
    conn.close()

    r = signed_in.get(f"/transactions/{txn}")
    assert "2 other transactions" in r.text
    assert "398.11" in r.text and "412.50" in r.text


def test_a_manual_entry_says_so_instead_of_naming_a_file(signed_in):
    conn = open_db()
    txn = add_txn(conn, 1, "2026-05-07", -1000, "CASH LUNCH")
    conn.execute("UPDATE transactions SET manual = 1 WHERE id = ?", (txn,))
    conn.commit()
    conn.close()

    r = signed_in.get(f"/transactions/{txn}")
    assert "Added by hand, not from a statement" in r.text


def test_an_unpaired_card_payment_explains_what_is_missing(signed_in):
    conn = open_db()
    txn = add_txn(conn, 1, "2026-05-07", -40027, "COMENITY PAY BH WEB PYMT",
                  category(conn, "Credit Card Payment"))
    conn.close()

    r = signed_in.get(f"/transactions/{txn}")
    assert "the other side was" in r.text
    assert "/import" in r.text
