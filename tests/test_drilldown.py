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


def get_csrf_attr(html: str) -> str:
    """The drill panel carries its token as an attribute, not a form field."""
    import re
    m = re.search(r'data-csrf="([^"]+)"', html)
    assert m, "no csrf token in the panel"
    return m.group(1)


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


# --- opening a month that isn't the one the page is showing -------------------

def test_every_chart_bar_carries_the_month_it_stands_for(conn):
    """The whole point of a twelve-month chart is the eleven months that
    aren't this one, so a bar can't drill into the header's month."""
    from app.services import charts
    account(conn)
    groceries, salary = category(conn, "Groceries"), category(conn, "Salary")
    for month in ("2026-06", "2026-07", "2026-08"):
        add_txn(conn, 1, f"{month}-02", -3000, "MARKET", groceries)
        add_txn(conn, 1, f"{month}-01", 500000, "PAY", salary)

    nets = charts.net_bars_chart(insights.monthly_net(conn, "2026-08", 3))
    assert 'data-drill-kind="net" data-drill-month="2026-06"' in nets
    assert 'data-drill-month="2026-07"' in nets

    flow = charts.cashflow_chart(insights.cashflow(conn, "2026-08", 3))
    assert 'data-drill-kind="income" data-drill-month="2026-07"' in flow
    assert 'data-drill-kind="spending" data-drill-month="2026-07"' in flow

    stacked = charts.stacked_chart(insights.category_composition(conn, "2026-08", 3))
    assert 'data-drill-month="2026-07"' in stacked
    assert 'data-drill-key="Groceries"' in stacked


def test_a_rolled_up_other_slice_has_nothing_to_open(conn):
    """"Other" is whatever fell outside the top categories — there is no one
    category behind it, so it must not pretend to open into one."""
    from app.services import charts
    account(conn)
    for i in range(9):
        cat = category(conn, ["Groceries", "Fuel", "Clothing",
                              "Restaurants & Takeout", "Pharmacy",
                              "Home Maintenance", "Gifts & Donations",
                              "Hobbies", "Pets"][i])
        add_txn(conn, 1, "2026-08-02", -(9 - i) * 1000, f"SHOP {i}", cat)

    composition = insights.category_composition(conn, "2026-08", 1)
    assert any(s["name"] == "Other" for s in composition["series"])
    svg = charts.stacked_chart(composition)
    assert 'data-drill-key="Other"' not in svg


def test_a_trend_bar_opens_its_own_month(conn):
    from app.services import charts
    svg = charts.spark_bars([100, 200], months=["2026-07", "2026-08"],
                            category="Groceries")
    assert 'data-drill-kind="category" data-drill-month="2026-07"' in svg
    assert 'data-drill-key="Groceries"' in svg
    # and without months it stays a plain picture, as the callers that pass
    # nothing expect
    assert "data-drill-kind" not in charts.spark_bars([100, 200])


def test_net_rows_add_up_to_that_months_net(conn):
    account(conn)
    add_txn(conn, 1, "2026-07-01", 500000, "PAY", category(conn, "Salary"))
    add_txn(conn, 1, "2026-07-02", -3000, "MARKET", category(conn, "Groceries"))
    add_txn(conn, 1, "2026-08-02", -9900, "OTHER MONTH", category(conn, "Groceries"))

    rows = drill.rows_for(conn, "net", "2026-07")
    assert total(rows) == 497000
    assert insights.monthly_net(conn, "2026-07", 1)[0]["net"] == 497000
    # biggest first, whichever way it points
    assert rows[0]["description"] == "PAY"


def test_the_panel_says_which_month_it_is_showing(signed_in):
    conn = open_db()
    groceries = category(conn, "Groceries")
    add_txn(conn, 1, "2026-07-02", -3000, "JULY MARKET", groceries)
    conn.close()

    away = signed_in.get("/insights/drill?kind=category&key=Groceries"
                         "&month=2026-07&page_month=2026-08")
    assert "JULY MARKET" in away.text
    assert "July 2026" in away.text
    assert "not the month" in away.text                 # said plainly
    assert 'href="/insights?month=2026-07"' in away.text   # and one tap away

    same = signed_in.get("/insights/drill?kind=category&key=Groceries"
                         "&month=2026-07&page_month=2026-07")
    assert "July 2026" in same.text
    assert "not the month" not in same.text


def test_a_subscription_is_not_labelled_with_one_month(signed_in):
    """It is deliberately shown across every month it appears in."""
    conn = open_db()
    subs = category(conn, "Subscriptions & Streaming")
    for month in ("2026-06", "2026-07", "2026-08"):
        add_txn(conn, 1, f"{month}-04", -1599, "NETFLIX.COM", subs)
    conn.close()

    r = signed_in.get("/insights/drill?kind=recurring&key=NETFLIX.COM"
                      "&month=2026-08&page_month=2026-08")
    assert r.text.count("NETFLIX") >= 3
    assert "August 2026" not in r.text


# --- re-filing from inside the panel ------------------------------------------

def test_a_row_can_be_recategorized_without_leaving_the_page(signed_in):
    conn = open_db()
    txn = add_txn(conn, 1, "2026-07-02", -3000, "MYSTERY SHOP")
    fuel = category(conn, "Fuel")
    conn.close()

    panel = signed_in.get("/insights/drill?kind=uncategorized&month=2026-07")
    assert 'class="cat-chip"' in panel.text
    assert "uncategorized" in panel.text

    r = signed_in.post("/insights/drill/categorize", data={
        "csrf": get_csrf_attr(panel.text), "txn_id": str(txn),
        "category_id": str(fuel)})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "category": "Fuel"}

    conn = open_db()
    row = conn.execute("SELECT category_id, classified_by, needs_review FROM "
                       "transactions WHERE id = ?", (txn,)).fetchone()
    conn.close()
    assert row["category_id"] == fuel
    assert row["classified_by"] == "user"      # a hand-filed row, not a guess
    assert row["needs_review"] == 0


def test_clearing_a_category_from_the_panel_is_allowed(signed_in):
    conn = open_db()
    txn = add_txn(conn, 1, "2026-07-02", -3000, "WRONG", category(conn, "Fuel"))
    conn.close()

    panel = signed_in.get("/insights/drill?kind=category&key=Fuel&month=2026-07")
    r = signed_in.post("/insights/drill/categorize", data={
        "csrf": get_csrf_attr(panel.text), "txn_id": str(txn), "category_id": ""})
    assert r.json() == {"ok": True, "category": ""}


def test_a_split_is_never_collapsed_by_a_select_in_a_panel(signed_in):
    """Choosing one category on the edit page deliberately replaces a split.
    From a chip inside a panel there is nothing to warn you, so it is refused
    and the panel offers the split editor instead."""
    conn = open_db()
    groceries, clothes = category(conn, "Groceries"), category(conn, "Clothing")
    txn = add_txn(conn, 1, "2026-07-02", -10000, "COSTCO RUN", groceries)
    for cat, cents in ((groceries, -6000), (clothes, -4000)):
        conn.execute("INSERT INTO transaction_splits (transaction_id, category_id, "
                     "amount_cents) VALUES (?, ?, ?)", (txn, cat, cents))
    conn.commit()
    conn.close()

    panel = signed_in.get("/insights/drill?kind=category&key=Groceries&month=2026-07")
    assert "part of a split" in panel.text
    assert f"/transactions/{txn}/split" in panel.text
    assert 'data-txn="%d"' % txn not in panel.text        # no chip to change it

    r = signed_in.post("/insights/drill/categorize", data={
        "csrf": get_csrf_attr(panel.text), "txn_id": str(txn),
        "category_id": str(clothes)})
    assert r.status_code == 409

    conn = open_db()
    parts = conn.execute("SELECT COUNT(*) FROM transaction_splits WHERE "
                         "transaction_id = ?", (txn,)).fetchone()[0]
    conn.close()
    assert parts == 2                                     # still split


def test_recategorizing_needs_a_valid_token(signed_in):
    conn = open_db()
    txn = add_txn(conn, 1, "2026-07-02", -3000, "MYSTERY")
    fuel = category(conn, "Fuel")
    conn.close()

    r = signed_in.post("/insights/drill/categorize", data={
        "csrf": "not-the-token", "txn_id": str(txn), "category_id": str(fuel)})
    assert r.status_code == 403


def test_a_part_month_figure_opens_only_that_part_of_the_month(conn):
    """Regression: "Spent by day 13" opened the whole month, so a panel hung
    off a $243 figure listed $1,785 of charges."""
    from datetime import date
    account(conn)
    groceries = category(conn, "Groceries")
    add_txn(conn, 1, "2026-08-02", -3000, "EARLY", groceries)
    add_txn(conn, 1, "2026-08-11", -4500, "ALSO EARLY", groceries)
    add_txn(conn, 1, "2026-08-21", -145000, "LATER", groceries)

    pace = insights.spending_pace(conn, "2026-08", today=date(2026, 8, 13))
    rows = drill.rows_for(conn, "spending", "2026-08", "", pace["elapsed"])
    assert -total(rows) == pace["spent"] == 7500
    assert "LATER" not in [r["description"] for r in rows]
    # and with no day given it is still the whole month
    assert len(drill.rows_for(conn, "spending", "2026-08")) == 3


def test_last_months_same_point_stops_at_the_same_day(conn):
    account(conn)
    groceries = category(conn, "Groceries")
    add_txn(conn, 1, "2026-07-05", -2000, "BEFORE", groceries)
    add_txn(conn, 1, "2026-07-30", -50000, "AFTER", groceries)

    from datetime import date
    pace = insights.spending_pace(conn, "2026-08", today=date(2026, 8, 13))
    rows = drill.rows_for(conn, "spending", "2026-07", "", pace["elapsed"])
    assert -total(rows) == pace["prev_same_day"] == 2000


def test_the_panel_says_where_a_part_month_stops(signed_in):
    conn = open_db()
    add_txn(conn, 1, "2026-08-02", -3000, "EARLY", category(conn, "Groceries"))
    conn.close()

    r = signed_in.get("/insights/drill?kind=spending&month=2026-08&day=13"
                      "&page_month=2026-08")
    assert "August 2026, to the 13th" in r.text
    for day, word in ((1, "1st"), (2, "2nd"), (3, "3rd"), (11, "11th"),
                      (12, "12th"), (21, "21st"), (22, "22nd")):
        got = signed_in.get(f"/insights/drill?kind=spending&month=2026-08&day={day}")
        assert f"to the {word}" in got.text


def test_the_pace_rows_carry_the_day_they_stop_at(signed_in):
    conn = open_db()
    add_txn(conn, 1, "2026-08-02", -3000, "EARLY", category(conn, "Groceries"))
    conn.close()

    r = signed_in.get("/insights?month=2026-08")
    assert "data-drill-day=" in r.text
    # last month's row opens last month, not this one
    assert 'data-drill-kind="spending" data-drill-month="2026-07"' in r.text


def test_the_link_out_is_labelled_with_what_it_actually_opens(signed_in):
    """The panel may be cut off partway through the month; the link isn't."""
    conn = open_db()
    add_txn(conn, 1, "2026-07-02", -3000, "EARLY", category(conn, "Groceries"))
    conn.close()

    r = signed_in.get("/insights/drill?kind=spending&month=2026-07&day=13"
                      "&page_month=2026-08")
    assert "July 2026, to the 13th —" in r.text          # what you are looking at
    assert ">Open July 2026</a>" in r.text               # what the link gives you


# --- the roll-up opens too -----------------------------------------------------

def nine_categories(conn):
    account(conn)
    names = ["Groceries", "Fuel", "Clothing", "Restaurants & Takeout", "Pharmacy",
             "Home Maintenance", "Gifts & Donations", "Hobbies", "Pets"]
    for i, name in enumerate(names):
        add_txn(conn, 1, "2026-08-02", -(9 - i) * 1000, f"SHOP {name}",
                category(conn, name))
    return names


def test_other_opens_into_the_categories_rolled_up_inside_it(conn):
    """Regression: "Other" was the one segment you couldn't look inside, which
    made it the one figure you couldn't account for."""
    names = nine_categories(conn)
    composition = insights.category_composition(conn, "2026-08", 1)
    other = next(s for s in composition["series"] if s["name"] == "Other")

    rows = drill.rows_for(conn, "other", "2026-08", "2026-08")
    assert -total(rows) == other["monthly"][-1]
    # the three smallest, which are exactly the ones outside the top six
    assert sorted(r["category"] for r in rows) == sorted(names[6:])


def test_other_is_ranked_by_the_chart_not_by_the_month_being_opened(conn):
    """The roll-up is decided across the whole chart, so opening July's bar has
    to use the chart's ranking, not July's own."""
    account(conn)
    groceries, pets = category(conn, "Groceries"), category(conn, "Pets")
    for m in ("2026-07", "2026-08"):
        add_txn(conn, 1, f"{m}-02", -90000, "BIG", groceries)
        add_txn(conn, 1, f"{m}-03", -1000, "SMALL", pets)
    for name in ("Fuel", "Clothing", "Pharmacy", "Hobbies", "Education"):
        add_txn(conn, 1, "2026-08-04", -50000, f"SHOP {name}", category(conn, name))

    rows = drill.rows_for(conn, "other", "2026-07", "2026-08")
    assert [r["description"] for r in rows] == ["SMALL"]     # Pets fell outside


def test_the_stacked_chart_marks_other_as_openable(conn):
    from app.services import charts
    nine_categories(conn)
    svg = charts.stacked_chart(insights.category_composition(conn, "2026-08", 3))
    assert 'data-drill-kind="other"' in svg
    # keyed by the month the chart is anchored at, which decides the ranking
    assert 'data-drill-kind="other" data-drill-month="2026-08" role="button" ' \
           'tabindex="0" aria-expanded="false" data-drill-key="2026-08"' in svg


def test_the_other_panel_says_what_it_is(signed_in):
    conn = open_db()
    for i, name in enumerate(["Groceries", "Fuel", "Clothing",
                              "Restaurants & Takeout", "Pharmacy",
                              "Home Maintenance", "Gifts & Donations"]):
        add_txn(conn, 1, "2026-08-02", -(9 - i) * 1000, f"SHOP {name}",
                category(conn, name))
    conn.close()

    r = signed_in.get("/insights/drill?kind=other&key=2026-08&month=2026-08"
                      "&page_month=2026-08")
    assert "Other" in r.text
    assert "outside the biggest categories" in r.text
    assert "SHOP Gifts &amp; Donations" in r.text     # escaped, and it is the row


def test_the_legend_entry_for_other_opens_it_as_well(signed_in):
    conn = open_db()
    for i, name in enumerate(["Groceries", "Fuel", "Clothing",
                              "Restaurants & Takeout", "Pharmacy",
                              "Home Maintenance", "Gifts & Donations"]):
        add_txn(conn, 1, "2026-08-02", -(9 - i) * 1000, f"SHOP {name}",
                category(conn, name))
    conn.close()

    r = signed_in.get("/insights?month=2026-08")
    assert 'data-drill-kind="other"' in r.text
