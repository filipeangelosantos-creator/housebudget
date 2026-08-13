"""Does the budget fit inside the income?

The budget page is where you decide what to spend, and the only question that
makes that a decision rather than a wish is whether the plan adds up to less
than you earn. It used to be one muted subtraction at the top of the page,
which is a number, not an answer.
"""
import pytest

from app import config, db
from app.services import budgets

from test_bulk_and_pairing import add_txn, category  # noqa: F401


@pytest.fixture()
def signed_in(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    return web


def budget(month="2026-08", **by_category):
    conn = db.connect(config.DB_PATH)
    for name, cents in by_category.items():
        budgets.set_budget(conn, category(conn, name.replace("_", " ")), month, cents)
    conn.commit()
    conn.close()


def payroll(dates, cents=200000):
    """A fortnightly wage, so the page has an expected income to fall back on."""
    conn = db.connect(config.DB_PATH)
    for d in dates:
        add_txn(conn, 1, d, cents, "ACME PAYROLL", category(conn, "Salary"))
    conn.close()


def test_a_plan_that_fits_says_what_is_left(signed_in):
    budget(Salary=300000, Groceries=120000, Fuel=80000)
    r = signed_in.get("/budgets?month=2026-08")
    assert "$1,000.00 left to allocate" in r.text
    assert "fits inside planned income" in r.text


def test_a_plan_that_does_not_fit_says_so_in_words(signed_in):
    budget(Salary=200000, Groceries=250000, Fuel=50000)
    r = signed_in.get("/budgets?month=2026-08")
    assert "$1,000.00 more than your income" in r.text
    assert "spends more than you plan to earn" in r.text
    assert "left to allocate" not in r.text


def test_a_plan_that_lands_exactly_on_the_income(signed_in):
    budget(Salary=200000, Groceries=200000)
    r = signed_in.get("/budgets?month=2026-08")
    assert "of income is allocated" in r.text
    assert "more than your income" not in r.text


def test_with_no_income_budget_it_measures_against_the_paydays(signed_in):
    """Most people budget their spending and never budget their pay. Falling
    back to nothing would report every such budget as infinitely over."""
    payroll(["2026-05-01", "2026-05-15", "2026-05-29", "2026-06-12",
             "2026-06-26", "2026-07-10", "2026-07-24", "2026-08-07"])
    budget(Groceries=100000)
    r = signed_in.get("/budgets?month=2026-08")
    # two paydays of $2,000 in August, $1,000 of it budgeted
    assert "$3,000.00 left to allocate" in r.text
    assert "No income budgeted yet" in r.text
    assert "$4,000.00" in r.text
    assert "your paydays are expected to" in r.text


def test_an_empty_budget_asks_for_one_rather_than_claiming_a_verdict(signed_in):
    r = signed_in.get("/budgets?month=2026-08")
    assert "Set a budget to see whether it fits your income" in r.text
    assert "left to allocate" not in r.text
    assert "more than your income" not in r.text


def test_the_totals_shown_are_the_ones_the_bars_are_drawn_from(signed_in):
    """Regression risk: two places computing "planned spending" drift apart.
    Both of these come from the same month summary."""
    budget(Salary=300000, Groceries=120000, Fuel=80000)
    r = signed_in.get("/budgets?month=2026-08")
    assert '<strong id="bal-income">$3,000.00</strong>' in r.text
    assert '<strong id="bal-spending">$2,000.00</strong>' in r.text


def test_an_excluded_category_is_left_out_of_both_sides(signed_in):
    """Credit Card Payment is money moving, not spending — budgeting it must
    not make the plan look over."""
    budget(Salary=300000, Groceries=100000, Credit_Card_Payment=500000)
    r = signed_in.get("/budgets?month=2026-08")
    assert "$2,000.00 left to allocate" in r.text


def test_the_page_gives_the_live_version_what_it_needs(signed_in):
    """The figure updates as you type; that needs the inputs labelled by side
    and the expected income to fall back on."""
    payroll(["2026-05-01", "2026-05-15", "2026-05-29", "2026-06-12",
             "2026-06-26", "2026-07-10", "2026-07-24", "2026-08-07"])
    budget(Salary=300000, Groceries=120000)
    r = signed_in.get("/budgets?month=2026-08")
    assert 'data-kind="income"' in r.text
    assert 'data-kind="expense"' in r.text
    assert 'data-expected="400000"' in r.text


def test_a_carried_forward_budget_is_measured_the_same_way(signed_in):
    """A month that inherits has no rows of its own; the verdict has to come
    from the effective budget or it reads as an empty plan."""
    budget("2026-06", Salary=300000, Groceries=120000)
    r = signed_in.get("/budgets?month=2026-08")
    assert "$1,800.00 left to allocate" in r.text
    assert "Carried forward from" in r.text
