"""Report which database this install would open, and what is in it.

    python -m app.doctor

Where the data lives is resolved three ways — HB_DATA_DIR, then a ./data
directory if one happens to exist, then the home directory of whoever is
running the process. The last of those means a service and a terminal on the
same machine can open different files, and the app then looks factory-fresh
rather than broken. This prints the answer instead of leaving you to guess it.

Reads only; it never creates a database that isn't already there.
"""
import getpass
import os
import sqlite3
import sys
from pathlib import Path

from . import config


def _describe(path: Path) -> str:
    if not path.exists():
        return "does not exist"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            txns = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as e:
        return f"unreadable ({e})"
    size = path.stat().st_size
    return (f"{size:,} bytes · schema v{version} · {users} user(s), "
            f"{accounts} account(s), {txns} transaction(s)")


def _why() -> str:
    """Which of the three resolution branches picked this directory."""
    if os.environ.get("HB_DB_PATH"):
        return "HB_DB_PATH names the file outright"
    if os.environ.get("HB_DATA_DIR"):
        return "HB_DATA_DIR is set"
    if config.DATA_DIR == config.LEGACY_DATA_DIR:
        return "a data directory exists inside the checkout"
    return ("no HB_DATA_DIR and no ./data directory, so it fell back to the "
            "home directory of whoever started it")


def main() -> int:
    chosen = config.DB_PATH
    print(f"    data directory : {config.DATA_DIR}")
    print(f"    database       : {chosen}")
    print(f"    {_describe(chosen)}")
    print(f"    running as     : {getpass.getuser()}")

    print(f"    chosen because : {_why()}")

    others = [p for p in (
        Path.home() / ".housebudget" / "budget.db",
        config.LEGACY_DATA_DIR / "budget.db",
        Path("C:/Windows/System32/config/systemprofile/.housebudget/budget.db"),
        Path("C:/ProgramData/HouseBudget/budget.db"),
    ) if p.exists() and p.resolve() != chosen.resolve()]
    if others:
        print("\n    Other databases on this machine:")
        for p in others:
            print(f"      {p}\n        {_describe(p)}")
        print("\n    If one of those is the one you have been using, point the"
              "\n    app at it with HB_DATA_DIR and restart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
