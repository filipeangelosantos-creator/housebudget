import pytest

from app.parsing.statements import load_statement
from app.services import accounts, importer
from tests.conftest import SAMPLES


def make_account(conn, name, type_="credit"):
    cur = conn.execute(
        "INSERT INTO accounts (name, type, created_at) VALUES (?, ?, 'now')",
        (name, type_))
    conn.commit()
    return cur.lastrowid


def make_user(conn):
    cur = conn.execute(
        "INSERT INTO users (username, display_name, password_hash, created_at) "
        "VALUES ('u', 'u', 'x', 'now')")
    conn.commit()
    return cur.lastrowid


def import_sample(conn, account_id, user_id, name="sample-checking.csv"):
    data = (SAMPLES / name).read_bytes()
    return importer.commit_import(conn, account_id, name, data,
                                  load_statement(name, data).parsed, user_id)


def test_delete_an_empty_duplicate(conn):
    keep = make_account(conn, "Citizens Master")
    dupe = make_account(conn, "Citizens Master")
    accounts.delete_account(conn, dupe)
    names = [r["id"] for r in conn.execute("SELECT id FROM accounts").fetchall()]
    assert names == [keep]


def test_delete_refuses_when_it_would_lose_transactions(conn):
    user = make_user(conn)
    acct = make_account(conn, "Citizens Master")
    import_sample(conn, acct, user)
    with pytest.raises(accounts.AccountError, match="still has transactions"):
        accounts.delete_account(conn, acct)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 14


def test_merge_moves_transactions_and_removes_the_duplicate(conn):
    user = make_user(conn)
    keep = make_account(conn, "Citizens Master")
    dupe = make_account(conn, "Citizens Master")
    import_sample(conn, dupe, user)

    result = accounts.merge_accounts(conn, dupe, keep)
    assert result == {"moved": 14, "duplicates_removed": 0}
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE account_id = ?",
        (keep,)).fetchone()[0] == 14
    # the import history follows, so undo still works
    assert conn.execute("SELECT account_id FROM imports").fetchone()[0] == keep


def test_merging_the_same_statement_does_not_double_it(conn):
    """The likely real case: the same statement was imported into both halves
    of an accidental duplicate."""
    user = make_user(conn)
    keep = make_account(conn, "Citizens Master")
    dupe = make_account(conn, "Citizens Master")
    import_sample(conn, keep, user)
    import_sample(conn, dupe, user)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 28

    result = accounts.merge_accounts(conn, dupe, keep)
    assert result == {"moved": 0, "duplicates_removed": 14}
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 14


def test_merge_keeps_extra_copies_the_target_lacks(conn):
    """A genuine second purchase of the same amount on the same day is not a
    duplicate and must survive the merge."""
    user = make_user(conn)
    keep = make_account(conn, "Card")
    dupe = make_account(conn, "Card copy")
    csv = b"Date,Description,Amount\n2026-08-01,COFFEE SPOT,-4.50\n"
    twice = csv + b"2026-08-01,COFFEE SPOT,-4.50\n"
    importer.commit_import(conn, keep, "a.csv", csv,
                           load_statement("a.csv", csv).parsed, user)
    importer.commit_import(conn, dupe, "b.csv", twice,
                           load_statement("b.csv", twice).parsed, user)

    result = accounts.merge_accounts(conn, dupe, keep)
    assert result == {"moved": 1, "duplicates_removed": 1}
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2


def test_merged_transactions_still_dedupe_on_reimport(conn):
    """A moved transaction must keep its identity in the new account, or the
    next import of that statement would duplicate everything."""
    user = make_user(conn)
    keep = make_account(conn, "Card")
    dupe = make_account(conn, "Card copy")
    import_sample(conn, dupe, user)
    accounts.merge_accounts(conn, dupe, keep)

    again = import_sample(conn, keep, user)
    assert again.added == 0 and again.duplicates == 14


def test_merge_into_itself_is_refused(conn):
    acct = make_account(conn, "Card")
    with pytest.raises(accounts.AccountError, match="different account"):
        accounts.merge_accounts(conn, acct, acct)
