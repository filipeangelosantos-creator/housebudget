"""Knowing whether a month's statements have actually landed.

Reported: statements lag badly, so the first half of any month shows a picture
that is missing whole accounts. A month missing its card statement looks like a
frugal month, and the budget check then offers to lower a budget that was never
underspent. The fix is to say what is in and what isn't — and, because the dates
can only ever suggest it, to let you overrule the guess.
"""
from datetime import date

import pytest

from app.services import coverage

from test_bulk_and_pairing import add_txn, category, get_csrf, open_db  # noqa: F401


@pytest.fixture()
def signed_in(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    return web


def account(conn, name, kind="checking", archived=0, created="2020-01-01T00:00:00Z"):
    cur = conn.execute(
        "INSERT INTO accounts (name, type, archived, created_at) VALUES (?, ?, ?, ?)",
        (name, kind, archived, created))
    conn.commit()
    return cur.lastrowid


def state_of(conn, month, account_id, today=date(2026, 9, 20)):
    got = coverage.for_month(conn, month, today=today)
    return next(r for r in got.rows if r.account_id == account_id).state


# --- what the dates alone can tell you ----------------------------------------

def test_data_past_the_month_end_means_the_statement_arrived(conn):
    """The only honest automatic signal: something on this account is dated
    after the month finished, so whatever covers the month has been imported."""
    a = account(conn, "Chase")
    add_txn(conn, a, "2026-08-14", -1200, "SHELL")
    add_txn(conn, a, "2026-09-02", -1200, "SHELL")
    assert state_of(conn, "2026-08", a) == "in"


def test_a_month_that_stops_short_is_only_partly_in(conn):
    a = account(conn, "Amex")
    add_txn(conn, a, "2026-08-03", -4500, "CVS")
    add_txn(conn, a, "2026-08-11", -1800, "NETFLIX")
    assert state_of(conn, "2026-08", a) == "partial"


def test_nothing_for_the_month_at_all_is_still_waiting(conn):
    a = account(conn, "Amex")
    add_txn(conn, a, "2026-07-28", -4500, "CVS")
    assert state_of(conn, "2026-08", a) == "waiting"


def test_an_account_with_no_history_is_waiting_not_silent(conn):
    """Adding a card and never importing it is exactly the case worth saying
    out loud — the month's totals are missing it entirely."""
    a = account(conn, "Amex")
    assert state_of(conn, "2026-08", a) == "waiting"


def test_an_archived_account_is_never_counted_as_missing(conn):
    """Closed the account, so no statement is coming and none is wanted."""
    a = account(conn, "Old Card", archived=1)
    got = coverage.for_month(conn, "2026-08", today=date(2026, 9, 20))
    assert state_of(conn, "2026-08", a) == "dormant"
    assert got.expected == []
    assert got.pending == []


def test_an_account_added_after_the_month_isnt_asked_for_it(conn):
    a = account(conn, "New Card", created="2026-09-15T00:00:00Z")
    assert state_of(conn, "2026-09", a) == "waiting"     # its own month counts
    assert state_of(conn, "2026-08", a) == "dormant"     # but not before it existed


def test_backfilled_history_beats_when_the_account_was_added(conn):
    """Added today, then two years of statements imported: the month is in."""
    a = account(conn, "New Card", created="2026-09-15T00:00:00Z")
    add_txn(conn, a, "2026-08-14", -1200, "SHELL")
    add_txn(conn, a, "2026-09-02", -1200, "SHELL")
    assert state_of(conn, "2026-08", a) == "in"


# --- and what you say overrules it --------------------------------------------

def test_saying_a_month_is_in_beats_the_dates(conn):
    """A card you barely use can have a whole statement of nothing. Only you
    know the statement arrived and was empty."""
    a = account(conn, "Amex")
    assert state_of(conn, "2026-08", a) == "waiting"
    coverage.mark(conn, a, "2026-08", "closed")
    conn.commit()
    assert state_of(conn, "2026-08", a) == "closed"


def test_saying_one_is_still_coming_beats_the_dates_too(conn):
    """The statement that arrived covered 12 Aug to 11 Sep, so the dates read
    complete while half of August is still missing."""
    a = account(conn, "Amex")
    add_txn(conn, a, "2026-08-14", -1200, "SHELL")
    add_txn(conn, a, "2026-09-02", -1200, "SHELL")
    assert state_of(conn, "2026-08", a) == "in"
    coverage.mark(conn, a, "2026-08", "waiting")
    conn.commit()
    assert state_of(conn, "2026-08", a) == "waiting"


def test_a_mark_can_be_taken_back(conn):
    a = account(conn, "Amex")
    add_txn(conn, a, "2026-08-14", -1200, "SHELL")
    coverage.mark(conn, a, "2026-08", "closed")
    conn.commit()
    coverage.mark(conn, a, "2026-08", "clear")
    conn.commit()
    assert state_of(conn, "2026-08", a) == "partial"     # back to the dates


def test_a_mark_belongs_to_one_month_only(conn):
    a = account(conn, "Amex")
    coverage.mark(conn, a, "2026-08", "closed")
    conn.commit()
    assert state_of(conn, "2026-08", a) == "closed"
    assert state_of(conn, "2026-09", a) == "waiting"


def test_marking_the_same_month_twice_replaces_it(conn):
    a = account(conn, "Amex")
    coverage.mark(conn, a, "2026-08", "closed")
    coverage.mark(conn, a, "2026-08", "waiting", note="chasing the bank")
    conn.commit()
    got = next(r for r in coverage.for_month(conn, "2026-08").rows
               if r.account_id == a)
    assert (got.state, got.note) == ("waiting", "chasing the bank")


# --- the month as a whole ------------------------------------------------------

def test_a_month_is_complete_only_when_every_account_has_reported(conn):
    chase, amex = account(conn, "Chase"), account(conn, "Amex")
    add_txn(conn, chase, "2026-09-02", -1200, "SHELL")
    got = coverage.for_month(conn, "2026-08", today=date(2026, 9, 20))
    assert not got.complete
    assert [r.name for r in got.pending] == ["Amex"]

    coverage.mark(conn, amex, "2026-08", "closed")
    conn.commit()
    assert coverage.for_month(conn, "2026-08", today=date(2026, 9, 20)).complete


def test_no_accounts_at_all_is_not_a_complete_month(conn):
    """Otherwise a brand-new install claims every month is fully accounted for."""
    assert not coverage.for_month(conn, "2026-08").complete


def test_the_waiting_list_reads_as_a_sentence(conn):
    for n in ("Amex", "Chase", "Barclays"):
        account(conn, n)
    got = coverage.for_month(conn, "2026-08", today=date(2026, 9, 20))
    assert got.pending_names == "Amex, Barclays and Chase"


def test_the_current_month_is_flagged_as_still_running(conn):
    """Half of August missing in August is not a problem worth a red banner."""
    account(conn, "Chase")
    assert coverage.for_month(conn, "2026-08", today=date(2026, 8, 17)).in_progress
    assert not coverage.for_month(conn, "2026-07", today=date(2026, 8, 17)).in_progress


def test_closing_everything_at_once_leaves_nothing_pending(conn):
    for n in ("Amex", "Chase"):
        account(conn, n)
    n = coverage.close_all(conn, "2026-08", today=date(2026, 9, 20))
    conn.commit()
    assert n == 2
    assert coverage.for_month(conn, "2026-08", today=date(2026, 9, 20)).complete


def test_closing_everything_skips_what_is_already_settled(conn):
    chase, amex = account(conn, "Chase"), account(conn, "Amex")
    add_txn(conn, chase, "2026-09-02", -1200, "SHELL")     # already 'in'
    assert coverage.close_all(conn, "2026-08", today=date(2026, 9, 20)) == 1
    conn.commit()
    assert state_of(conn, "2026-08", chase) == "in"        # not overwritten
    assert state_of(conn, "2026-08", amex) == "closed"


def test_earlier_months_still_waiting_are_listed_newest_first(conn):
    a = account(conn, "Amex")
    add_txn(conn, a, "2026-06-14", -1200, "SHELL")         # June partial
    coverage.mark(conn, a, "2026-05", "closed")            # May done
    conn.commit()
    gaps = coverage.months_with_gaps(conn, "2026-07", back=4,
                                     today=date(2026, 9, 20))
    # July has nothing, June stops mid-month, May you closed by hand, and April
    # is covered by a later transaction.
    assert [g.month for g in gaps] == ["2026-07", "2026-06"]


# --- on the pages --------------------------------------------------------------
#
# These go through the routes, which read the real clock, so they work in a
# month that has definitely finished rather than a hardcoded one that stops
# being in the past.

def finished_month():
    from app.services import budgets as b
    return b.shift_month(b.current_month(), -2)


def a_day_in(month, day=4):
    return f"{month}-{day:02d}"


def test_the_statements_page_says_what_is_in(signed_in):
    m = finished_month()
    conn = open_db()
    chase, amex = account(conn, "Chase"), account(conn, "Amex")
    add_txn(conn, chase, a_day_in(m), -1200, "SHELL")
    add_txn(conn, chase, a_day_in(shift(m, 1), 2), -1200, "SHELL")
    conn.close()

    r = signed_in.get(f"/statements?month={m}")
    assert "Chase" in r.text and "Amex" in r.text
    assert "looks complete" in r.text
    assert "not in yet" in r.text


def shift(month, n):
    from app.services import budgets as b
    return b.shift_month(month, n)


def test_a_month_page_warns_that_figures_will_rise(signed_in):
    m = finished_month()
    conn = open_db()
    account(conn, "Amex")
    chase = account(conn, "Chase")
    add_txn(conn, chase, a_day_in(m), -12000, "SHELL", category(conn, "Fuel"))
    add_txn(conn, chase, a_day_in(shift(m, 1), 2), -12000, "SHELL",
            category(conn, "Fuel"))
    conn.close()

    r = signed_in.get(f"/budgets?month={m}")
    assert "these figures will rise" in r.text
    assert "Amex" in r.text


def test_marking_it_in_from_the_page_clears_the_warning(signed_in):
    m = finished_month()
    conn = open_db()
    amex = account(conn, "Amex")
    chase = account(conn, "Chase")
    add_txn(conn, chase, a_day_in(shift(m, 1), 2), -12000, "SHELL")
    conn.close()

    page = signed_in.get(f"/statements?month={m}")
    signed_in.post("/statements/mark", data={
        "csrf": get_csrf(page.text), "account_id": str(amex),
        "month": m, "state": "closed"})

    assert "these figures will rise" not in signed_in.get(f"/budgets?month={m}").text
    assert "Everything is in for" in signed_in.get(f"/statements?month={m}").text


def test_the_current_month_gets_the_quiet_note_not_the_banner(signed_in):
    conn = open_db()
    from app.services import budgets as b
    account(conn, "Amex")
    conn.close()
    r = signed_in.get(f"/statements?month={b.current_month()}")
    assert "is still running" in r.text


def test_marking_needs_a_valid_token(signed_in):
    conn = open_db()
    amex = account(conn, "Amex")
    conn.close()
    signed_in.post("/statements/mark", data={
        "csrf": "nope", "account_id": str(amex), "month": "2026-08",
        "state": "closed"})
    conn = open_db()
    assert conn.execute("SELECT COUNT(*) FROM statement_months").fetchone()[0] == 0
    conn.close()


def test_the_statements_page_needs_a_login(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    web.post("/logout")
    assert web.get("/statements", follow_redirects=False).status_code == 303


def test_merging_accounts_keeps_one_answer_per_month(conn):
    """Two rows for the same card, each with its own marks: the survivor keeps
    its own answer rather than ending up with two for one month."""
    from app.services import accounts as accounts_svc
    keep, dupe = account(conn, "Amex"), account(conn, "Amex Old")
    coverage.mark(conn, keep, "2026-08", "closed")
    coverage.mark(conn, dupe, "2026-08", "waiting")
    coverage.mark(conn, dupe, "2026-07", "closed")
    conn.commit()

    accounts_svc.merge_accounts(conn, dupe, keep)
    rows = conn.execute("SELECT month, state FROM statement_months "
                        "WHERE account_id = ? ORDER BY month", (keep,)).fetchall()
    assert [(r["month"], r["state"]) for r in rows] == [
        ("2026-07", "closed"), ("2026-08", "closed")]


def test_deleting_an_account_takes_its_marks_with_it(conn):
    from app.services import accounts as accounts_svc
    a = account(conn, "Amex")
    coverage.mark(conn, a, "2026-08", "closed")
    conn.commit()
    accounts_svc.delete_account(conn, a)
    assert conn.execute("SELECT COUNT(*) FROM statement_months").fetchone()[0] == 0


def test_the_gap_list_stops_where_your_statements_start(conn):
    """Every month before you started using the app is missing every statement.
    Saying so is true, useless, and six rows of amber."""
    a = account(conn, "Amex")
    add_txn(conn, a, "2026-06-14", -1200, "SHELL")
    gaps = coverage.months_with_gaps(conn, "2026-07", back=12,
                                     today=date(2026, 9, 20))
    assert [g.month for g in gaps] == ["2026-07", "2026-06"]


def test_an_archived_account_says_why_it_is_not_expected(signed_in):
    conn = open_db()
    account(conn, "Old Card", archived=1)
    conn.close()
    r = signed_in.get(f"/statements?month={finished_month()}")
    assert "archived, so no statement is expected" in r.text
    # and offers no buttons for a month it will never report
    assert r.text.count('name="state" value="closed"') == 0
