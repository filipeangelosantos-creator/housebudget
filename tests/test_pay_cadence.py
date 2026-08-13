"""Budgets that carry forward, and income that lands every other week.

A fortnightly salary falls three times in some months and twice in others, so a
flat monthly income budget is wrong every time the third payday shows up. These
cover the cadence detection and the month-by-month expectation built on it.
"""
from datetime import date

from app.services import budgets, insights

from test_budgets_insights import add_txn, cat_id


def biweekly_dates(start: date, n: int) -> list[date]:
    from datetime import timedelta
    return [start + timedelta(days=14 * i) for i in range(n)]


def stream(cadence: str, days: list[int], last: str = "2026-08-07") -> insights.PayStream:
    return insights.PayStream(name="Acme", category="Salary", cadence=cadence,
                              typical_cents=200000,
                              last_date=date.fromisoformat(last), days_of_month=days)


# --- cadence detection -------------------------------------------------------

def test_biweekly_and_semimonthly_are_told_apart():
    """Both average about a fortnight; only the day-of-month pattern separates
    them, and getting it wrong makes every third payday disappear."""
    fortnightly = biweekly_dates(date(2026, 5, 1), 8)
    assert insights._classify_cadence(fortnightly)[0] == "biweekly"

    twice_monthly = [date(y, m, d) for y, m in
                     ((2026, 5), (2026, 6), (2026, 7), (2026, 8)) for d in (15, 30)]
    assert insights._classify_cadence(twice_monthly)[0] == "semimonthly"


def test_weekly_and_monthly_cadences():
    from datetime import timedelta
    weekly = [date(2026, 7, 3) + timedelta(days=7 * i) for i in range(6)]
    assert insights._classify_cadence(weekly)[0] == "weekly"

    monthly = [date(2026, m, 28) for m in range(3, 9)]
    kind, days = insights._classify_cadence(monthly)
    assert (kind, days) == ("monthly", [28])


def test_irregular_income_is_not_called_a_cadence():
    assert insights._classify_cadence([date(2026, 3, 2), date(2026, 3, 9)]) is None
    scattered = [date(2026, 3, 2), date(2026, 4, 27), date(2026, 5, 3),
                 date(2026, 8, 19)]
    assert insights._classify_cadence(scattered) is None


# --- how many paydays fall in a given month ----------------------------------

def test_fortnightly_pay_falls_three_times_in_some_months():
    s = stream("biweekly", [], last="2026-08-07")
    assert s.paydays_in("2026-08") == [date(2026, 8, 7), date(2026, 8, 21)]
    # Same stream, walked forward: October catches a third payday.
    assert s.paydays_in("2026-10") == [date(2026, 10, 2), date(2026, 10, 16),
                                       date(2026, 10, 30)]
    # and backward, before last_date, without drifting off the fortnight
    assert s.paydays_in("2026-07") == [date(2026, 7, 10), date(2026, 7, 24)]


def test_semimonthly_pay_clamps_to_the_end_of_short_months():
    s = stream("semimonthly", [15, 31])
    assert s.paydays_in("2026-02") == [date(2026, 2, 15), date(2026, 2, 28)]
    assert s.paydays_in("2026-03") == [date(2026, 3, 15), date(2026, 3, 31)]


def test_cadence_labels_read_as_english():
    assert stream("biweekly", []).cadence_label == "every 2 weeks"
    assert stream("semimonthly", [1, 15]).cadence_label == "twice a month"


# --- expected income for a month ---------------------------------------------

def payroll(conn, dates, cents=200000, desc="ACME PAYROLL"):
    for d in dates:
        add_txn(conn, d.isoformat(), cents, desc, "Salary")


def test_expected_income_follows_the_calendar_not_a_flat_figure(conn):
    payroll(conn, biweekly_dates(date(2026, 5, 1), 8))     # through 2026-08-07
    two_payday_month = insights.expected_income(conn, "2026-08")
    assert two_payday_month["total"] == 400000

    three_payday_month = insights.expected_income(conn, "2026-10")
    assert three_payday_month["total"] == 600000
    assert len(three_payday_month["detail"][0]["paydays"]) == 3
    assert three_payday_month["detail"][0]["stream"].cadence == "biweekly"


def test_expected_income_reports_what_has_already_landed(conn):
    payroll(conn, biweekly_dates(date(2026, 5, 1), 8))
    got = insights.expected_income(conn, "2026-08")
    # 2026-08-07 is the only one imported so far in August
    assert got["received"] == 200000
    assert got["total"] == 400000


def test_one_off_income_is_not_forecast(conn):
    add_txn(conn, "2026-08-11", 150000, "SOLD THE BIKE", "Other Income")
    got = insights.expected_income(conn, "2026-08")
    assert got["total"] == 0            # nothing recurring to project
    assert got["received"] == 150000    # but the money is still counted


# --- budgets carried forward -------------------------------------------------

def test_budget_carries_forward_until_the_month_sets_its_own(conn):
    groceries = cat_id(conn, "Groceries")
    budgets.set_budget(conn, groceries, "2026-06", 60000)
    conn.commit()

    inherited, source = budgets.effective_budgets(conn, "2026-08")
    assert inherited[groceries] == 60000
    assert source == "2026-06"

    budgets.set_budget(conn, groceries, "2026-08", 75000)
    conn.commit()
    own, source = budgets.effective_budgets(conn, "2026-08")
    assert own[groceries] == 75000
    assert source is None


def test_carried_budget_shows_up_in_the_month_summary(conn):
    groceries = cat_id(conn, "Groceries")
    budgets.set_budget(conn, groceries, "2026-06", 60000)
    conn.commit()
    add_txn(conn, "2026-08-02", -50000, "market", "Groceries")

    s = budgets.month_summary(conn, "2026-08")
    assert s.expense_budget == 60000
    assert s.budget_from == "2026-06"
    line = next(l for g in s.groups for l in g.lines if l.name == "Groceries")
    assert (line.budget, line.actual) == (60000, 50000)


def test_no_earlier_budget_means_no_budget(conn):
    assert budgets.effective_budgets(conn, "2026-08") == ({}, None)
    assert budgets.month_summary(conn, "2026-08").budget_from is None


def test_a_later_month_never_leaks_backwards(conn):
    """Carry-forward looks back only — setting September's budget must not
    change what August was measured against."""
    groceries = cat_id(conn, "Groceries")
    budgets.set_budget(conn, groceries, "2026-09", 90000)
    conn.commit()
    assert budgets.effective_budgets(conn, "2026-08") == ({}, None)


def test_upcoming_paydays_are_listed_only_while_they_are_still_ahead(conn):
    payroll(conn, biweekly_dates(date(2026, 5, 1), 8))     # last is 2026-08-07

    mid_month = insights.expected_income(conn, "2026-08", today=date(2026, 8, 13))
    assert mid_month["upcoming"] == [date(2026, 8, 21)]

    after = insights.expected_income(conn, "2026-08", today=date(2026, 8, 31))
    assert after["upcoming"] == []
    assert after["total"] == 400000


# --- pay that isn't the same every time --------------------------------------

def varying(conn, amounts, start=date(2026, 5, 1), desc="ACME PAYROLL"):
    """One fortnightly stream whose cheque differs each time."""
    from datetime import timedelta
    for i, cents in enumerate(amounts):
        add_txn(conn, (start + timedelta(days=14 * i)).isoformat(), cents, desc,
                "Salary")


def test_the_estimate_follows_the_recent_paydays_not_the_whole_history(conn):
    """A raise six months ago should not still be dragging the figure down."""
    varying(conn, [150000, 150000, 150000, 150000, 200000, 200000, 200000])
    stream = insights.pay_streams(conn, "2026-08")[0]
    assert stream.typical_cents == 200000      # the new rate, not the old median


def test_a_single_odd_cheque_does_not_move_the_estimate(conn):
    """Median of the recent window, so one big month is not mistaken for a
    raise — that is the whole reason it isn't a mean."""
    varying(conn, [160000, 162000, 158000, 161000, 900000, 159000])
    stream = insights.pay_streams(conn, "2026-08")[0]
    assert 155000 <= stream.typical_cents <= 165000


def test_a_varying_stream_reports_the_range_it_moves_in(conn):
    varying(conn, [150000, 172000, 158000, 165000, 149000, 168000])
    stream = insights.pay_streams(conn, "2026-08")[0]
    assert stream.varies is True
    assert (stream.low_cents, stream.high_cents) == (149000, 172000)
    assert [cents for _, cents in stream.recent][:2] == [168000, 149000]  # newest first


def test_pay_that_really_is_constant_is_not_called_variable(conn):
    varying(conn, [160000] * 6)
    stream = insights.pay_streams(conn, "2026-08")[0]
    assert stream.varies is False
    assert stream.low_cents == stream.high_cents == 160000


def test_a_few_cents_of_drift_is_noise_not_a_range(conn):
    varying(conn, [160000, 160120, 159950, 160080, 160010, 159990])
    assert insights.pay_streams(conn, "2026-08")[0].varies is False


def test_expected_income_carries_the_range_for_the_month(conn):
    varying(conn, [150000, 172000, 158000, 165000, 149000, 168000])
    got = insights.expected_income(conn, "2026-08")
    assert got["varies"] is True
    n = len(got["detail"][0]["paydays"])
    assert got["low"] == 149000 * n
    assert got["high"] == 172000 * n
    assert got["low"] < got["total"] < got["high"]


def test_two_deposits_on_one_day_are_one_payday_not_two_half_ones(conn):
    """Both salaries from the same employer land the same day under the same
    description. Averaging the transactions would halve every payday."""
    from datetime import timedelta
    day = date(2026, 5, 1)
    for _ in range(6):
        add_txn(conn, day.isoformat(), 120000, "ACME PAYROLL", "Salary")
        add_txn(conn, day.isoformat(), 90000, "ACME PAYROLL", "Salary")
        day += timedelta(days=14)

    stream = insights.pay_streams(conn, "2026-08")[0]
    assert stream.cadence == "biweekly"          # not "weekly" from doubled rows
    assert stream.typical_cents == 210000        # the two summed, not averaged
    assert stream.varies is False


def test_a_constant_stream_still_budgets_to_a_single_figure(conn):
    varying(conn, [160000] * 6)
    got = insights.expected_income(conn, "2026-08")
    assert got["varies"] is False
    assert got["low"] == got["total"] == got["high"]
