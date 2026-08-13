"""Is the budget right, and what about the bills that don't come every month?

A monthly budget quietly assumes every bill is monthly. Water every quarter and
insurance twice a year break that assumption in both directions: the months
they skip look under budget, and the month one lands looks like overspending.
Judging those on a monthly figure produces a warning that is wrong four times a
year, which is how a page stops being read.
"""
from datetime import date, timedelta

import pytest

from app.services import budgets, insights

from test_bulk_and_pairing import add_txn, category, get_csrf, open_db  # noqa: F401


@pytest.fixture()
def signed_in(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    return web


def account(conn, acc_id=1):
    conn.execute("INSERT OR IGNORE INTO accounts (id, name, type, created_at) "
                 "VALUES (?, 'Checking', 'checking', 'now')", (acc_id,))
    conn.commit()


def every(conn, start: str, days: int, times: int, cents: int, desc: str,
          category_name: str):
    """A charge repeating on a fixed interval, the way a real bill does."""
    day = date.fromisoformat(start)
    for _ in range(times):
        add_txn(conn, 1, day.isoformat(), -cents, desc, category(conn, category_name))
        day += timedelta(days=days)


def monthly(conn, months: list[str], cents: int, desc: str, category_name: str):
    for m in months:
        add_txn(conn, 1, f"{m}-08", -cents, desc, category(conn, category_name))


# --- spotting a bill that isn't monthly ---------------------------------------

def test_a_quarterly_bill_is_recognised_as_quarterly(conn):
    account(conn)
    every(conn, "2025-11-14", 91, 4, 18500, "CITY WATER DEPT", "Water")

    bill = next(b for b in insights.periodic_bills(conn, "2026-08")
                if b.merchant.startswith("CITY WATER"))
    assert bill.cadence == "quarterly"
    assert bill.months_per == 3
    assert bill.typical_cents == 18500
    assert bill.monthly_cents == 6167          # what to put aside each month
    assert bill.last_date == date(2026, 8, 14)
    assert bill.next_due == date(2026, 11, 13)  # 91 days on from the last one


def test_a_twice_yearly_bill_needs_only_two_sightings(conn):
    """A yearly bill produces its second data point a year in. Demanding three
    would mean never recognising one at all."""
    account(conn)
    every(conn, "2026-02-01", 182, 2, 74000, "GEICO AUTO INSURANCE", "Car Insurance")

    bill = next(b for b in insights.periodic_bills(conn, "2026-08")
                if "GEICO" in b.merchant)
    assert bill.cadence == "every 6 months"
    assert bill.monthly_cents == 12333
    assert bill.times_seen == 2


def test_a_monthly_bill_is_not_listed_among_them(conn):
    """Netflix every month is a subscription, already reported elsewhere."""
    account(conn)
    monthly(conn, [f"2026-{m:02d}" for m in range(2, 9)], 1599, "NETFLIX.COM",
            "Subscriptions & Streaming")
    assert not [b for b in insights.periodic_bills(conn, "2026-08")
                if "NETFLIX" in b.merchant]


def test_irregular_spending_is_not_mistaken_for_a_bill(conn):
    account(conn)
    for day, cents in (("2026-02-03", 4000), ("2026-03-19", 12000),
                       ("2026-07-27", 800), ("2026-08-02", 25000)):
        add_txn(conn, 1, day, -cents, "HOME DEPOT #2431",
                category(conn, "Home Maintenance"))
    assert not [b for b in insights.periodic_bills(conn, "2026-08")
                if "HOME DEPOT" in b.merchant]


def test_a_bill_paid_in_two_lines_is_still_one_bill(conn):
    """Two rows the same day would otherwise halve the apparent interval and
    turn a quarterly bill into something it isn't."""
    account(conn)
    day = date(2025, 11, 14)
    for _ in range(4):
        add_txn(conn, 1, day.isoformat(), -10000, "CITY WATER DEPT",
                category(conn, "Water"))
        add_txn(conn, 1, day.isoformat(), -8500, "CITY WATER DEPT",
                category(conn, "Water"))
        day += timedelta(days=91)

    bill = next(b for b in insights.periodic_bills(conn, "2026-08")
                if b.merchant.startswith("CITY WATER"))
    assert bill.cadence == "quarterly"
    assert bill.typical_cents == 18500          # the two summed, not each alone


def test_the_month_a_bill_falls_due_is_flagged(conn):
    account(conn)
    # Last paid in May, so the next one is due in August — the month being shown.
    every(conn, "2025-11-14", 91, 3, 18500, "CITY WATER DEPT", "Water")
    bill = next(b for b in insights.periodic_bills(conn, "2026-08")
                if b.merchant.startswith("CITY WATER"))
    assert bill.due_in("2026-08") is True
    assert bill.due_in("2026-09") is False


# --- what the budget needs from you -------------------------------------------

def notes_by_category(conn, month="2026-08"):
    return {n.category: n for n in insights.budget_review(conn, month)}


def test_a_category_over_its_budget_is_reported_with_the_overspend(conn):
    account(conn)
    budgets.set_budget(conn, category(conn, "Groceries"), "2026-08", 30000)
    conn.commit()
    add_txn(conn, 1, "2026-08-02", -42000, "MARKET", category(conn, "Groceries"))

    note = notes_by_category(conn)["Groceries"]
    assert note.kind == "over"
    assert (note.budget, note.actual, note.over_by) == (30000, 42000, 12000)


def test_a_budget_that_never_matches_reality_is_reported_as_too_low(conn):
    """Not one bad month — a figure that has been wrong every month, which is
    the budget's fault rather than the spending's."""
    account(conn)
    budgets.set_budget(conn, category(conn, "Fuel"), "2026-08", 8000)
    conn.commit()
    monthly(conn, ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07"],
            12000, "SHELL OIL 574", "Fuel")

    note = notes_by_category(conn)["Fuel"]
    assert note.kind == "raise"
    assert note.typical == 12000
    assert note.suggested == 12000
    assert note.change == 4000              # what raising it would cost


def test_money_left_sitting_in_a_budget_is_reported_too(conn):
    account(conn)
    budgets.set_budget(conn, category(conn, "Hobbies"), "2026-08", 30000)
    conn.commit()
    monthly(conn, ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07"],
            5000, "HOBBY SHOP", "Hobbies")

    note = notes_by_category(conn)["Hobbies"]
    assert note.kind == "lower"
    assert note.suggested == 5000


def test_regular_spending_with_no_budget_at_all_is_reported(conn):
    account(conn)
    monthly(conn, ["2026-04", "2026-05", "2026-06", "2026-07"], 4500,
            "CVS PHARMACY", "Pharmacy")

    note = notes_by_category(conn)["Pharmacy"]
    assert note.kind == "unbudgeted"
    assert note.suggested == 4500
    assert note.budget == 0


def test_one_off_spending_does_not_demand_a_budget(conn):
    """Two months out of six is not a habit worth budgeting for."""
    account(conn)
    monthly(conn, ["2026-05", "2026-07"], 4500, "SOME SHOP", "Shopping")
    assert "Shopping" not in notes_by_category(conn)


def test_a_carried_forward_budget_is_what_gets_judged(conn):
    """The month has no budget rows of its own, but it is being measured
    against one — so that is the figure to compare against."""
    account(conn)
    budgets.set_budget(conn, category(conn, "Groceries"), "2026-06", 30000)
    conn.commit()
    add_txn(conn, 1, "2026-08-02", -42000, "MARKET", category(conn, "Groceries"))

    assert budgets.get_budgets(conn, "2026-08") == {}
    note = notes_by_category(conn)["Groceries"]
    assert (note.kind, note.budget) == ("over", 30000)


def test_a_quarterly_bill_is_not_called_overspending(conn):
    """The whole point: Water lands once a quarter, so in the month it lands it
    is three months' worth. Reporting that as going over budget is a warning
    that fires four times a year and means nothing."""
    account(conn)
    every(conn, "2025-11-14", 91, 4, 18500, "CITY WATER DEPT", "Water")
    budgets.set_budget(conn, category(conn, "Water"), "2026-08", 6000)
    conn.commit()

    note = notes_by_category(conn)["Water"]
    assert note.kind == "periodic"           # not "over", though 18500 > 6000
    assert note.suggested == 6167            # set aside this much per month
    assert note.bill.cadence == "quarterly"


def test_a_quarterly_bill_is_not_called_underspent_either(conn):
    """Two months out of three it costs nothing, which is not money to spare."""
    account(conn)
    every(conn, "2025-11-14", 91, 4, 18500, "CITY WATER DEPT", "Water")
    budgets.set_budget(conn, category(conn, "Water"), "2026-09", 6200)
    conn.commit()

    note = notes_by_category(conn, "2026-09")["Water"]
    assert note.kind == "periodic"


def test_the_most_urgent_thing_is_listed_first(conn):
    account(conn)
    budgets.set_budget(conn, category(conn, "Groceries"), "2026-08", 30000)
    budgets.set_budget(conn, category(conn, "Hobbies"), "2026-08", 30000)
    conn.commit()
    add_txn(conn, 1, "2026-08-02", -42000, "MARKET", category(conn, "Groceries"))
    monthly(conn, ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07"],
            5000, "HOBBY SHOP", "Hobbies")
    monthly(conn, ["2026-04", "2026-05", "2026-06", "2026-07"], 4500,
            "CVS PHARMACY", "Pharmacy")

    kinds = [n.kind for n in insights.budget_review(conn, "2026-08")]
    assert kinds.index("over") < kinds.index("unbudgeted") < kinds.index("lower")


def test_a_category_you_neither_budget_nor_spend_on_is_not_mentioned(conn):
    account(conn)
    add_txn(conn, 1, "2026-08-02", -42000, "MARKET", category(conn, "Groceries"))
    assert "Pets" not in notes_by_category(conn)


# --- on the page --------------------------------------------------------------

def test_the_insights_page_shows_the_budget_check(signed_in):
    conn = open_db()
    budgets.set_budget(conn, category(conn, "Groceries"), "2026-08", 30000)
    conn.commit()
    add_txn(conn, 1, "2026-08-02", -42000, "MARKET", category(conn, "Groceries"))
    conn.close()

    r = signed_in.get("/insights?month=2026-08")
    assert "Budget check" in r.text
    assert "$420.00 spent of" in r.text
    assert "+$120.00" in r.text
    assert 'href="/budgets?month=2026-08"' in r.text


def test_the_insights_page_lists_the_bills_that_are_not_monthly(signed_in):
    conn = open_db()
    day = date(2025, 11, 14)
    for _ in range(3):        # last paid in May, next due in August
        add_txn(conn, 1, day.isoformat(), -18500, "CITY WATER DEPT",
                category(conn, "Water"))
        day += timedelta(days=91)
    conn.close()

    r = signed_in.get("/insights?month=2026-08")
    assert "Bills that don't come every month" in r.text
    assert "quarterly" in r.text
    assert "$61.67" in r.text                 # the monthly set-aside
    assert "due this month" in r.text


def test_the_budget_page_says_which_categories_are_not_monthly(signed_in):
    """This is where you act on it — the figure you type is right there."""
    conn = open_db()
    day = date(2025, 11, 14)
    for _ in range(4):
        add_txn(conn, 1, day.isoformat(), -18500, "CITY WATER DEPT",
                category(conn, "Water"))
        day += timedelta(days=91)
    conn.close()

    r = signed_in.get("/budgets?month=2026-08")
    assert "quarterly" in r.text
    assert "set aside" in r.text
    assert "$61.67" in r.text
