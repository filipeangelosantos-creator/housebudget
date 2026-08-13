from app.services import classify


def cat_id(conn, name):
    return conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()["id"]


def run(conn, desc):
    rules = classify.load_rules(conn)
    return classify.classify(rules, classify.normalize_desc(desc),
                             classify.merchant_key(desc))


def test_merchant_key_strips_noise():
    assert classify.merchant_key("STARBUCKS #1234 SEATTLE") == "STARBUCKS SEATTLE"
    assert classify.merchant_key("AMZN MKTP US*Z12AB3") == "AMZN MKTP US"
    assert classify.merchant_key("POS DEBIT WALMART 4421") == "WALMART"
    assert classify.merchant_key("PAYPAL *SPOTIFY 12345") == "PAYPAL SPOTIFY"


def test_merchant_survives_glued_reference():
    """Regression: ONEQUINCE*Q28484714 SANFRANCISCOCA lost its merchant to the
    digit filter, leaving the CITY as the key — and 'always file
    SANFRANCISCOCA here' as the suggested rule."""
    key = classify.merchant_key("ONEQUINCE*Q28484714 SANFRANCISCOCA")
    assert key.startswith("ONEQUINCE")
    assert classify.suggest_pattern("ONEQUINCE*Q28484714 SANFRANCISCOCA") == "ONEQUINCE"
    # same joiner style with '#'
    assert classify.merchant_key("TJMAXX#0569 BROOKLINE MA").startswith("TJMAXX")


def test_suggest_pattern():
    assert classify.suggest_pattern("STARBUCKS #1234 SEATTLE") == "STARBUCKS"
    assert classify.suggest_pattern("KFC 0231 LISBON") == "KFC LISBON"


def test_suggest_pattern_never_bare_aggregator():
    """A rule on 'GOOGLE' or 'PAYPAL' alone would swallow half the statement."""
    assert classify.suggest_pattern("GOOGLE*LinkedInCommu MOUNTAINVIEWCA") == \
        "GOOGLE LINKEDINCOMMU"
    assert classify.suggest_pattern("PAYPAL *SPOTIFY 12345") == "PAYPAL SPOTIFY"
    assert classify.suggest_pattern("MED*BETHISRAELLAHEY CAMBRIDGE MA") == \
        "MED BETHISRAELLAHEY"


def test_seed_rules_specificity(conn):
    # UBER EATS must beat plain UBER
    assert run(conn, "UBER EATS PENDING") == cat_id(conn, "Restaurants & Takeout")
    assert run(conn, "UBER *TRIP HELP.UBER.COM") == cat_id(conn, "Rideshare & Taxi")
    assert run(conn, "WALMART SUPERCENTER #3181") == cat_id(conn, "Groceries")
    assert run(conn, "INTEREST CHARGE ON PURCHASES") == cat_id(conn, "Bank Fees")
    assert run(conn, "TOTALLY UNKNOWN MERCHANT") is None


def test_learned_rule_beats_seed(conn):
    groceries = cat_id(conn, "Groceries")
    coffee = cat_id(conn, "Coffee & Snacks")
    assert run(conn, "STARBUCKS #99") == coffee
    classify.create_rule(conn, "STARBUCKS", groceries)  # learned, priority 10
    conn.commit()
    assert run(conn, "STARBUCKS #99") == groceries


def test_apply_rules_to_uncategorized(conn):
    conn.execute(
        "INSERT INTO accounts (name, type, created_at) VALUES ('t', 'checking', 'now')")
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount_cents, description, "
        "normalized_desc, merchant_key, dedupe_hash, created_at) "
        "VALUES (1, '2026-08-01', -500, 'MYSTERY SHOP', 'MYSTERY SHOP', "
        "'MYSTERY SHOP', 'h1', 'now')")
    conn.commit()
    assert classify.apply_rules_to_uncategorized(conn) == 0
    rule_id = classify.create_rule(conn, "MYSTERY", cat_id(conn, "Shopping"))
    conn.commit()
    assert classify.apply_rules_to_uncategorized(conn, only_rule_id=rule_id) == 1
    row = conn.execute("SELECT category_id FROM transactions WHERE dedupe_hash='h1'").fetchone()
    assert row["category_id"] == cat_id(conn, "Shopping")
