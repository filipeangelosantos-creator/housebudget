import pytest

from app.services import budgets, insights, splits
from app.services.splits import Split


def cat_id(conn, name):
    return conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()["id"]


def add_txn(conn, date, cents, desc, category=None, needs_review=0):
    conn.execute(
        "INSERT OR IGNORE INTO accounts (id, name, type, created_at) "
        "VALUES (1, 'a', 'checking', 'now')")
    from app.services.classify import merchant_key, normalize_desc
    cur = conn.execute(
        "INSERT INTO transactions (account_id, date, amount_cents, description, "
        "normalized_desc, merchant_key, category_id, dedupe_hash, needs_review, "
        "created_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, 'now')",
        (date, cents, desc, normalize_desc(desc), merchant_key(desc),
         cat_id(conn, category) if category else None,
         f"{date}|{cents}|{desc}", needs_review))
    conn.commit()
    return cur.lastrowid


def test_split_must_add_up(conn):
    txn = add_txn(conn, "2026-08-02", -20000, "COSTCO WHOLESALE #692")
    with pytest.raises(splits.SplitError):
        splits.save_splits(conn, txn, [
            Split(cat_id(conn, "Groceries"), -12000),
            Split(cat_id(conn, "Clothing"), -5000),   # 3000 short
        ])
    assert splits.get_splits(conn, txn) == []


def test_split_needs_two_parts(conn):
    txn = add_txn(conn, "2026-08-02", -20000, "COSTCO WHOLESALE #692")
    with pytest.raises(splits.SplitError):
        splits.save_splits(conn, txn, [Split(cat_id(conn, "Groceries"), -20000)])


def test_split_allocates_to_each_category(conn):
    txn = add_txn(conn, "2026-08-02", -20000, "COSTCO WHOLESALE #692",
                  category="Groceries", needs_review=1)
    splits.save_splits(conn, txn, [
        Split(cat_id(conn, "Groceries"), -12000),
        Split(cat_id(conn, "Clothing"), -5000),
        Split(cat_id(conn, "Home Maintenance"), -3000, note="lightbulbs"),
    ])
    actuals = budgets.actuals_by_category(conn, "2026-08")
    assert actuals[cat_id(conn, "Groceries")] == -12000
    assert actuals[cat_id(conn, "Clothing")] == -5000
    assert actuals[cat_id(conn, "Home Maintenance")] == -3000

    # the transaction keeps a representative category and stops asking
    row = conn.execute("SELECT category_id, needs_review FROM transactions WHERE id = ?",
                       (txn,)).fetchone()
    assert row["category_id"] == cat_id(conn, "Groceries")   # largest part
    assert row["needs_review"] == 0

    # totals are unchanged by splitting
    s = budgets.month_summary(conn, "2026-08")
    assert s.expense_actual == 20000


def test_split_does_not_double_count_in_insights(conn):
    txn = add_txn(conn, "2026-08-02", -20000, "COSTCO WHOLESALE #692")
    splits.save_splits(conn, txn, [
        Split(cat_id(conn, "Groceries"), -15000),
        Split(cat_id(conn, "Clothing"), -5000),
    ])
    flow = insights.cashflow(conn, "2026-08", 1)
    assert flow[-1]["spent"] == 20000            # whole amount, counted once

    merchants = insights.top_merchants(conn, "2026-08")
    assert len(merchants) == 1
    assert merchants[0]["spent"] == 20000
    assert merchants[0]["n"] == 1                # one transaction, not three

    largest = insights.largest_transactions(conn, "2026-08")
    assert len(largest) == 1
    assert largest[0]["amount_cents"] == -20000
    assert "split across 2" in largest[0]["category"]


def test_unsplit_restores_single_category(conn):
    txn = add_txn(conn, "2026-08-02", -20000, "COSTCO WHOLESALE #692")
    splits.save_splits(conn, txn, [
        Split(cat_id(conn, "Groceries"), -15000),
        Split(cat_id(conn, "Clothing"), -5000),
    ])
    splits.clear_splits(conn, txn)
    actuals = budgets.actuals_by_category(conn, "2026-08")
    assert actuals[cat_id(conn, "Groceries")] == -20000
    assert cat_id(conn, "Clothing") not in actuals


def test_merchant_history_and_suggestion(conn):
    for i, day in enumerate(("01", "08", "15")):
        add_txn(conn, f"2026-07-{day}", -8000, "COSTCO WHOLESALE #692",
                category="Groceries")
    add_txn(conn, "2026-07-20", -6000, "COSTCO WHOLESALE #692", category="Clothing")

    mkey = conn.execute("SELECT merchant_key FROM transactions LIMIT 1").fetchone()[0]
    history = splits.merchant_history(conn, mkey)
    assert history[0]["name"] == "Groceries" and history[0]["count"] == 3
    assert history[0]["share"] == 75
    assert history[1]["name"] == "Clothing"

    suggestion = splits.suggest_category(conn, mkey)
    assert suggestion["name"] == "Groceries"


def test_mixed_basket_detection(conn):
    assert splits.is_mixed_basket_merchant("COSTCO WHOLESALE #692", "COSTCO WHOLESALE")
    assert splits.is_mixed_basket_merchant("AMZN MKTP US*Z12", "AMZN MKTP")
    assert not splits.is_mixed_basket_merchant("STARBUCKS #123", "STARBUCKS")

    # big-box charge over the threshold prompts; a small one doesn't
    assert splits.should_prompt_split(conn, "COSTCO WHOLESALE", "COSTCO", -20000)
    assert not splits.should_prompt_split(conn, "COSTCO WHOLESALE", "COSTCO", -900)
    assert not splits.should_prompt_split(conn, "STARBUCKS", "STARBUCKS", -20000)
    # income is never a split prompt
    assert not splits.should_prompt_split(conn, "COSTCO REFUND", "COSTCO", 20000)


def test_learned_multi_category_merchant_prompts(conn):
    """A merchant you've filed under two categories starts prompting too."""
    assert not splits.should_prompt_split(conn, "LOCAL MARKET", "LOCAL MARKET", -9000)
    add_txn(conn, "2026-07-01", -5000, "LOCAL MARKET", category="Groceries")
    add_txn(conn, "2026-07-02", -5000, "LOCAL MARKET", category="Clothing")
    assert splits.should_prompt_split(conn, "LOCAL MARKET", "LOCAL MARKET", -9000)


def test_pending_confirmations(conn):
    a = add_txn(conn, "2026-08-01", -20000, "COSTCO WHOLESALE #692",
                category="Groceries", needs_review=1)
    add_txn(conn, "2026-08-02", -900, "STARBUCKS #1", category="Coffee & Snacks")
    pending = splits.pending_confirmations(conn)
    assert [p["id"] for p in pending] == [a]
    assert splits.pending_confirmation_count(conn) == 1

    splits.confirm_category(conn, a)
    assert splits.pending_confirmation_count(conn) == 0


def test_uncategorized_excluded_from_cashflow(conn):
    """Regression: uncategorized money must not inflate the cashflow chart."""
    add_txn(conn, "2026-08-01", 300000, "PAY", category="Salary")
    add_txn(conn, "2026-08-02", -50000, "MYSTERY TRANSFER")   # uncategorized
    flow = insights.cashflow(conn, "2026-08", 1)
    assert flow[-1]["income"] == 300000
    assert flow[-1]["spent"] == 0

    summary = budgets.month_summary(conn, "2026-08")
    assert summary.expense_actual == 0          # dashboard agrees
    assert summary.uncategorized_count == 1
