from app.services import budgets, insights


def cat_id(conn, name):
    return conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()["id"]


def add_txn(conn, date, cents, desc, category=None, account=1):
    conn.execute(
        "INSERT OR IGNORE INTO accounts (id, name, type, created_at) "
        "VALUES (1, 'a', 'checking', 'now')")
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount_cents, description, "
        "normalized_desc, merchant_key, category_id, dedupe_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'now')",
        (account, date, cents, desc, desc.upper(), desc.upper(),
         cat_id(conn, category) if category else None,
         f"{date}|{cents}|{desc}|{account}"))
    conn.commit()


def test_shift_month():
    assert budgets.shift_month("2026-01", -1) == "2025-12"
    assert budgets.shift_month("2026-12", 1) == "2027-01"
    assert budgets.shift_month("2026-08", -13) == "2025-07"


def test_month_summary_math(conn):
    add_txn(conn, "2026-08-01", 300000, "PAY", "Salary")
    add_txn(conn, "2026-08-02", -50000, "market", "Groceries")
    add_txn(conn, "2026-08-03", -20000, "market2", "Groceries")
    add_txn(conn, "2026-08-04", -85000, "cc payment", "Credit Card Payment")  # excluded
    add_txn(conn, "2026-08-05", -1000, "mystery")  # uncategorized

    budgets.set_budget(conn, cat_id(conn, "Groceries"), "2026-08", 60000)
    conn.commit()
    s = budgets.month_summary(conn, "2026-08")
    assert s.income_actual == 300000
    assert s.expense_actual == 70000          # CC payment not counted
    assert s.net == 230000
    assert s.uncategorized_count == 1
    food = next(g for g in s.groups if g.name == "Food")
    groc = next(l for l in food.lines if l.name == "Groceries")
    assert groc.budget == 60000 and groc.actual == 70000
    assert groc.pct == 117


def test_copy_budgets(conn):
    budgets.set_budget(conn, cat_id(conn, "Groceries"), "2026-07", 40000)
    conn.commit()
    assert budgets.copy_budgets(conn, "2026-07", "2026-08") == 1
    assert budgets.get_budgets(conn, "2026-08")[cat_id(conn, "Groceries")] == 40000


def test_recurring_detection(conn):
    for month in ("2026-05", "2026-06", "2026-07", "2026-08"):
        add_txn(conn, f"{month}-04", -1599, "NETFLIX", "Subscriptions & Streaming")
    add_txn(conn, "2026-08-09", -12000, "one off shop", "Shopping")
    rec = insights.recurring_charges(conn, "2026-08")
    assert len(rec) == 1
    assert rec[0].merchant == "NETFLIX"
    assert rec[0].monthly_cents == 1599
    assert rec[0].months_seen == 4


def test_anomalies(conn):
    for month in ("2026-05", "2026-06", "2026-07"):
        add_txn(conn, f"{month}-10", -30000, f"food {month}", "Groceries")
    add_txn(conn, "2026-08-10", -90000, "big food", "Groceries")
    alerts = insights.anomalies(conn, "2026-08")
    assert len(alerts) == 1
    assert alerts[0].category == "Groceries"
    assert alerts[0].average == 30000 and alerts[0].current == 90000


def test_cashflow_excludes_transfers(conn):
    add_txn(conn, "2026-08-01", 100000, "PAY", "Salary")
    add_txn(conn, "2026-08-02", -40000, "VISA PAYMENT", "Credit Card Payment")
    flow = insights.cashflow(conn, "2026-08", 2)
    assert flow[-1]["income"] == 100000
    assert flow[-1]["spent"] == 0
