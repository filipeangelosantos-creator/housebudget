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


# --- the verdict follows you down the page ------------------------------------

def test_the_verdict_is_a_sticky_strip_not_a_line_at_the_top(signed_in):
    """It answers a question about the inputs further down the page, so it has
    to still be there when you scroll to them."""
    budget(Salary=300000, Groceries=120000)
    r = signed_in.get("/budgets?month=2026-08")
    assert 'class="bal-strip pos" id="bal-verdict"' in r.text
    # and it sits above the card it summarises, so reading order is verdict first
    assert r.text.index('id="bal-verdict"') < r.text.index('id="balance"')


# --- copying a budget from one month to another -------------------------------

def get_csrf(html):
    import re
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m
    return m.group(1)


def copy(client, into, frm):
    page = client.get(f"/budgets?month={into}")
    return client.post("/budgets/copy", data={
        "csrf": get_csrf(page.text), "month": into, "from_month": frm},
        follow_redirects=True)


def amounts(month):
    conn = db.connect(config.DB_PATH)
    got = {conn.execute("SELECT name FROM categories WHERE id = ?",
                        (cat,)).fetchone()["name"]: cents
           for cat, cents in budgets.get_budgets(conn, month).items()}
    conn.close()
    return got


def test_copying_an_earlier_month_forward(signed_in):
    budget("2026-06", Groceries=60000, Fuel=20000)
    r = copy(signed_in, "2026-08", "2026-06")
    assert amounts("2026-08") == {"Groceries": 60000, "Fuel": 20000}
    assert "Copied" in r.text and "June 2026" in r.text


def test_copying_this_month_backwards(signed_in):
    """Both directions: a statement imported late needs a budget for a month
    that has already gone by."""
    budget("2026-08", Groceries=70000)
    r = copy(signed_in, "2026-05", "2026-08")
    assert amounts("2026-05") == {"Groceries": 70000}
    assert "May 2026" in r.text            # it lands on the month that changed


def test_copying_replaces_rather_than_merges(signed_in):
    """A merge leaves the target holding amounts from a month you didn't copy,
    in categories the source never mentioned."""
    budget("2026-06", Groceries=60000)
    budget("2026-08", Fuel=99000, Groceries=10000)
    copy(signed_in, "2026-08", "2026-06")
    assert amounts("2026-08") == {"Groceries": 60000}      # no leftover Fuel


def test_copying_from_a_month_that_inherited_copies_what_it_shows(signed_in):
    """June's budget carries forward, so July shows it. Copying July must copy
    those figures, not the nothing July has rows for."""
    budget("2026-06", Groceries=60000)
    conn = db.connect(config.DB_PATH)
    assert budgets.get_budgets(conn, "2026-07") == {}       # no rows of its own
    assert budgets.effective_budgets(conn, "2026-07")[0]    # but it shows June's
    conn.close()

    copy(signed_in, "2026-11", "2026-07")
    assert amounts("2026-11") == {"Groceries": 60000}


def test_copying_a_month_onto_itself_does_nothing(signed_in):
    """The target is cleared before the copy, so a self-copy that went through
    the motions would be a way to wipe the month you were looking at."""
    budget("2026-08", Groceries=60000)
    r = copy(signed_in, "2026-08", "2026-08")
    assert amounts("2026-08") == {"Groceries": 60000}       # not wiped
    assert "the month you're already on" in r.text


def test_copying_from_an_empty_month_says_so_instead_of_looking_broken(signed_in):
    budget("2026-08", Groceries=60000)
    r = copy(signed_in, "2026-08", "2026-02")
    assert "Nothing to copy" in r.text
    assert "February 2026" in r.text
    assert amounts("2026-08") == {"Groceries": 60000}       # and left alone


def test_the_copy_panel_offers_both_directions(signed_in):
    budget("2026-06", Groceries=60000)
    r = signed_in.get("/budgets?month=2026-08")
    assert "Copy a budget between months" in r.text
    assert "Copy into August 2026 from" in r.text
    assert "Copy August 2026 out to" in r.text
    # the "from" box starts on the last month you actually budgeted
    assert 'name="from_month" value="2026-06"' in r.text
    assert "June 2026 is the most recent month you set a budget for" in r.text
    # and the "to" box on the month after this one
    assert 'name="month" value="2026-09"' in r.text


def test_copying_needs_a_valid_token(signed_in):
    budget("2026-06", Groceries=60000)
    r = signed_in.post("/budgets/copy", data={
        "csrf": "nope", "month": "2026-08", "from_month": "2026-06"})
    assert r.status_code == 403
    assert amounts("2026-08") == {}
