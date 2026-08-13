"""Opening a figure on the insights page into the transactions behind it.

Every number there is an aggregate. The point of the drill-down is that the
rows it shows add up to the figure that was clicked — otherwise it is a second,
differently-wrong answer rather than an explanation.
"""
import pytest

from app.services import budgets, drill, insights, transfers

from test_bulk_and_pairing import add_txn, category, open_db  # noqa: F401


@pytest.fixture()
def signed_in(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    return web


def account(conn, acc_id=1, name="Checking", type_="checking"):
    conn.execute("INSERT OR IGNORE INTO accounts (id, name, type, created_at) "
                 "VALUES (?, ?, ?, 'now')", (acc_id, name, type_))
    conn.commit()


def total(rows):
    return sum(r["amount_cents"] for r in rows)


# --- the rows match the aggregate --------------------------------------------

def test_category_rows_add_up_to_the_category_total(conn):
    account(conn)
    groceries, fuel = category(conn, "Groceries"), category(conn, "Fuel")
    add_txn(conn, 1, "2026-08-02", -3000, "MARKET ONE", groceries)
    add_txn(conn, 1, "2026-08-09", -4500, "MARKET TWO", groceries)
    add_txn(conn, 1, "2026-08-11", -2000, "PETROL", fuel)
    add_txn(conn, 1, "2026-07-02", -9900, "LAST MONTH", groceries)

    rows = drill.rows_for(conn, "category", "2026-08", "Groceries")
    assert len(rows) == 2
    assert total(rows) == -7500
    # and that is what the month summary says for the same category
    summary = budgets.month_summary(conn, "2026-08")
    line = next(l for g in summary.groups for l in g.lines if l.name == "Groceries")
    assert line.actual == 7500


def test_a_split_shows_only_the_part_in_that_category(conn):
    """Aggregates read allocations, so the drill-down has to as well — showing
    the whole receipt under one of its categories would overstate it."""
    account(conn)
    groceries, clothes = category(conn, "Groceries"), category(conn, "Clothing")
    txn = add_txn(conn, 1, "2026-08-02", -10000, "COSTCO RUN", groceries)
    for cat, cents in ((groceries, -6000), (clothes, -4000)):
        conn.execute("INSERT INTO transaction_splits (transaction_id, category_id, "
                     "amount_cents) VALUES (?, ?, ?)", (txn, cat, cents))
    conn.commit()

    grocery_rows = drill.rows_for(conn, "category", "2026-08", "Groceries")
    assert total(grocery_rows) == -6000
    assert grocery_rows[0]["is_split"] == 1
    assert total(drill.rows_for(conn, "category", "2026-08", "Clothing")) == -4000


def test_merchant_rows_match_the_top_merchants_figure(conn):
    account(conn)
    groceries = category(conn, "Groceries")
    for day, cents in ((2, -1200), (9, -3400), (17, -800)):
        add_txn(conn, 1, f"2026-08-{day:02d}", cents, "WALMART SUPERCENTER", groceries)

    top = insights.top_merchants(conn, "2026-08")[0]
    rows = drill.rows_for(conn, "merchant", "2026-08", top["merchant"])
    assert len(rows) == top["n"] == 3
    assert -total(rows) == top["spent"] == 5400


def test_spending_rows_match_the_pace_figure(conn):
    account(conn)
    groceries = category(conn, "Groceries")
    add_txn(conn, 1, "2026-08-02", -3000, "SHOP", groceries)
    add_txn(conn, 1, "2026-08-09", -4500, "SHOP TWO", groceries)
    add_txn(conn, 1, "2026-08-10", 500000, "PAY", category(conn, "Salary"))

    from datetime import date
    pace = insights.spending_pace(conn, "2026-08", today=date(2026, 8, 31))
    rows = drill.rows_for(conn, "spending", "2026-08")
    assert -total(rows) == pace["spent"] == 7500       # income is not spending


def test_transfers_are_left_out_of_spending_just_as_they_are_in_the_totals(conn):
    account(conn)
    account(conn, 2, "Visa", "credit")
    groceries = category(conn, "Groceries")
    add_txn(conn, 1, "2026-08-02", -3000, "SHOP", groceries)
    out = add_txn(conn, 1, "2026-08-05", -85000, "TRANSFER TO VISA")
    inn = add_txn(conn, 2, "2026-08-05", 85000, "PAYMENT THANK YOU")
    transfers.link(conn, out, inn, source="manual")

    rows = drill.rows_for(conn, "spending", "2026-08")
    assert [r["txn_id"] for r in rows] == [
        conn.execute("SELECT id FROM transactions WHERE description = 'SHOP'"
                     ).fetchone()["id"]]


def test_recurring_looks_across_months_not_just_this_one(conn):
    """A subscription is only interesting as a series."""
    account(conn)
    subs = category(conn, "Subscriptions & Streaming")
    for month in ("2026-06", "2026-07", "2026-08"):
        add_txn(conn, 1, f"{month}-04", -1599, "NETFLIX.COM", subs)

    this_month = drill.rows_for(conn, "merchant", "2026-08", "NETFLIX.COM")
    assert len(this_month) == 1
    every_month = drill.rows_for(conn, "recurring", "2026-08", "NETFLIX.COM")
    assert len(every_month) == 3
    assert every_month[0]["date"] > every_month[-1]["date"]   # newest first


def test_unmatched_rows_are_the_transfers_with_no_other_side(conn):
    account(conn)
    account(conn, 2, "Visa", "credit")
    paid = category(conn, "Credit Card Payment")
    lonely = add_txn(conn, 1, "2026-08-05", -85000, "TRANSFER TO VISA", paid)
    out = add_txn(conn, 1, "2026-08-20", -40000, "TRANSFER TWO", paid)
    inn = add_txn(conn, 2, "2026-08-20", 40000, "PAYMENT THANK YOU", paid)
    transfers.link(conn, out, inn, source="manual")

    rows = drill.rows_for(conn, "unmatched", "2026-08")
    assert [r["txn_id"] for r in rows] == [lonely]
    assert total(rows) == -85000
    # the banner's own count comes from the same place
    assert len(transfers.unmatched_transfers(conn, "2026-08")) == 1


def test_uncategorized_rows(conn):
    account(conn)
    add_txn(conn, 1, "2026-08-02", -3000, "MYSTERY")
    add_txn(conn, 1, "2026-08-03", -1000, "KNOWN", category(conn, "Groceries"))
    rows = drill.rows_for(conn, "uncategorized", "2026-08")
    assert [r["description"] for r in rows] == ["MYSTERY"]


def test_an_unknown_kind_returns_nothing_rather_than_erroring(conn):
    assert drill.rows_for(conn, "nonsense", "2026-08") == []
    assert drill.rows_for(conn, "", "2026-08") == []


# --- the endpoint and the page ------------------------------------------------

def test_drill_endpoint_returns_a_table_of_the_transactions(signed_in):
    conn = open_db()
    groceries = category(conn, "Groceries")
    add_txn(conn, 1, "2026-08-02", -3000, "MARKET ONE", groceries)
    add_txn(conn, 1, "2026-08-09", -4500, "MARKET TWO", groceries)
    conn.close()

    r = signed_in.get("/insights/drill?kind=category&key=Groceries&month=2026-08")
    assert r.status_code == 200
    assert "MARKET ONE" in r.text and "MARKET TWO" in r.text
    assert "2 transactions" in r.text
    assert "Open in Activity" in r.text
    # a fragment, not a whole page — it is spliced into the insights page
    assert "<html" not in r.text


def test_drill_endpoint_needs_a_login(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    web.post("/logout")
    r = web.get("/insights/drill?kind=category&key=Groceries&month=2026-08",
                follow_redirects=False)
    assert r.status_code in (303, 401, 403)


def test_drill_endpoint_survives_a_missing_month(signed_in):
    r = signed_in.get("/insights/drill?kind=category&key=Groceries")
    assert r.status_code == 200


def test_insights_page_marks_its_figures_as_openable(signed_in):
    conn = open_db()
    groceries = category(conn, "Groceries")
    for day in (2, 9, 17):
        add_txn(conn, 1, f"2026-08-{day:02d}", -3000, "WALMART SUPERCENTER", groceries)
    conn.close()

    r = signed_in.get("/insights?month=2026-08")
    assert 'data-drill-kind="merchant"' in r.text
    assert 'data-drill-kind="spending"' in r.text
    assert 'data-drill-month="2026-08"' in r.text


# --- the two things that were not working -------------------------------------

def test_spending_pace_uses_a_carried_forward_budget(conn):
    """Regression: pace read the budgets table directly, so a month inheriting
    its budget showed a budget everywhere else and none on the pace card."""
    account(conn)
    groceries = category(conn, "Groceries")
    budgets.set_budget(conn, groceries, "2026-06", 60000)
    conn.commit()
    add_txn(conn, 1, "2026-08-02", -30000, "SHOP", groceries)

    assert budgets.expense_budget_total(conn, "2026-08") == 60000
    from datetime import date
    pace = insights.spending_pace(conn, "2026-08", today=date(2026, 8, 15))
    assert pace["budget"] == 60000
    assert pace["on_pace"] > 0


def test_budget_total_counts_only_spending_categories(conn):
    salary = category(conn, "Salary")
    groceries = category(conn, "Groceries")
    transfers_cat = conn.execute(
        "SELECT id FROM categories WHERE excluded = 1 LIMIT 1").fetchone()["id"]
    for cat, cents in ((salary, 500000), (groceries, 60000), (transfers_cat, 90000)):
        budgets.set_budget(conn, cat, "2026-08", cents)
    conn.commit()
    assert budgets.expense_budget_total(conn, "2026-08") == 60000


def test_transfers_page_shows_the_ones_missing_their_other_side(signed_in):
    """Regression: the insights banner sent you to /transfers, which listed
    suggestions and matched pairs and never mentioned these at all."""
    conn = open_db()
    paid = category(conn, "Credit Card Payment")
    add_txn(conn, 1, "2026-08-05", -85000, "TRANSFER TO VISA", paid)
    conn.close()

    r = signed_in.get("/transfers")
    assert "Missing the other side (1)" in r.text
    assert "TRANSFER TO VISA" in r.text
    assert "/transfers/manual?side=" in r.text       # and you can act on it


def test_all_unmatched_spans_months(conn):
    account(conn)
    paid = category(conn, "Credit Card Payment")
    add_txn(conn, 1, "2026-08-05", -85000, "AUGUST PAYMENT", paid)
    add_txn(conn, 1, "2026-05-05", -70000, "MAY PAYMENT", paid)

    assert len(transfers.unmatched_transfers(conn, "2026-08")) == 1
    every = transfers.all_unmatched(conn)
    assert [r["description"] for r in every] == ["AUGUST PAYMENT", "MAY PAYMENT"]
