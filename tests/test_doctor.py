"""Saying which database this is, before you spend an evening guessing.

Reported: the app was showing a setup form on a machine that had months of
history. Nothing had been deleted — a second instance had resolved to a
different file and created an empty one. The app looked factory-fresh rather
than broken, which is the worst way for it to fail, so it now says out loud
which file it opened and what is in it.
"""
import sqlite3
import subprocess
import sys

from app import config, db


def run_doctor(env_extra):
    """The conftest pins HB_DB_PATH for the whole session, and that outranks
    HB_DATA_DIR — so the child starts from an environment with both cleared."""
    import os
    env = {k: v for k, v in os.environ.items()
           if k not in ("HB_DATA_DIR", "HB_DB_PATH")}
    env.update(env_extra)
    env["PYTHONPATH"] = "."
    return subprocess.run([sys.executable, "-m", "app.doctor"],
                          capture_output=True, text=True, env=env).stdout


def test_it_names_the_file_it_would_open(tmp_path):
    out = run_doctor({"HB_DATA_DIR": str(tmp_path)})
    assert str(tmp_path / "budget.db") in out
    assert "does not exist" in out


def test_it_counts_what_is_in_a_real_one(tmp_path):
    path = tmp_path / "budget.db"
    conn = db.connect(path)
    db.init_db(conn)
    conn.execute("INSERT INTO accounts (name, type, created_at) "
                 "VALUES ('Chase', 'checking', 'now')")
    conn.commit()
    conn.close()

    out = run_doctor({"HB_DATA_DIR": str(tmp_path)})
    assert "1 account(s)" in out
    assert f"schema v{db.SCHEMA_VERSION}" in out


def test_it_says_which_rule_picked_the_directory(tmp_path):
    assert "HB_DATA_DIR is set" in run_doctor({"HB_DATA_DIR": str(tmp_path)})
    assert "HB_DB_PATH names the file outright" in run_doctor(
        {"HB_DATA_DIR": str(tmp_path), "HB_DB_PATH": str(tmp_path / "x.db")})


def test_a_corrupt_database_is_reported_not_raised(tmp_path):
    """A file that isn't a database at all is exactly when you need the report
    to still print."""
    (tmp_path / "budget.db").write_text("this is not a database")
    out = run_doctor({"HB_DATA_DIR": str(tmp_path)})
    assert "unreadable" in out


def test_the_startup_banner_names_the_database_and_warns_when_empty(tmp_path, caplog):
    """The service and the terminal can open different files; the log is where
    you find out which one you are looking at."""
    import logging
    from app import main as app_main

    conn = db.connect(tmp_path / "budget.db")
    db.init_db(conn)
    with caplog.at_level(logging.WARNING):
        app_main._announce(conn)
    conn.close()

    text = caplog.text
    assert "HouseBudget data:" in text
    assert "this database is empty" in text
    assert "HB_DATA_DIR" in text


def test_the_banner_stays_quiet_once_there_is_a_user(tmp_path, caplog):
    import logging
    from app import main as app_main

    conn = db.connect(tmp_path / "budget.db")
    db.init_db(conn)
    conn.execute("INSERT INTO users (username, display_name, password_hash, "
                 "created_at) VALUES ('a', 'a', 'x', 'now')")
    conn.commit()
    with caplog.at_level(logging.WARNING):
        app_main._announce(conn)
    conn.close()

    assert "1 user(s)" in caplog.text
    assert "this database is empty" not in caplog.text
