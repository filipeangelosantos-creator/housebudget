from app.services import budgets, insights, transfers


def cat_id(conn, name):
    return conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()["id"]


def account(conn, acc_id, name, type_="checking"):
    conn.execute(
        "INSERT OR IGNORE INTO accounts (id, name, type, created_at) VALUES (?, ?, ?, 'now')",
        (acc_id, name, type_))
    conn.commit()


def add_txn(conn, acc_id, date, cents, desc, category=None):
    from app.services.classify import merchant_key, normalize_desc
    cur = conn.execute(
        "INSERT INTO transactions (account_id, date, amount_cents, description, "
        "normalized_desc, merchant_key, category_id, dedupe_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'now')",
        (acc_id, date, cents, desc, normalize_desc(desc), merchant_key(desc),
         cat_id(conn, category) if category else None, f"{acc_id}|{date}|{cents}|{desc}"))
    conn.commit()
    return cur.lastrowid


def two_accounts(conn):
    account(conn, 1, "Checking", "checking")
    account(conn, 2, "Visa", "credit")


def test_auto_links_obvious_card_payment(conn):
    two_accounts(conn)
    out = add_txn(conn, 1, "2026-08-05", -85000, "TRANSFER TO VISA ****4421")
    inn = add_txn(conn, 2, "2026-08-06", 85000, "PAYMENT THANK YOU")
    assert transfers.auto_link(conn) == 1
    pairs = transfers.linked_pairs(conn)
    assert len(pairs) == 1
    assert (pairs[0]["out_id"], pairs[0]["in_id"]) == (out, inn)
    assert pairs[0]["source"] == "auto"


def test_paired_transfer_never_counts_as_money(conn):
    two_accounts(conn)
    add_txn(conn, 1, "2026-08-01", 300000, "PAYROLL", category="Salary")
    add_txn(conn, 1, "2026-08-05", -85000, "TRANSFER TO VISA")
    add_txn(conn, 2, "2026-08-05", 85000, "PAYMENT THANK YOU")
    add_txn(conn, 2, "2026-08-03", -12000, "GROCERY RUN", category="Groceries")
    transfers.auto_link(conn)

    s = budgets.month_summary(conn, "2026-08")
    assert s.income_actual == 300000        # the +850 arrival is not income
    assert s.expense_actual == 12000        # the -850 departure is not spending
    flow = insights.cashflow(conn, "2026-08", 1)
    assert flow[-1]["income"] == 300000
    assert flow[-1]["spent"] == 12000


def test_pairing_works_without_transfer_wording(conn):
    """The whole point: matching two sides beats matching description text."""
    two_accounts(conn)
    add_txn(conn, 1, "2026-08-10", -50000, "OPERACAO 8842")
    add_txn(conn, 2, "2026-08-10", 50000, "CREDITO CONTA 5521")
    # no hint wording on either side, so it is offered rather than assumed
    assert transfers.auto_link(conn) == 0
    suggestions = transfers.suggestions(conn)
    assert len(suggestions) == 1
    assert suggestions[0].amount_cents == 50000
    assert "same day" in suggestions[0].why

    c = suggestions[0]
    transfers.link(conn, c.out_txn["id"], c.in_txn["id"], source="manual")
    s = budgets.month_summary(conn, "2026-08")
    assert s.income_actual == 0 and s.expense_actual == 0


def test_same_account_and_far_apart_are_not_pairs(conn):
    two_accounts(conn)
    # same account
    add_txn(conn, 1, "2026-08-10", -20000, "A")
    add_txn(conn, 1, "2026-08-10", 20000, "B")
    # too far apart
    add_txn(conn, 1, "2026-08-01", -30000, "C")
    add_txn(conn, 2, "2026-08-20", 30000, "D")
    assert transfers.find_candidates(conn) == []


def test_dismissed_pair_stops_being_suggested(conn):
    two_accounts(conn)
    out = add_txn(conn, 1, "2026-08-10", -50000, "SHOP REFUND CASE")
    inn = add_txn(conn, 2, "2026-08-11", 50000, "REFUND RECEIVED")
    assert len(transfers.suggestions(conn)) == 1
    transfers.dismiss(conn, out, inn)
    assert transfers.suggestions(conn) == []


def test_unlink_restores_the_money(conn):
    two_accounts(conn)
    out = add_txn(conn, 1, "2026-08-05", -85000, "TRANSFER TO VISA", category="Transfers")
    add_txn(conn, 2, "2026-08-05", 85000, "PAYMENT THANK YOU",
            category="Credit Card Payment")
    transfers.auto_link(conn)
    assert transfers.linked_count(conn) == 1
    link_id = transfers.linked_pairs(conn)[0]["id"]
    transfers.unlink(conn, link_id)
    assert transfers.linked_count(conn) == 0
    assert len(transfers.find_candidates(conn)) == 1


def test_one_side_only_is_reported_as_unmatched(conn):
    """A card payment with no matching card statement hides real spending."""
    two_accounts(conn)
    add_txn(conn, 1, "2026-08-05", -85000, "TRANSFER TO VISA", category="Transfers")
    unmatched = transfers.unmatched_transfers(conn, "2026-08")
    assert len(unmatched) == 1
    assert unmatched[0]["amount_cents"] == -85000


def test_each_side_links_only_once(conn):
    two_accounts(conn)
    account(conn, 3, "Savings", "savings")
    out = add_txn(conn, 1, "2026-08-05", -50000, "TRANSFER OUT")
    a = add_txn(conn, 2, "2026-08-05", 50000, "TRANSFER IN")
    b = add_txn(conn, 3, "2026-08-05", 50000, "TRANSFER IN OTHER")
    assert transfers.link(conn, out, a) is True
    assert transfers.link(conn, out, b) is False     # out side already used
    assert transfers.linked_count(conn) == 1


def test_recurring_moves_link_as_one_group(conn):
    """A monthly savings move shouldn't need confirming twelve times."""
    two_accounts(conn)
    account(conn, 3, "Savings", "savings")
    for month in ("2026-04", "2026-05", "2026-06"):
        add_txn(conn, 1, f"{month}-02", -40000, "OPERACAO 8842 CONTA POUPANCA")
        add_txn(conn, 3, f"{month}-02", 40000, "DEPOSITO 8842")
    # one unrelated pair that must not be swept up
    add_txn(conn, 1, "2026-06-15", -7500, "SOMETHING ELSE")
    add_txn(conn, 2, "2026-06-15", 7500, "OTHER THING")

    pending = transfers.suggestions(conn)
    assert len(pending) == 4
    sizes = transfers.group_sizes(conn)
    savings_key = next(c.group_key for c in pending if c.amount_cents == 40000)
    assert sizes[savings_key] == 3

    assert transfers.link_group(conn, savings_key) == 3
    remaining = transfers.suggestions(conn)
    assert len(remaining) == 1 and remaining[0].amount_cents == 7500
    assert transfers.linked_count(conn) == 3


def test_dismiss_group(conn):
    two_accounts(conn)
    for month in ("2026-05", "2026-06"):
        add_txn(conn, 1, f"{month}-02", -40000, "REFUND CASE OUT")
        add_txn(conn, 2, f"{month}-02", 40000, "REFUND CASE IN")
    key = transfers.suggestions(conn)[0].group_key
    assert transfers.dismiss_group(conn, key) == 2
    assert transfers.suggestions(conn) == []
    assert transfers.linked_count(conn) == 0


def test_tiny_amounts_are_not_suggested(conn):
    two_accounts(conn)
    add_txn(conn, 1, "2026-08-10", -500, "COFFEE")
    add_txn(conn, 2, "2026-08-10", 500, "SMALL REFUND")
    assert transfers.find_candidates(conn) == []


# --- manual pairing ----------------------------------------------------------

def test_manual_candidates_offers_mismatched_amounts(conn):
    """The whole reason manual pairing exists: a wire fee makes the two sides
    differ, so the automatic matcher refuses to guess and offers nothing."""
    two_accounts(conn)
    out = add_txn(conn, 1, "2026-08-05", -100000, "WIRE OUT")
    add_txn(conn, 2, "2026-08-09", 99500, "WIRE IN LESS FEE")
    assert transfers.find_candidates(conn) == []          # amounts differ

    options = transfers.manual_candidates(conn, out)
    assert len(options) == 1
    assert options[0]["amount_cents"] == 99500
    assert options[0]["exact_amount"] is False
    assert options[0]["days_apart"] == 4


def test_manual_candidates_exclude_same_account_and_same_direction(conn):
    two_accounts(conn)
    account(conn, 3, "Savings", "savings")
    out = add_txn(conn, 1, "2026-08-05", -50000, "MOVE OUT")
    add_txn(conn, 1, "2026-08-05", 50000, "SAME ACCOUNT IN")     # same account
    add_txn(conn, 2, "2026-08-05", -50000, "ALSO GOING OUT")     # same direction
    add_txn(conn, 2, "2026-06-01", 50000, "TOO LONG AGO")        # outside window
    good = add_txn(conn, 3, "2026-08-06", 50000, "ARRIVED")

    assert [o["id"] for o in transfers.manual_candidates(conn, out)] == [good]


def test_manual_candidates_rank_exact_amounts_first(conn):
    two_accounts(conn)
    out = add_txn(conn, 1, "2026-08-05", -50000, "MOVE OUT")
    add_txn(conn, 2, "2026-08-05", 49000, "CLOSE BUT NOT EQUAL")
    exact = add_txn(conn, 2, "2026-08-12", 50000, "EXACT BUT LATER")
    assert transfers.manual_candidates(conn, out)[0]["id"] == exact


def test_link_pair_accepts_either_order_and_hides_the_money(conn):
    two_accounts(conn)
    out = add_txn(conn, 1, "2026-08-05", -100000, "WIRE OUT", category="Transfers")
    inn = add_txn(conn, 2, "2026-08-09", 99500, "WIRE IN", category="Salary")

    assert transfers.link_pair(conn, inn, out) is True       # given in-side first
    pair = transfers.linked_pairs(conn)[0]
    assert (pair["out_id"], pair["in_id"]) == (out, inn)     # stored the right way
    s = budgets.month_summary(conn, "2026-08")
    assert s.income_actual == 0 and s.expense_actual == 0


def test_link_pair_refuses_two_sides_going_the_same_way(conn):
    two_accounts(conn)
    a = add_txn(conn, 1, "2026-08-05", -50000, "OUT ONE")
    b = add_txn(conn, 2, "2026-08-05", -50000, "OUT TWO")
    assert transfers.link_pair(conn, a, b) is False
    assert transfers.link_pair(conn, a, a) is False
    assert transfers.linked_count(conn) == 0


def test_linked_partner_reports_the_other_side(conn):
    two_accounts(conn)
    out = add_txn(conn, 1, "2026-08-05", -85000, "TRANSFER TO VISA")
    inn = add_txn(conn, 2, "2026-08-06", 85000, "PAYMENT THANK YOU")
    assert transfers.linked_partner(conn, out) is None
    transfers.auto_link(conn)

    from_out = transfers.linked_partner(conn, out)
    from_in = transfers.linked_partner(conn, inn)
    assert from_out["id"] == inn and from_out["account_name"] == "Visa"
    assert from_in["id"] == out and from_in["account_name"] == "Checking"
    assert from_out["link_id"] == from_in["link_id"]
    # and a manual candidate list never offers an already-paired row
    assert transfers.manual_candidates(conn, out) == []


def test_unpaired_pool_skips_linked_rows(conn):
    two_accounts(conn)
    out = add_txn(conn, 1, "2026-08-05", -85000, "TRANSFER TO VISA")
    add_txn(conn, 2, "2026-08-06", 85000, "PAYMENT THANK YOU")
    loose = add_txn(conn, 1, "2026-08-07", -2500, "COFFEE SHOP")
    assert len(transfers.unpaired(conn)) == 3
    transfers.auto_link(conn)

    remaining = transfers.unpaired(conn)
    assert [r["id"] for r in remaining] == [loose]
    assert [r["id"] for r in transfers.unpaired(conn, q="coffee")] == [loose]
    assert transfers.unpaired(conn, account=2) == []
    assert out not in [r["id"] for r in remaining]
