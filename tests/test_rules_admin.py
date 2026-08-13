"""Searching the rule list, and clearing out rules that can never fire.

Ticking "always file this here" used to add a rule every time, so the list
filled up with copies of the same rule — and only the first of any set is ever
consulted, which makes the rest invisible dead weight.
"""
import pytest

from app import config, db
from app.services import classify

from test_bulk_and_pairing import add_txn, category, get_csrf, open_db  # noqa: F401


@pytest.fixture()
def signed_in(web):
    web.post("/setup", data={"username": "tester", "password": "password12",
                             "password2": "password12"})
    return web


def rules_named(conn, pattern):
    return conn.execute(
        "SELECT id, pattern, category_id, priority, source FROM rules "
        "WHERE pattern = ? COLLATE NOCASE ORDER BY id", (pattern,)).fetchall()


# --- duplicates stop being created --------------------------------------------

def test_teaching_the_same_rule_twice_updates_it(conn):
    groceries, clothes = category(conn, "Groceries"), category(conn, "Clothing")
    first = classify.create_rule(conn, "LIDL", groceries)
    again = classify.create_rule(conn, "LIDL", groceries)
    changed = classify.create_rule(conn, "LIDL", clothes)
    conn.commit()

    assert first == again == changed
    learned = [r for r in rules_named(conn, "LIDL") if r["source"] == "learned"]
    assert len(learned) == 1
    assert learned[0]["category_id"] == clothes    # the latest choice wins


def test_case_and_spacing_do_not_make_a_new_rule(conn):
    groceries = category(conn, "Groceries")
    a = classify.create_rule(conn, "Lidl", groceries)
    b = classify.create_rule(conn, "  LIDL  ", groceries)
    conn.commit()
    assert a == b
    assert len([r for r in rules_named(conn, "LIDL")
                if r["source"] == "learned"]) == 1


def test_a_learned_rule_does_not_overwrite_the_built_in_one(conn):
    """The seed rule stays put; the learned one wins on priority instead."""
    clothes = category(conn, "Clothing")
    seeded = [r for r in rules_named(conn, "LIDL") if r["source"] == "seed"]
    assert seeded, "expected a built-in LIDL rule to exist"

    classify.create_rule(conn, "LIDL", clothes)
    conn.commit()
    still_there = [r for r in rules_named(conn, "LIDL") if r["source"] == "seed"]
    assert len(still_there) == 1
    assert still_there[0]["category_id"] == seeded[0]["category_id"]
    # and the learned one is what actually applies
    assert classify.classify(classify.load_rules(conn), "LIDL LISBOA", "LIDL") == clothes


def test_different_match_types_are_genuinely_different_rules(conn):
    groceries = category(conn, "Groceries")
    a = classify.create_rule(conn, "LIDL", groceries, match_type="contains")
    b = classify.create_rule(conn, "LIDL", groceries, match_type="exact")
    conn.commit()
    assert a != b


# --- finding and clearing the ones already there ------------------------------

def seed_duplicates(conn):
    """The state an existing database is already in. The pattern is one the
    built-in rules don't already cover, so the group is exactly these three."""
    groceries, clothes = category(conn, "Groceries"), category(conn, "Clothing")
    ids = []
    for cat in (groceries, groceries, clothes):
        cur = conn.execute(
            "INSERT INTO rules (pattern, match_type, category_id, priority, source, "
            "created_at) VALUES ('QUINTA VELHA', 'contains', ?, 10, 'learned', 'now')",
            (cat,))
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def test_duplicate_groups_names_the_rule_that_actually_fires(conn):
    ids = seed_duplicates(conn)
    group = next(g for g in classify.duplicate_groups(conn)
                 if g["pattern"] == "QUINTA VELHA")
    assert group["keep"]["id"] == ids[0]           # lowest priority, then lowest id
    assert [d["id"] for d in group["drop"]] == ids[1:]
    assert group["conflicting"] is True            # the third names another category

    # and it agrees with what the engine would do
    rules = classify.load_rules(conn)
    assert classify.matching_rule(rules, "QUINTA VELHA LISBOA",
                                  "QUINTA VELHA")["id"] == ids[0]


def test_identical_duplicates_are_removed_and_conflicting_ones_are_not(conn):
    ids = seed_duplicates(conn)
    assert classify.remove_duplicate_rules(conn) == 1     # only the exact copy

    left = [r["id"] for r in rules_named(conn, "QUINTA VELHA")]
    assert ids[0] in left and ids[1] not in left
    assert ids[2] in left, "a different category is a decision, not noise"


def test_removing_duplicates_changes_nothing_about_classification(conn):
    seed_duplicates(conn)
    before = classify.classify(classify.load_rules(conn), "QUINTA VELHA X", "QUINTA VELHA")
    classify.remove_duplicate_rules(conn, include_conflicting=True)
    after = classify.classify(classify.load_rules(conn), "QUINTA VELHA X", "QUINTA VELHA")
    assert before == after
    assert len(rules_named(conn, "QUINTA VELHA")) == 1


def test_transactions_follow_the_rule_that_survives(conn):
    """Deleting a duplicate must not leave rows pointing at nothing — they would
    read as filed by a rule that no longer exists."""
    conn.execute("INSERT OR IGNORE INTO accounts (id, name, type, created_at) "
                 "VALUES (1, 'a', 'checking', 'now')")
    groceries = category(conn, "Groceries")
    ids = seed_duplicates(conn)
    txn = add_txn(conn, 1, "2026-08-02", -3000, "QUINTA VELHA LISBOA", groceries)
    conn.execute("UPDATE transactions SET classified_by = 'rule', rule_id = ? "
                 "WHERE id = ?", (ids[1], txn))       # filed by the doomed copy
    conn.commit()

    classify.remove_duplicate_rules(conn)
    row = conn.execute("SELECT category_id, rule_id, classified_by FROM transactions "
                       "WHERE id = ?", (txn,)).fetchone()
    assert row["rule_id"] == ids[0]
    assert row["category_id"] == groceries
    assert row["classified_by"] == "rule"


def test_keep_only_resolves_one_conflicting_set(conn):
    ids = seed_duplicates(conn)
    assert classify.keep_only(conn, ids[2]) == 2
    left = [r["id"] for r in rules_named(conn, "QUINTA VELHA")]
    assert left == [ids[2]]


def test_keep_only_ignores_a_rule_with_no_twins(conn):
    groceries = category(conn, "Groceries")
    lone = classify.create_rule(conn, "ONLY ONE OF THESE", groceries)
    conn.commit()
    assert classify.keep_only(conn, lone) == 0
    assert len(rules_named(conn, "ONLY ONE OF THESE")) == 1


def test_a_clean_rule_set_reports_no_duplicates(conn):
    assert classify.duplicate_groups(conn) == []
    assert classify.remove_duplicate_rules(conn) == 0


# --- the page -----------------------------------------------------------------

def test_search_narrows_the_rule_list(signed_in):
    r = signed_in.get("/rules")
    assert "LIDL" in r.text

    r = signed_in.get("/rules?q=LIDL")
    assert "LIDL" in r.text
    assert "NETFLIX" not in r.text
    assert "of 156 rules" in r.text or "of 15" in r.text   # "N of TOTAL rules"

    # searching by category finds every rule that files there
    r = signed_in.get("/rules?q=Groceries")
    assert "LIDL" in r.text
    assert "NETFLIX" not in r.text

    r = signed_in.get("/rules?q=nothing-matches-this")
    assert "No rule matches that search" in r.text


def test_source_filter_separates_taught_rules_from_built_in_ones(signed_in):
    conn = open_db()
    groceries = category(conn, "Groceries")
    classify.create_rule(conn, "MY OWN SHOP", groceries)
    conn.commit()
    conn.close()

    r = signed_in.get("/rules?source=learned")
    assert "MY OWN SHOP" in r.text
    assert "NETFLIX" not in r.text          # a built-in rule

    r = signed_in.get("/rules?source=seed")
    assert "MY OWN SHOP" not in r.text
    assert "NETFLIX" in r.text


def test_page_offers_to_clear_duplicates_and_does(signed_in):
    conn = open_db()
    seed_duplicates(conn)

    r = signed_in.get("/rules")
    assert "duplicate rule" in r.text
    assert "Same pattern, different categories" in r.text

    r = signed_in.post("/rules/dedupe", data={"csrf": get_csrf(r.text)},
                       follow_redirects=True)
    assert "Removed 1 rule" in r.text
    assert len(rules_named(conn, "QUINTA VELHA")) == 2     # the conflict survives
    conn.close()


def test_page_shows_how_many_transactions_each_rule_accounts_for(signed_in):
    conn = open_db()
    groceries = category(conn, "Groceries")
    rule_id = classify.create_rule(conn, "MERCADO", groceries)
    conn.commit()
    for day in (1, 2, 3):
        add_txn(conn, 1, f"2026-08-0{day}", -1000, "MERCADO CENTRAL")
    classify.apply_rules_to_uncategorized(conn)
    assert classify.rule_usage(conn)[rule_id] == 3
    conn.close()

    r = signed_in.get("/rules?q=MERCADO")
    assert ">3<" in r.text.replace(" ", "").replace("\n", "")


def test_dedupe_needs_a_csrf_token(signed_in):
    conn = open_db()
    seed_duplicates(conn)
    r = signed_in.post("/rules/dedupe", data={}, follow_redirects=False)
    assert r.status_code == 403
    assert len(rules_named(conn, "QUINTA VELHA")) == 3
    conn.close()
