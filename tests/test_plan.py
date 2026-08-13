"""Telling the app when money moves, instead of letting it guess.

Detection from statements is a good guess and never a certainty, and every
wrong guess shows up as a category swinging for no reason: pay every other week
becomes "sometimes three paydays", a quarterly bill is invisible until it has
been paid three times. A schedule you declare replaces the guess.
"""
from datetime import date

import pytest

from app import config, db
from app.services import budgets, insights, schedules

from test_bulk_and_pairing import add_txn, category, get_csrf, open_db  # noqa: F401


@pytest.fixture()
def signed_in(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    return web


def declare(conn, name, cadence, cents, anchor, note=""):
    schedules.set_schedule(conn, category(conn, name), cadence, cents, anchor, note)
    conn.commit()
    return schedules.get(conn, category(conn, name))


# --- when a schedule says something lands -------------------------------------

def test_a_fortnightly_schedule_lands_two_or_three_times(conn):
    s = declare(conn, "Salary", "biweekly", 210000, "2026-08-07")
    assert s.dates_in("2026-08") == [date(2026, 8, 7), date(2026, 8, 21)]
    assert s.dates_in("2026-10") == [date(2026, 10, 2), date(2026, 10, 16),
                                     date(2026, 10, 30)]
    # and backwards, without drifting off the fortnight
    assert s.dates_in("2026-07") == [date(2026, 7, 10), date(2026, 7, 24)]


def test_a_quarterly_schedule_skips_the_months_between(conn):
    s = declare(conn, "Water", "quarterly", 18500, "2026-08-14")
    assert s.dates_in("2026-08") == [date(2026, 8, 14)]
    assert s.dates_in("2026-09") == []
    assert s.dates_in("2026-10") == []
    assert s.dates_in("2026-11") == [date(2026, 11, 14)]
    assert s.dates_in("2026-05") == [date(2026, 5, 14)]     # and backwards


def test_a_monthly_schedule_clamps_to_a_short_month(conn):
    s = declare(conn, "Rent / Mortgage", "monthly", 145000, "2026-01-31")
    assert s.dates_in("2026-02") == [date(2026, 2, 28)]
    assert s.dates_in("2026-03") == [date(2026, 3, 31)]


def test_the_monthly_figure_is_the_bill_spread_over_its_cycle(conn):
    assert declare(conn, "Water", "quarterly", 18500, "2026-08-14").monthly_cents == 6167
    assert declare(conn, "Car Insurance", "semiannual", 74000,
                   "2026-02-01").monthly_cents == 12333
    # and pay every two weeks is 26 of them a year, not 24
    assert declare(conn, "Salary", "biweekly", 210000,
                   "2026-08-07").monthly_cents == 455000


def test_the_next_occurrence_can_be_a_year_out(conn):
    s = declare(conn, "Property Tax", "yearly", 320000, "2026-03-15")
    assert s.next_after(date(2026, 8, 13)) == date(2027, 3, 15)


def test_only_a_cadence_longer_than_a_month_needs_setting_aside_for(conn):
    assert declare(conn, "Water", "quarterly", 100, "2026-08-14").is_periodic
    assert not declare(conn, "Groceries", "monthly", 100, "2026-08-14").is_periodic


# --- a declaration beats a guess ----------------------------------------------

def test_a_declared_bill_needs_no_statements_at_all(conn):
    """The point of saying so: a quarterly bill is quarterly from the first
    statement, not from the third."""
    declare(conn, "Water", "quarterly", 18500, "2026-08-14")
    bill = next(b for b in insights.periodic_bills(conn, "2026-08",
                                                  today=date(2026, 9, 1))
                if b.category == "Water")
    assert bill.declared is True
    assert bill.monthly_cents == 6167
    assert bill.last_date == date(2026, 8, 14)
    assert bill.next_due == date(2026, 11, 14)


def test_declaring_a_category_stops_it_being_guessed_at(conn):
    """Otherwise saying "Water is quarterly" leaves the app still arguing about
    the water company."""
    conn.execute("INSERT OR IGNORE INTO accounts (id, name, type, created_at) "
                 "VALUES (1, 'a', 'checking', 'now')")
    from datetime import timedelta
    day = date(2025, 11, 14)
    for _ in range(4):
        add_txn(conn, 1, day.isoformat(), -18500, "CITY WATER DEPT",
                category(conn, "Water"))
        day += timedelta(days=91)

    assert len([b for b in insights.periodic_bills(conn, "2026-08")
                if b.category == "Water"]) == 1        # the detected one
    declare(conn, "Water", "quarterly", 20000, "2026-08-14")
    bills = [b for b in insights.periodic_bills(conn, "2026-08")
             if b.category == "Water"]
    assert len(bills) == 1                             # not two
    assert bills[0].declared and bills[0].typical_cents == 20000


def test_a_declared_bill_is_never_judged_month_by_month(conn):
    """However small it looks against the category, you said it is quarterly."""
    conn.execute("INSERT OR IGNORE INTO accounts (id, name, type, created_at) "
                 "VALUES (1, 'a', 'checking', 'now')")
    declare(conn, "Pharmacy", "quarterly", 4600, "2026-08-14")
    for m in ("2026-03", "2026-04", "2026-05", "2026-06", "2026-07"):
        add_txn(conn, 1, f"{m}-08", -9000, "CVS", category(conn, "Pharmacy"))

    note = next(n for n in insights.budget_review(conn, "2026-08")
                if n.category == "Pharmacy")
    assert note.kind == "periodic"


def test_declared_income_replaces_the_detected_stream(conn):
    """Cadence read off a statement is a guess, and guessing wrong is exactly
    the third-payday-that-isn't problem."""
    conn.execute("INSERT OR IGNORE INTO accounts (id, name, type, created_at) "
                 "VALUES (1, 'a', 'checking', 'now')")
    from datetime import timedelta
    day = date(2026, 5, 1)
    for _ in range(8):
        add_txn(conn, 1, day.isoformat(), 200000, "ACME PAYROLL",
                category(conn, "Salary"))
        day += timedelta(days=14)

    declare(conn, "Salary", "biweekly", 210000, "2026-08-07")
    got = insights.expected_income(conn, "2026-08")
    assert got["total"] == 420000                  # two paydays at what you said
    assert len(got["detail"]) == 1                 # not the detected one as well
    assert got["detail"][0]["paydays"] == [date(2026, 8, 7), date(2026, 8, 21)]

    three = insights.expected_income(conn, "2026-10")
    assert three["total"] == 630000                # October catches a third


# --- acting on a suggestion ---------------------------------------------------

def amounts(month):
    conn = open_db()
    got = {conn.execute("SELECT name FROM categories WHERE id = ?",
                        (cat,)).fetchone()["name"]: cents
           for cat, cents in budgets.get_budgets(conn, month).items()}
    conn.close()
    return got


def test_a_suggestion_can_be_applied_without_retyping_it(signed_in):
    conn = open_db()
    for m in ("2026-03", "2026-04", "2026-05", "2026-06", "2026-07"):
        add_txn(conn, 1, f"{m}-08", -12000, "SHELL", category(conn, "Fuel"))
    fuel = category(conn, "Fuel")
    conn.close()

    page = signed_in.get("/insights?month=2026-08")
    assert "Budget $120.00" in page.text            # the button, not just a figure

    r = signed_in.post("/budgets/apply", data={
        "csrf": get_csrf(page.text), "category_id": str(fuel),
        "month": "2026-08", "amount": "120.00", "scope": "month",
        "back": "/insights?month=2026-08"}, follow_redirects=True)
    assert amounts("2026-08") == {"Fuel": 12000}
    assert "Budget saved for" in r.text and "Fuel" in r.text


def test_applying_onward_lets_later_months_follow_it(signed_in):
    """Carry-forward already means an unset month follows the last one set, so
    applying forward clears the later figures that would override it."""
    conn = open_db()
    fuel = category(conn, "Fuel")
    budgets.set_budget(conn, fuel, "2026-08", 4000)
    budgets.set_budget(conn, fuel, "2026-10", 4000)     # a later month's own
    conn.commit()
    conn.close()

    page = signed_in.get("/budgets?month=2026-08")
    signed_in.post("/budgets/apply", data={
        "csrf": get_csrf(page.text), "category_id": str(fuel),
        "month": "2026-08", "amount": "120.00", "scope": "onward"})

    assert amounts("2026-08") == {"Fuel": 12000}
    assert amounts("2026-10") == {}                     # inherits now
    conn = open_db()
    assert budgets.effective_budgets(conn, "2026-10")[0][fuel] == 12000
    conn.close()


def test_applying_to_one_month_leaves_the_others_alone(signed_in):
    conn = open_db()
    fuel = category(conn, "Fuel")
    budgets.set_budget(conn, fuel, "2026-08", 4000)
    budgets.set_budget(conn, fuel, "2026-10", 4000)
    conn.commit()
    conn.close()

    page = signed_in.get("/budgets?month=2026-08")
    signed_in.post("/budgets/apply", data={
        "csrf": get_csrf(page.text), "category_id": str(fuel),
        "month": "2026-08", "amount": "120.00", "scope": "month"})

    assert amounts("2026-08") == {"Fuel": 12000}
    assert amounts("2026-10") == {"Fuel": 4000}         # untouched


def test_saving_the_whole_budget_can_go_forward_too(signed_in):
    conn = open_db()
    groceries = category(conn, "Groceries")
    budgets.set_budget(conn, groceries, "2026-11", 9900)
    conn.commit()
    conn.close()

    page = signed_in.get("/budgets?month=2026-08")
    assert "later months follow this" in page.text
    signed_in.post("/budgets/save", data={
        "csrf": get_csrf(page.text), "month": "2026-08", "scope": "onward",
        f"cat_{groceries}": "300"})

    assert amounts("2026-08") == {"Groceries": 30000}
    assert amounts("2026-11") == {}


def test_saving_for_one_month_is_still_possible(signed_in):
    conn = open_db()
    groceries = category(conn, "Groceries")
    budgets.set_budget(conn, groceries, "2026-11", 9900)
    conn.commit()
    conn.close()

    page = signed_in.get("/budgets?month=2026-08")
    signed_in.post("/budgets/save", data={
        "csrf": get_csrf(page.text), "month": "2026-08", "scope": "month",
        f"cat_{groceries}": "300"})
    assert amounts("2026-11") == {"Groceries": 9900}


# --- the plan page ------------------------------------------------------------

def test_the_plan_page_offers_what_it_found_for_confirming(signed_in):
    conn = open_db()
    from datetime import timedelta
    day = date(2025, 11, 14)
    for _ in range(4):
        add_txn(conn, 1, day.isoformat(), -18500, "CITY WATER DEPT",
                category(conn, "Water"))
        day += timedelta(days=91)
    conn.close()

    r = signed_in.get("/plan")
    assert "Found in your statements" in r.text
    assert "Water" in r.text
    assert "Confirm" in r.text


def test_confirming_a_suggestion_makes_it_a_schedule(signed_in):
    conn = open_db()
    water = category(conn, "Water")
    conn.close()

    page = signed_in.get("/plan")
    signed_in.post("/plan/save", data={
        "csrf": get_csrf(page.text), "category_id": str(water),
        "cadence": "quarterly", "amount": "185.00",
        "anchor_date": "2026-08-14", "note": "city water"})

    conn = open_db()
    s = schedules.get(conn, water)
    conn.close()
    assert (s.cadence, s.amount_cents, s.note) == ("quarterly", 18500, "city water")

    r = signed_in.get("/plan")
    assert "What you've told it" in r.text
    assert "every 3 months" in r.text


def test_a_schedule_can_be_removed_and_the_guessing_comes_back(signed_in):
    conn = open_db()
    water = category(conn, "Water")
    schedules.set_schedule(conn, water, "quarterly", 18500, "2026-08-14")
    conn.commit()
    conn.close()

    page = signed_in.get(f"/plan?category={water}")
    assert "Remove this schedule" in page.text
    signed_in.post("/plan/remove", data={
        "csrf": get_csrf(page.text), "category_id": str(water)})

    conn = open_db()
    assert schedules.get(conn, water) is None
    conn.close()


def test_a_nonsense_cadence_is_refused(signed_in):
    conn = open_db()
    water = category(conn, "Water")
    conn.close()
    page = signed_in.get("/plan")
    signed_in.post("/plan/save", data={
        "csrf": get_csrf(page.text), "category_id": str(water),
        "cadence": "whenever", "amount": "10", "anchor_date": "2026-08-14"})

    conn = open_db()
    assert schedules.get(conn, water) is None
    conn.close()


def test_the_plan_needs_a_login(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    web.post("/logout")
    r = web.get("/plan", follow_redirects=False)
    assert r.status_code == 303


def test_the_budget_page_links_a_category_to_its_schedule(signed_in):
    r = signed_in.get("/budgets?month=2026-08")
    assert "not every month?" in r.text
    assert "/plan?category=" in r.text
