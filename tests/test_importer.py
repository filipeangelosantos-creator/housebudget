from app.parsing.statements import load_statement
from app.services import importer
from tests.conftest import SAMPLES


def make_account(conn, name="Checking", type_="checking"):
    cur = conn.execute(
        "INSERT INTO accounts (name, type, created_at) VALUES (?, ?, 'now')",
        (name, type_))
    conn.commit()
    return cur.lastrowid


def make_user(conn):
    cur = conn.execute(
        "INSERT INTO users (username, display_name, password_hash, created_at) "
        "VALUES ('t', 't', 'x', 'now')")
    conn.commit()
    return cur.lastrowid


def test_import_and_reimport_dedupes(conn):
    acct = make_account(conn)
    user = make_user(conn)
    data = (SAMPLES / "sample-checking.csv").read_bytes()
    stmt = load_statement("sample-checking.csv", data)

    r1 = importer.commit_import(conn, acct, "s.csv", data, stmt.parsed, user)
    assert r1.added == 14 and r1.duplicates == 0
    assert r1.categorized >= 9   # payroll, walmart, starbucks x2, shell, netflix...

    # Same-day identical purchases both imported (two identical Starbucks rows
    # appear on different days here, so craft an explicit same-day duplicate)
    stmt2 = load_statement("sample-checking.csv", data)
    r2 = importer.commit_import(conn, acct, "s.csv", data, stmt2.parsed, user)
    assert r2.added == 0 and r2.duplicates == 14


def test_same_day_identical_rows_both_import(conn):
    acct = make_account(conn)
    user = make_user(conn)
    csv_text = ("Date,Description,Amount\n"
                "2026-08-01,COFFEE SPOT,-4.50\n"
                "2026-08-01,COFFEE SPOT,-4.50\n")
    data = csv_text.encode()
    stmt = load_statement("x.csv", data)
    r = importer.commit_import(conn, acct, "x.csv", data, stmt.parsed, user)
    assert r.added == 2

    # importing the overlapping file again skips both
    stmt2 = load_statement("x.csv", data)
    r2 = importer.commit_import(conn, acct, "x.csv", data, stmt2.parsed, user)
    assert r2.added == 0 and r2.duplicates == 2


def test_ofx_fitid_dedupe(conn):
    acct = make_account(conn)
    user = make_user(conn)
    data = (SAMPLES / "sample-bank.ofx").read_bytes()
    stmt = load_statement("b.ofx", data)
    r1 = importer.commit_import(conn, acct, "b.ofx", data, stmt.parsed, user)
    assert r1.added == 3
    r2 = importer.commit_import(conn, acct, "b.ofx", data,
                                load_statement("b.ofx", data).parsed, user)
    assert r2.added == 0 and r2.duplicates == 3


def test_delete_import_rolls_back(conn):
    acct = make_account(conn)
    user = make_user(conn)
    data = (SAMPLES / "sample-checking.csv").read_bytes()
    stmt = load_statement("s.csv", data)
    r = importer.commit_import(conn, acct, "s.csv", data, stmt.parsed, user)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 14
    importer.delete_import(conn, r.import_id)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_preview_stats(conn):
    acct = make_account(conn)
    data = (SAMPLES / "sample-checking.csv").read_bytes()
    stmt = load_statement("s.csv", data)
    stats = importer.preview_stats(conn, acct, stmt.parsed)
    assert stats.ok == 14 and stats.duplicates == 0
    assert stats.date_min == "2026-08-01" and stats.date_max == "2026-08-11"
