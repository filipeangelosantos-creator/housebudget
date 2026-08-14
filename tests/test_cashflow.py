"""What's in the account now, and whether it survives the month.

Statements say what moved and never what is there. Everything else here is
built from movements, which makes the app good at "you spent 179 on groceries"
and unable to answer "do we have enough until Friday". You supply the balance
once; the transactions carry it forward and the plan carries it onward.
"""
from datetime import date

import pytest

from app.services import cashflow, schedules

from test_bulk_and_pairing import add_txn, category, get_csrf, open_db  # noqa: F401


@pytest.fixture()
def signed_in(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    return web


def account(conn, acc_id, name, type_="checking"):
    conn.execute("INSERT OR IGNORE INTO accounts (id, name, type, created_at) "
                 "VALUES (?, ?, ?, 'now')", (acc_id, name, type_))
    conn.commit()
    return acc_id


def declare(conn, cat, cadence, cents, anchor, name=""):
    schedules.save(conn, category(conn, cat), cadence, cents, anchor, name=name)
    conn.commit()


# --- carrying a stated balance forward ----------------------------------------

def test_a_stated_balance_is_moved_on_by_what_landed_after_it(conn):
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, "2026-08-01", 107680)
    conn.commit()
    add_txn(conn, 1, "2026-08-04", -33895, "AMEX")
    add_txn(conn, 1, "2026-08-06", 207943, "PAYROLL")

    b = cashflow.balances(conn, today=date(2026, 8, 13))[0]
    assert b.stated_cents == 107680
    assert b.since_cents == -33895 + 207943
    assert b.current_cents == 281728


def test_the_day_you_state_is_already_included(conn):
    """A closing balance is the end of that day, so that day's rows are in it
    already — counting them again is a day's spending twice over."""
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, "2026-08-04", 100000)
    conn.commit()
    add_txn(conn, 1, "2026-08-04", -25000, "SAME DAY")

    assert cashflow.balances(conn, today=date(2026, 8, 13))[0].current_cents == 100000


def test_transfers_count_towards_a_balance_even_though_they_are_not_spending(conn):
    """Budgets ignore them, balances cannot: moving money to the card really
    does leave the current account."""
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, "2026-08-01", 100000)
    conn.commit()
    add_txn(conn, 1, "2026-08-05", -40000, "TRANSFER TO VISA",
            category(conn, "Credit Card Payment"))

    assert cashflow.balances(conn, today=date(2026, 8, 13))[0].current_cents == 60000


def test_a_future_dated_row_is_not_counted_as_already_happened(conn):
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, "2026-08-01", 100000)
    conn.commit()
    add_txn(conn, 1, "2026-08-30", -20000, "LATER")

    assert cashflow.balances(conn, today=date(2026, 8, 13))[0].current_cents == 100000


def test_an_account_you_have_not_valued_is_left_out_rather_than_called_empty(conn):
    account(conn, 1, "Citizens Checking")
    account(conn, 2, "Chase CC", "credit")
    cashflow.set_balance(conn, 1, "2026-08-01", 100000)
    conn.commit()

    total, known, missing = cashflow.on_hand(conn, today=date(2026, 8, 13))
    assert (total, known, missing) == (100000, 1, 1)


def test_a_card_you_owe_on_is_money_you_do_not_have(conn):
    account(conn, 1, "Citizens Checking")
    account(conn, 2, "Chase CC", "credit")
    cashflow.set_balance(conn, 1, "2026-08-01", 100000)
    cashflow.set_balance(conn, 2, "2026-08-01", -34167)
    conn.commit()

    assert cashflow.on_hand(conn, today=date(2026, 8, 13))[0] == 65833


def test_only_the_most_recent_statement_of_a_balance_counts(conn):
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, "2026-07-01", 500000)
    cashflow.set_balance(conn, 1, "2026-08-01", 107680)
    conn.commit()
    add_txn(conn, 1, "2026-07-15", -100000, "OLD")     # before the newer one

    b = cashflow.balances(conn, today=date(2026, 8, 13))[0]
    assert b.as_of == date(2026, 8, 1)
    assert b.current_cents == 107680                   # the old row is not re-applied


# --- the forecast -------------------------------------------------------------

def test_the_plan_carries_the_balance_forward(conn):
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, "2026-08-13", 100000)
    conn.commit()
    declare(conn, "Salary", "biweekly", 210000, "2026-08-21")
    declare(conn, "Rent / Mortgage", "monthly", 145000, "2026-09-01")

    f = cashflow.forecast(conn, days=30, today=date(2026, 8, 13))
    assert f.start_cents == 100000
    assert [(e.when, e.amount_cents) for e in f.events] == [
        (date(2026, 8, 21), 210000),
        (date(2026, 9, 1), -145000),
        (date(2026, 9, 4), 210000),
    ]
    assert f.end_cents == 100000 + 210000 - 145000 + 210000


def test_the_forecast_names_the_day_it_runs_out(conn):
    """The actionable figure is not the end balance, it is the low point — a
    month can finish fine and still bounce on the 3rd."""
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, "2026-08-13", 50000)
    conn.commit()
    declare(conn, "Rent / Mortgage", "monthly", 145000, "2026-09-01")
    declare(conn, "Salary", "monthly", 400000, "2026-09-05")

    f = cashflow.forecast(conn, days=30, today=date(2026, 8, 13))
    assert f.goes_negative is True
    assert f.low_cents == 50000 - 145000
    assert f.low_on == date(2026, 9, 1)
    assert f.end_cents > 0                     # and still ends the month up


def test_a_balance_that_holds_is_not_reported_as_a_shortfall(conn):
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, "2026-08-13", 500000)
    conn.commit()
    declare(conn, "Rent / Mortgage", "monthly", 145000, "2026-09-01")

    f = cashflow.forecast(conn, days=30, today=date(2026, 8, 13))
    assert f.goes_negative is False
    assert f.low_cents == 355000


def test_a_future_dated_import_is_in_the_forecast_as_a_fact(conn):
    """It has already cleared — that is not a projection, it is a date."""
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, "2026-08-13", 100000)
    conn.commit()
    add_txn(conn, 1, "2026-08-21", -20000, "SCHEDULED CARD PAYMENT")

    f = cashflow.forecast(conn, days=30, today=date(2026, 8, 13))
    assert [(e.label, e.kind) for e in f.events] == [
        ("SCHEDULED CARD PAYMENT", "imported")]
    assert f.end_cents == 80000


def test_nothing_is_projected_from_a_guess(conn):
    """Detection is good enough to describe your spending and much too loose to
    spend against. Only what you declared is forecast."""
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, "2026-08-13", 100000)
    conn.commit()
    for m in ("2026-04", "2026-05", "2026-06", "2026-07"):
        add_txn(conn, 1, f"{m}-04", -1599, "NETFLIX.COM",
                category(conn, "Subscriptions & Streaming"))

    assert cashflow.forecast(conn, days=60, today=date(2026, 8, 13)).events == []


def test_an_unvalued_account_contributes_nothing_to_the_forecast(conn):
    account(conn, 1, "Citizens Checking")
    account(conn, 2, "Chase CC", "credit")
    cashflow.set_balance(conn, 1, "2026-08-13", 100000)
    conn.commit()
    add_txn(conn, 2, "2026-08-20", -50000, "CARD SPEND")   # no balance for acct 2

    f = cashflow.forecast(conn, days=30, today=date(2026, 8, 13))
    assert f.events == []
    assert f.accounts_missing == 1


def test_two_salaries_both_show_up_in_the_forecast(conn):
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, "2026-08-13", 100000)
    conn.commit()
    declare(conn, "Salary", "biweekly", 210000, "2026-08-21", name="mine")
    declare(conn, "Salary", "monthly", 300000, "2026-08-25", name="theirs")

    f = cashflow.forecast(conn, days=20, today=date(2026, 8, 13))
    assert [e.label for e in f.events] == ["Salary — mine", "Salary — theirs"]


# --- the page -----------------------------------------------------------------

def test_the_page_takes_a_balance_and_remembers_it(signed_in):
    conn = open_db()
    account(conn, 1, "Citizens Checking")
    conn.close()

    page = signed_in.get("/cashflow")
    assert "What each account is worth" in page.text
    signed_in.post("/cashflow/balances", data={
        "csrf": get_csrf(page.text), "as_of": "2026-08-13", "bal_1": "1,076.80"})

    conn = open_db()
    b = cashflow.balances(conn, today=date(2026, 8, 13))[0]
    conn.close()
    assert b.stated_cents == 107680


def test_a_blank_box_forgets_the_account_rather_than_zeroing_it(signed_in):
    conn = open_db()
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, "2026-08-13", 107680)
    conn.commit()
    conn.close()

    page = signed_in.get("/cashflow")
    signed_in.post("/cashflow/balances", data={
        "csrf": get_csrf(page.text), "as_of": "2026-08-13", "bal_1": ""})

    conn = open_db()
    b = cashflow.balances(conn, today=date(2026, 8, 13))[0]
    conn.close()
    assert b.known is False
    assert b.current_cents == 0        # not counted, rather than counted as zero


def test_the_page_says_what_the_low_point_is(signed_in):
    conn = open_db()
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, date.today().isoformat(), 50000)
    conn.commit()
    from datetime import timedelta
    soon = (date.today() + timedelta(days=5)).isoformat()
    schedules.save(conn, category(conn, "Rent / Mortgage"), "monthly", 145000, soon)
    conn.commit()
    conn.close()

    r = signed_in.get("/cashflow")
    assert "Down to" in r.text
    assert "-$950.00" in r.text or "−$950.00" in r.text


def test_with_no_balances_the_page_asks_for_them_instead_of_forecasting(signed_in):
    conn = open_db()
    account(conn, 1, "Citizens Checking")
    conn.close()
    r = signed_in.get("/cashflow")
    assert "Lowest it gets" not in r.text
    assert "What each account is worth" in r.text


def test_the_horizon_can_be_changed(signed_in):
    conn = open_db()
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, date.today().isoformat(), 500000)
    conn.commit()
    conn.close()
    assert "In 30 days" in signed_in.get("/cashflow?days=30").text
    assert "In 90 days" in signed_in.get("/cashflow?days=999").text   # clamped


def test_cashflow_needs_a_login(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    web.post("/logout")
    assert web.get("/cashflow", follow_redirects=False).status_code == 303


def test_a_bill_already_taken_is_not_projected_on_top_of_itself(conn):
    """A schedule says it is coming; the import says it has gone. Counting
    both is the same money twice, and a forecast that overstates the outgoings
    is one nobody trusts twice."""
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, "2026-08-13", 300000)
    conn.commit()
    declare(conn, "Rent / Mortgage", "monthly", 145000, "2026-09-01")
    add_txn(conn, 1, "2026-09-02", -145000, "RENT PAYMENT",
            category(conn, "Rent / Mortgage"))       # posted a day early

    f = cashflow.forecast(conn, days=25, today=date(2026, 8, 13))
    assert [(e.label, e.kind) for e in f.events] == [("RENT PAYMENT", "imported")]
    assert f.end_cents == 300000 - 145000            # not 300000 - 290000


def test_a_later_occurrence_of_the_same_bill_is_still_projected(conn):
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, "2026-08-13", 900000)
    conn.commit()
    declare(conn, "Rent / Mortgage", "monthly", 145000, "2026-09-01")
    add_txn(conn, 1, "2026-09-01", -145000, "RENT PAYMENT",
            category(conn, "Rent / Mortgage"))

    f = cashflow.forecast(conn, days=60, today=date(2026, 8, 13))
    kinds = [(e.when, e.kind) for e in f.events]
    assert (date(2026, 9, 1), "imported") in kinds
    assert (date(2026, 10, 1), "scheduled") in kinds     # next month still comes


def test_a_different_category_landing_nearby_does_not_suppress_anything(conn):
    account(conn, 1, "Citizens Checking")
    cashflow.set_balance(conn, 1, "2026-08-13", 900000)
    conn.commit()
    declare(conn, "Rent / Mortgage", "monthly", 145000, "2026-09-01")
    add_txn(conn, 1, "2026-09-01", -9160, "SHELL OIL", category(conn, "Fuel"))

    f = cashflow.forecast(conn, days=25, today=date(2026, 8, 13))
    assert any(e.kind == "scheduled" for e in f.events)


# --- money that is already spoken for -----------------------------------------

from app.services import commitments  # noqa: E402


def test_a_card_to_clear_by_a_date_says_what_that_costs_a_month(conn):
    """The reported case: a card that has to be gone by Aug 2028, growing, while
    savings grows beside it looking like spare money."""
    account(conn, 1, "Savings", "savings")
    account(conn, 2, "Amex", "credit")
    cashflow.set_balance(conn, 1, "2026-08-13", 1200000)
    cashflow.set_balance(conn, 2, "2026-08-13", -1420000)
    commitments.save(conn, "Clear the Amex", "payoff", "2028-08-01", account_id=2)
    conn.commit()

    c = commitments.all_commitments(conn, today=date(2026, 8, 13))[0]
    assert c.outstanding_cents == 1420000
    assert c.months_left == 23                      # Aug 2026 to Aug 2028
    assert c.per_month_cents == 61739


def test_a_debt_is_not_taken_off_the_total_twice(conn):
    """A card you owe on is already a negative balance, so the household total
    has it. Subtracting the payoff again would double it."""
    account(conn, 1, "Savings", "savings")
    account(conn, 2, "Amex", "credit")
    cashflow.set_balance(conn, 1, "2026-08-13", 1200000)
    cashflow.set_balance(conn, 2, "2026-08-13", -1420000)
    commitments.save(conn, "Clear the Amex", "payoff", "2028-08-01", account_id=2)
    conn.commit()

    p = commitments.position(conn, today=date(2026, 8, 13))
    assert p.on_hand_cents == -220000               # savings minus the card
    assert p.committed_cents == 0                   # not counted a second time
    assert p.free_cents == -220000


def test_money_held_for_something_does_come_off_what_is_free(conn):
    """Nothing has subtracted a goal yet, so it genuinely reduces spare money."""
    account(conn, 1, "Savings", "savings")
    cashflow.set_balance(conn, 1, "2026-08-13", 1200000)
    commitments.save(conn, "New roof", "goal", "2027-06-01", target_cents=800000)
    conn.commit()

    p = commitments.position(conn, today=date(2026, 8, 13))
    assert (p.on_hand_cents, p.committed_cents, p.free_cents) == \
        (1200000, 800000, 400000)


def test_the_debt_figure_follows_the_account_rather_than_a_typed_number(conn):
    """A card you are still spending on owes more each month, and a payoff plan
    against a stale figure is fiction."""
    account(conn, 1, "Amex", "credit")
    cashflow.set_balance(conn, 1, "2026-08-01", -1420000)
    commitments.save(conn, "Clear the Amex", "payoff", "2028-08-01", account_id=1)
    conn.commit()
    add_txn(conn, 1, "2026-08-10", -50000, "MORE SPENDING ON IT")

    c = commitments.all_commitments(conn, today=date(2026, 8, 13))[0]
    assert c.outstanding_cents == 1470000
    assert c.per_month_cents > 61739               # and the monthly cost went up


def test_a_debt_already_cleared_asks_for_nothing(conn):
    account(conn, 1, "Amex", "credit")
    cashflow.set_balance(conn, 1, "2026-08-13", 0)
    commitments.save(conn, "Clear the Amex", "payoff", "2028-08-01", account_id=1)
    conn.commit()

    c = commitments.all_commitments(conn, today=date(2026, 8, 13))[0]
    assert c.outstanding_cents == 0
    assert c.per_month_cents == 0


def test_a_card_in_credit_is_not_a_debt(conn):
    """Overpay a card and it is money you have, not money you owe."""
    account(conn, 1, "Amex", "credit")
    cashflow.set_balance(conn, 1, "2026-08-13", 5000)
    commitments.save(conn, "Clear the Amex", "payoff", "2028-08-01", account_id=1)
    conn.commit()
    assert commitments.all_commitments(conn, today=date(2026, 8, 13))[0]\
        .outstanding_cents == 0


def test_a_deadline_this_month_asks_for_all_of_it(conn):
    """Zero months would divide by nothing; one month is the true answer."""
    account(conn, 1, "Amex", "credit")
    cashflow.set_balance(conn, 1, "2026-08-13", -100000)
    commitments.save(conn, "Clear the Amex", "payoff", "2026-08-20", account_id=1)
    conn.commit()

    c = commitments.all_commitments(conn, today=date(2026, 8, 13))[0]
    assert c.months_left == 1
    assert c.per_month_cents == 100000
    assert c.overdue is True


def test_everything_promised_adds_up_to_one_monthly_figure(conn):
    account(conn, 1, "Savings", "savings")
    account(conn, 2, "Amex", "credit")
    cashflow.set_balance(conn, 1, "2026-08-13", 1200000)
    cashflow.set_balance(conn, 2, "2026-08-13", -1420000)
    commitments.save(conn, "Clear the Amex", "payoff", "2028-08-01", account_id=2)
    commitments.save(conn, "New roof", "goal", "2027-08-01", target_cents=240000)
    conn.commit()

    p = commitments.position(conn, today=date(2026, 8, 13))
    assert p.per_month_cents == sum(c.per_month_cents for c in p.items)
    # 11 months to the roof, not 12: the 1st falls before the 13th
    roof = next(c for c in p.items if c.name == "New roof")
    assert (roof.months_left, roof.per_month_cents) == (11, 21818)
    assert p.has_debt_deadline is True


# --- on the page --------------------------------------------------------------

def test_the_page_separates_free_money_from_promised_money(signed_in):
    conn = open_db()
    account(conn, 1, "Savings", "savings")
    cashflow.set_balance(conn, 1, date.today().isoformat(), 1200000)
    commitments.save(conn, "New roof", "goal", "2027-06-01", target_cents=800000)
    conn.commit()
    conn.close()

    r = signed_in.get("/cashflow")
    assert "Set aside for something" in r.text
    assert "Free to spend" in r.text
    assert "$4,000.00" in r.text                  # 12,000 held minus 8,000 promised


def test_the_page_can_take_a_payoff_and_report_its_monthly_cost(signed_in):
    conn = open_db()
    account(conn, 1, "Amex", "credit")
    cashflow.set_balance(conn, 1, date.today().isoformat(), -1420000)
    conn.commit()
    conn.close()

    page = signed_in.get("/cashflow")
    signed_in.post("/cashflow/commitments", data={
        "csrf": get_csrf(page.text), "name": "Clear the Amex", "kind": "payoff",
        "account_id": "1", "due_date": "2028-08-01"})

    r = signed_in.get("/cashflow")
    assert "Clear the Amex" in r.text
    assert "still owed by" in r.text
    assert "not taken off twice" in r.text        # says why, rather than hiding it


def test_a_commitment_can_be_removed(signed_in):
    conn = open_db()
    account(conn, 1, "Savings", "savings")
    cid = commitments.save(conn, "New roof", "goal", "2027-06-01",
                           target_cents=800000)
    conn.commit()
    conn.close()

    page = signed_in.get("/cashflow")
    signed_in.post("/cashflow/commitments", data={
        "csrf": get_csrf(page.text), "commitment_id": str(cid), "delete": "1"})

    conn = open_db()
    assert commitments.all_commitments(conn) == []
    conn.close()


def test_a_commitment_with_no_date_is_refused_rather_than_guessed(signed_in):
    conn = open_db()
    account(conn, 1, "Savings", "savings")
    conn.close()
    page = signed_in.get("/cashflow")
    signed_in.post("/cashflow/commitments", data={
        "csrf": get_csrf(page.text), "name": "Vague", "kind": "goal",
        "target": "100", "due_date": ""})

    conn = open_db()
    assert commitments.all_commitments(conn) == []
    conn.close()
