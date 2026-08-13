"""Auditing what the classifier decided on its own.

The review queue only surfaces what the app *couldn't* place. Anything it
placed confidently went into the budget unseen, so there has to be a way to see
and change those too — and to tell them apart from the categories you chose.
"""
import re

import pytest

from app import config, db
from app.services import classified, classify

from test_bulk_and_pairing import add_txn, category, get_csrf, open_db  # noqa: F401


@pytest.fixture()
def signed_in(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    return web


def imported(conn, desc, n=3, cents=-2000, start_day=1):
    """Rows put through the same path an import uses, so they carry whatever
    attribution the classifier gives them."""
    from app.services.classify import load_rules, matching_rule, merchant_key
    from app.services.classify import normalize_desc
    account(conn)
    rules = load_rules(conn)
    ids = []
    for i in range(n):
        norm = normalize_desc(desc)
        mkey = merchant_key(desc)
        rule = matching_rule(rules, norm, mkey)
        cur = conn.execute(
            "INSERT INTO transactions (account_id, date, amount_cents, description, "
            "normalized_desc, merchant_key, category_id, dedupe_hash, classified_by, "
            "rule_id, created_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'now')",
            (f"2026-08-{start_day + i:02d}", cents, desc, norm, mkey,
             rule["category_id"] if rule else None, f"{desc}|{i}|{cents}",
             "rule" if rule else "", rule["id"] if rule else None))
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def account(conn):
    conn.execute("INSERT OR IGNORE INTO accounts (id, name, type, created_at) "
                 "VALUES (1, 'Checking', 'checking', 'now')")
    conn.commit()


# --- attribution -------------------------------------------------------------

def test_a_rule_match_records_which_rule_filed_it(conn):
    account(conn)
    groceries = category(conn, "Groceries")
    rule_id = classify.create_rule(conn, "MERCADO CENTRAL", groceries)
    conn.commit()
    add_txn(conn, 1, "2026-08-02", -3000, "MERCADO CENTRAL LISBOA")
    assert classify.apply_rules_to_uncategorized(conn) == 1

    row = conn.execute("SELECT classified_by, rule_id, category_id "
                       "FROM transactions").fetchone()
    assert (row["classified_by"], row["rule_id"]) == ("rule", rule_id)
    assert row["category_id"] == groceries


def test_choosing_a_category_yourself_is_recorded_as_yours(signed_in):
    conn = open_db()
    txn = add_txn(conn, 1, "2026-08-02", -3000, "SOME SHOP")
    groceries = category(conn, "Groceries")
    r = signed_in.get(f"/transactions/{txn}")
    signed_in.post(f"/transactions/{txn}", data={
        "csrf": get_csrf(r.text), "category_id": str(groceries), "notes": ""})

    row = conn.execute("SELECT classified_by, rule_id FROM transactions "
                       "WHERE id = ?", (txn,)).fetchone()
    assert (row["classified_by"], row["rule_id"]) == ("user", None)
    conn.close()


def test_deleting_a_rule_keeps_the_transactions_it_filed(conn):
    """Losing a rule must never lose your history — the rows stay categorized
    and are flagged as owned by nothing."""
    account(conn)
    groceries = category(conn, "Groceries")
    rule_id = classify.create_rule(conn, "MERCADO", groceries)
    conn.commit()
    add_txn(conn, 1, "2026-08-02", -3000, "MERCADO CENTRAL")
    classify.apply_rules_to_uncategorized(conn)

    conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    conn.commit()
    row = conn.execute("SELECT category_id, rule_id, classified_by "
                       "FROM transactions").fetchone()
    assert row["category_id"] == groceries
    assert row["rule_id"] is None
    assert row["classified_by"] == "rule"

    group = classified.merchant_groups(conn)[0]
    assert group["orphan_rule"] is True
    assert "no longer exists" in group["why"]


def test_legacy_rows_are_labelled_when_the_column_arrives(conn):
    """An existing database has no record of who filed what. Rows a rule would
    produce today are attributed to it; everything else stays yours, which is
    the side that never hides one of your own choices."""
    account(conn)
    groceries, clothes = category(conn, "Groceries"), category(conn, "Clothing")
    rule_id = classify.create_rule(conn, "MERCADO", groceries)
    conn.commit()
    by_rule = add_txn(conn, 1, "2026-08-02", -3000, "MERCADO CENTRAL", groceries)
    disagrees = add_txn(conn, 1, "2026-08-03", -4000, "MERCADO CENTRAL", clothes)
    no_rule = add_txn(conn, 1, "2026-08-04", -5000, "HARDWARE STORE", clothes)
    conn.execute("UPDATE transactions SET classified_by = '', rule_id = NULL")
    conn.commit()

    assert classify.label_existing_classifications(conn) == 3
    got = {r["id"]: (r["classified_by"], r["rule_id"]) for r in
           conn.execute("SELECT id, classified_by, rule_id FROM transactions")}
    assert got[by_rule] == ("rule", rule_id)
    assert got[disagrees] == ("user", None)     # category differs from the rule
    assert got[no_rule] == ("user", None)


# --- the merchant audit list -------------------------------------------------

def test_groups_are_one_row_per_merchant_biggest_first(conn):
    account(conn)
    groceries = category(conn, "Groceries")
    classify.create_rule(conn, "MERCADO", groceries)
    classify.create_rule(conn, "COFFEE", groceries)
    conn.commit()
    imported(conn, "MERCADO CENTRAL LISBOA", n=3, cents=-10000)
    imported(conn, "COFFEE CORNER", n=5, cents=-500, start_day=10)

    groups = classified.merchant_groups(conn)
    assert [g["merchant_key"] for g in groups] == ["MERCADO CENTRAL LISBOA",
                                                   "COFFEE CORNER"]
    big = groups[0]
    assert (big["n"], big["total"]) == (3, -30000)
    assert big["category_name"] == "Groceries"
    assert big["mixed"] is False
    assert 'rule “MERCADO”' in big["why"]


def test_a_merchant_filed_two_ways_is_shown_as_mixed(conn):
    account(conn)
    groceries, clothes = category(conn, "Groceries"), category(conn, "Clothing")
    add_txn(conn, 1, "2026-08-01", -1000, "COSTCO WHOLESALE", groceries)
    add_txn(conn, 1, "2026-08-02", -2000, "COSTCO WHOLESALE", groceries)
    add_txn(conn, 1, "2026-08-03", -9000, "COSTCO WHOLESALE", clothes)
    conn.execute("UPDATE transactions SET classified_by = 'user'")
    conn.commit()

    g = classified.merchant_groups(conn, source="user")[0]
    assert g["mixed"] is True
    assert g["category_name"] == "Groceries"          # the more common one
    assert {b["category_name"]: b["n"] for b in g["buckets"]} == {"Groceries": 2,
                                                                 "Clothing": 1}


def test_the_default_view_hides_what_you_filed_yourself(conn):
    account(conn)
    groceries = category(conn, "Groceries")
    classify.create_rule(conn, "MERCADO", groceries)
    conn.commit()
    imported(conn, "MERCADO CENTRAL", n=2)
    mine = add_txn(conn, 1, "2026-08-09", -7000, "MY OWN CHOICE", groceries)
    conn.execute("UPDATE transactions SET classified_by = 'user' WHERE id = ?", (mine,))
    conn.commit()

    auto = [g["merchant_key"] for g in classified.merchant_groups(conn)]
    assert auto == ["MERCADO CENTRAL"]
    assert [g["merchant_key"] for g in
            classified.merchant_groups(conn, source="user")] == ["MY OWN CHOICE"]
    assert len(classified.merchant_groups(conn, source="all")) == 2


def test_filters_narrow_the_audit_the_way_the_page_shows_it(conn):
    account(conn)
    conn.execute("INSERT INTO accounts (id, name, type, created_at) "
                 "VALUES (2, 'Visa', 'credit', 'now')")
    groceries = category(conn, "Groceries")
    add_txn(conn, 1, "2026-08-02", -1000, "SHOP ONE", groceries)
    add_txn(conn, 1, "2026-07-02", -1000, "SHOP TWO", groceries)
    add_txn(conn, 2, "2026-08-02", -1000, "SHOP THREE", groceries)
    conn.execute("UPDATE transactions SET classified_by = 'rule'")
    conn.commit()

    assert len(classified.merchant_groups(conn)) == 3
    assert len(classified.merchant_groups(conn, month="2026-08")) == 2
    assert len(classified.merchant_groups(conn, account=2)) == 1
    assert [g["merchant_key"] for g in
            classified.merchant_groups(conn, q="two")] == ["SHOP TWO"]


# --- changing a merchant's category ------------------------------------------

def test_refiling_a_merchant_moves_every_one_of_its_transactions(conn):
    account(conn)
    groceries, clothes = category(conn, "Groceries"), category(conn, "Clothing")
    classify.create_rule(conn, "TARGET", groceries)
    conn.commit()
    ids = imported(conn, "TARGET STORE", n=4)

    assert classified.refile(conn, "TARGET STORE", clothes, remember=True) == 4
    cats = {r["category_id"] for r in conn.execute(
        "SELECT category_id FROM transactions")}
    assert cats == {clothes}
    # and it sticks: a future import of the same merchant follows the new rule
    assert classify.classify(classify.load_rules(conn), "TARGET STORE",
                             "TARGET STORE") == clothes
    assert len(ids) == 4


def test_remembering_uses_an_exact_rule_that_cannot_bleed(conn):
    """A `contains` rule built from a merchant name is what makes rules catch
    unrelated shops; an exact key can only ever match this one."""
    account(conn)
    clothes = category(conn, "Clothing")
    imported(conn, "GAP", n=2)
    classified.refile(conn, "GAP", clothes, remember=True)

    rule = conn.execute("SELECT pattern, match_type FROM rules "
                        "WHERE pattern = 'GAP'").fetchone()
    assert rule["match_type"] == "exact"
    rules = classify.load_rules(conn)
    assert classify.classify(rules, "GAP", "GAP") == clothes
    assert classify.classify(rules, "GAP INC WAREHOUSE SALE",
                             "GAP INC WAREHOUSE SALE") != clothes


def test_refiling_twice_updates_the_rule_instead_of_stacking_them(conn):
    account(conn)
    groceries, clothes = category(conn, "Groceries"), category(conn, "Clothing")
    imported(conn, "SOME SHOP", n=2)
    classified.refile(conn, "SOME SHOP", clothes, remember=True)
    classified.refile(conn, "SOME SHOP", groceries, remember=True)

    rules = conn.execute("SELECT category_id FROM rules WHERE pattern = 'SOME SHOP'"
                         ).fetchall()
    assert len(rules) == 1 and rules[0]["category_id"] == groceries


def test_not_remembering_leaves_no_rule_and_marks_it_yours(conn):
    account(conn)
    clothes = category(conn, "Clothing")
    imported(conn, "ONE OFF SHOP", n=2)
    classified.refile(conn, "ONE OFF SHOP", clothes, remember=False)

    assert conn.execute("SELECT COUNT(*) FROM rules WHERE pattern = 'ONE OFF SHOP'"
                        ).fetchone()[0] == 0
    rows = conn.execute("SELECT classified_by, rule_id, category_id "
                        "FROM transactions").fetchall()
    assert all((r["classified_by"], r["rule_id"]) == ("user", None) for r in rows)
    assert all(r["category_id"] == clothes for r in rows)


def test_refiling_within_a_month_leaves_other_months_alone(conn):
    account(conn)
    groceries, clothes = category(conn, "Groceries"), category(conn, "Clothing")
    august = add_txn(conn, 1, "2026-08-02", -1000, "SHOP", groceries)
    july = add_txn(conn, 1, "2026-07-02", -1000, "SHOP", groceries)
    conn.execute("UPDATE transactions SET classified_by = 'rule'")
    conn.commit()

    assert classified.refile(conn, "SHOP", clothes, remember=False,
                             month="2026-08") == 1
    got = {r["id"]: r["category_id"] for r in
           conn.execute("SELECT id, category_id FROM transactions")}
    assert got[august] == clothes and got[july] == groceries


def test_refiling_replaces_a_split(conn):
    account(conn)
    groceries, clothes = category(conn, "Groceries"), category(conn, "Clothing")
    txn = add_txn(conn, 1, "2026-08-02", -10000, "COSTCO", groceries)
    for cat, cents in ((groceries, -6000), (clothes, -4000)):
        conn.execute("INSERT INTO transaction_splits (transaction_id, category_id, "
                     "amount_cents) VALUES (?, ?, ?)", (txn, cat, cents))
    conn.execute("UPDATE transactions SET classified_by = 'rule'")
    conn.commit()

    classified.refile(conn, "COSTCO", clothes, remember=False)
    assert conn.execute("SELECT COUNT(*) FROM transaction_splits").fetchone()[0] == 0
    assert conn.execute("SELECT SUM(amount_cents) FROM txn_allocations WHERE "
                        "category_id = ?", (clothes,)).fetchone()[0] == -10000


# --- the page ----------------------------------------------------------------

def test_page_lists_merchants_and_saving_re_files_them(signed_in):
    conn = open_db()
    groceries, clothes = category(conn, "Groceries"), category(conn, "Clothing")
    classify.create_rule(conn, "TARGET", groceries)
    conn.commit()
    imported(conn, "TARGET STORE", n=3, cents=-5000)

    r = signed_in.get("/classification")
    assert "TARGET STORE" in r.text
    assert "3 transactions" in r.text
    assert "rule “TARGET”" in r.text

    idx = re.search(r'name="mk_(\d+)" value="TARGET STORE"', r.text).group(1)
    signed_in.post("/classification/save", data={
        "csrf": get_csrf(r.text), "f_month": "", "f_account": "", "f_source": "auto",
        f"mk_{idx}": "TARGET STORE", f"was_{idx}": str(groceries),
        f"cat_{idx}": str(clothes), f"remember_{idx}": "1"})

    assert {r["category_id"] for r in conn.execute(
        "SELECT category_id FROM transactions")} == {clothes}
    conn.close()


def test_page_leaves_unchanged_merchants_alone(signed_in):
    conn = open_db()
    groceries = category(conn, "Groceries")
    classify.create_rule(conn, "TARGET", groceries)
    conn.commit()
    imported(conn, "TARGET STORE", n=2)

    r = signed_in.get("/classification")
    idx = re.search(r'name="mk_(\d+)" value="TARGET STORE"', r.text).group(1)
    r2 = signed_in.post("/classification/save", data={
        "csrf": get_csrf(r.text), "f_source": "auto",
        f"mk_{idx}": "TARGET STORE", f"was_{idx}": str(groceries),
        f"cat_{idx}": str(groceries), f"remember_{idx}": "1"},
        follow_redirects=False)
    # nothing changed, so no "re-filed" message and no new rule
    assert "Re-filed" not in r2.headers["location"]
    assert conn.execute("SELECT COUNT(*) FROM rules WHERE pattern = 'TARGET STORE'"
                        ).fetchone()[0] == 0
    conn.close()


def test_page_can_expand_one_merchant_into_its_transactions(signed_in):
    conn = open_db()
    groceries = category(conn, "Groceries")
    classify.create_rule(conn, "TARGET", groceries)
    conn.commit()
    imported(conn, "TARGET STORE", n=2)
    conn.close()

    r = signed_in.get("/classification")
    assert "Show the 2 transactions" in r.text

    r = signed_in.get("/classification?expand=TARGET+STORE")
    assert "Hide the 2 transactions" in r.text
    assert r.text.count("2026-08-0") >= 2


def test_saving_needs_a_csrf_token(signed_in):
    conn = open_db()
    clothes = category(conn, "Clothing")
    imported(conn, "TARGET STORE", n=2)
    r = signed_in.post("/classification/save", data={
        "mk_1": "TARGET STORE", "was_1": "", "cat_1": str(clothes)},
        follow_redirects=False)
    assert r.status_code == 403
    conn.close()
