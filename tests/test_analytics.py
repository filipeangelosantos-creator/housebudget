from datetime import date

from app.services import budgets, charts, insights


def cat_id(conn, name):
    return conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()["id"]


def add_txn(conn, date, cents, desc, category=None, account=1):
    from app.services.classify import merchant_key, normalize_desc
    conn.execute(
        "INSERT OR IGNORE INTO accounts (id, name, type, created_at) "
        "VALUES (1, 'a', 'checking', 'now')")
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount_cents, description, "
        "normalized_desc, merchant_key, category_id, dedupe_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'now')",
        (account, date, cents, desc, normalize_desc(desc), merchant_key(desc),
         cat_id(conn, category) if category else None, f"{date}|{cents}|{desc}"))
    conn.commit()


def test_days_in_month():
    assert insights.days_in_month("2026-02") == 28
    assert insights.days_in_month("2024-02") == 29
    assert insights.days_in_month("2026-08") == 31
    assert insights.days_in_month("2026-04") == 30


def test_spending_pace_cumulates_and_projects(conn):
    add_txn(conn, "2026-07-05", -20000, "shop a", "Groceries")
    add_txn(conn, "2026-07-25", -10000, "shop b", "Groceries")
    add_txn(conn, "2026-08-03", -12000, "shop c", "Groceries")
    add_txn(conn, "2026-08-10", -8000, "shop d", "Groceries")
    budgets.set_budget(conn, cat_id(conn, "Groceries"), "2026-08", 60000)
    conn.commit()

    # a finished month: the whole month has elapsed
    pace = insights.spending_pace(conn, "2026-08", today=date(2026, 9, 4))
    assert pace["days"] == 31 and pace["elapsed"] == 31
    assert pace["this"][2] == 12000               # cumulative on the 3rd
    assert pace["this"][9] == 20000               # cumulative on the 10th
    assert pace["this"][-1] == 20000              # flat after the last purchase
    assert pace["spent"] == 20000
    assert pace["budget"] == 60000
    assert pace["prev_same_day"] == 30000         # all of July by day 31
    assert pace["projected"] == 20000             # nothing left to project

    # mid-month: only what's happened counts, and the rest is extrapolated
    mid = insights.spending_pace(conn, "2026-08", today=date(2026, 8, 10))
    assert mid["elapsed"] == 10
    assert mid["spent"] == 20000
    assert mid["prev_same_day"] == 20000          # July by day 10
    assert mid["on_pace"] == round(60000 * 10 / 31)
    assert mid["projected"] == round(20000 * 31 / 10)   # heading well over budget
    assert mid["projected"] > mid["budget"]


def test_spending_pace_ignores_income_and_transfers(conn):
    add_txn(conn, "2026-08-01", 300000, "PAY", "Salary")
    add_txn(conn, "2026-08-02", -85000, "CARD PAYMENT", "Credit Card Payment")
    add_txn(conn, "2026-08-03", -5000, "shop", "Groceries")
    pace = insights.spending_pace(conn, "2026-08")
    assert pace["this"][-1] == 5000


def test_monthly_net(conn):
    add_txn(conn, "2026-07-01", 200000, "PAY", "Salary")
    add_txn(conn, "2026-07-05", -250000, "big spend", "Shopping")
    add_txn(conn, "2026-08-01", 300000, "PAY", "Salary")
    add_txn(conn, "2026-08-05", -100000, "spend", "Shopping")
    nets = insights.monthly_net(conn, "2026-08", 2)
    assert [n["month"] for n in nets] == ["2026-07", "2026-08"]
    assert nets[0]["net"] == -50000        # deficit
    assert nets[1]["net"] == 200000        # surplus


def test_category_composition_groups_tail_into_other(conn):
    for name, amount in (("Groceries", 50000), ("Fuel", 30000), ("Clothing", 20000),
                         ("Pharmacy", 10000), ("Fitness", 9000), ("Pets", 8000),
                         ("Hobbies", 7000), ("Education", 6000)):
        add_txn(conn, "2026-08-05", -amount, f"shop {name}", name)
    comp = insights.category_composition(conn, "2026-08", n=1, top=6)
    names = [s["name"] for s in comp["series"]]
    assert names[:3] == ["Groceries", "Fuel", "Clothing"]
    assert len(names) == 7 and names[-1] == "Other"
    # nothing is lost: parts still total the month's spending
    assert sum(s["monthly"][0] for s in comp["series"]) == 140000
    assert comp["series"][-1]["monthly"][0] == 13000    # Hobbies + Education


def test_template_dicts_avoid_dict_method_names(conn):
    """Jinja resolves `x.values` to dict.values(), silently rendering nothing.
    Any dict handed to a template must not use a dict method name as a key."""
    add_txn(conn, "2026-08-05", -12000, "shop", "Groceries")
    reserved = set(dir({}))

    structures = [
        insights.category_composition(conn, "2026-08", 2),
        insights.spending_pace(conn, "2026-08"),
        *insights.category_trends(conn, "2026-08", 2),
        *insights.monthly_net(conn, "2026-08", 2),
        *insights.top_merchants(conn, "2026-08"),
        *insights.largest_transactions(conn, "2026-08"),
    ]
    for struct in structures:
        clashes = set(struct) & reserved
        assert not clashes, f"key(s) shadow dict methods: {clashes}"
    for s in insights.category_composition(conn, "2026-08", 2)["series"]:
        assert not set(s) & reserved


def test_biggest_movers_reports_both_directions(conn):
    for month in ("2026-05", "2026-06", "2026-07"):
        add_txn(conn, f"{month}-10", -30000, f"food {month}", "Groceries")
        add_txn(conn, f"{month}-11", -20000, f"fuel {month}", "Fuel")
    add_txn(conn, "2026-08-10", -60000, "big food", "Groceries")   # way up
    add_txn(conn, "2026-08-11", -2000, "little fuel", "Fuel")      # way down

    movers = insights.biggest_movers(conn, "2026-08")
    by_name = {m.category: m for m in movers}
    assert by_name["Groceries"].change == 30000
    assert by_name["Groceries"].pct_change == 100
    assert by_name["Fuel"].change == -18000
    assert by_name["Fuel"].pct_change == -90


def test_year_over_year(conn):
    add_txn(conn, "2025-08-05", -100000, "old spend", "Groceries")
    add_txn(conn, "2026-08-05", -150000, "new spend", "Groceries")
    yoy = insights.year_over_year(conn, "2026-08")
    assert yoy["spent_then"] == 100000 and yoy["spent_now"] == 150000
    assert yoy["spent_pct"] == 50
    # no data a year before -> nothing to compare
    assert insights.year_over_year(conn, "2025-08") is None


def test_charts_render_valid_svg(conn):
    add_txn(conn, "2026-08-05", -12000, "shop", "Groceries")
    add_txn(conn, "2026-08-06", 200000, "PAY", "Salary")
    add_txn(conn, "2026-07-05", -40000, "shop", "Groceries")

    pace_svg = charts.pace_chart(insights.spending_pace(conn, "2026-08"))
    net_svg = charts.net_bars_chart(insights.monthly_net(conn, "2026-08", 3))
    stack_svg = charts.stacked_chart(insights.category_composition(conn, "2026-08", 3))
    flow_svg = charts.cashflow_chart(insights.cashflow(conn, "2026-08", 3))

    for svg in (pace_svg, net_svg, stack_svg, flow_svg):
        assert svg.startswith("<svg") and svg.endswith("</svg>")
        assert 'role="img"' in svg and "aria-label" in svg
        assert svg.count("<title>") > 0        # hover values on the marks
        assert "NaN" not in svg and "Infinity" not in svg
    # surplus and deficit use different marks
    assert "bar-surplus" in net_svg
    # stacked segments use the fixed categorical order, never cycled past 7
    assert "series-1" in stack_svg


def test_charts_survive_empty_and_single_point(conn):
    assert charts.net_bars_chart([]) == ""
    assert charts.stacked_chart({"months": [], "series": []}) == ""
    assert charts.cashflow_chart([]) == ""
    pace = insights.spending_pace(conn, "2026-08")      # no transactions at all
    svg = charts.pace_chart(pace)
    assert svg.startswith("<svg") and "NaN" not in svg


def test_pace_chart_handles_zero_budget(conn):
    add_txn(conn, "2026-08-05", -1000, "shop", "Groceries")
    pace = insights.spending_pace(conn, "2026-08")
    assert pace["budget"] == 0 and pace["on_pace"] == 0
    assert "NaN" not in charts.pace_chart(pace)
