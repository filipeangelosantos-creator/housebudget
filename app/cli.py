"""Maintenance commands (run inside the app environment/container):

    python -m app.cli create-user <username> [display name]
    python -m app.cli reset-password <username>

Both prompt for the password so it never lands in shell history.
"""
import getpass
import sys

from . import auth, config, db
from .services.seed import seed_defaults


def _prompt_password() -> str:
    while True:
        p1 = getpass.getpass("New password (8+ chars): ")
        if len(p1) < 8:
            print("Too short.")
            continue
        p2 = getpass.getpass("Repeat: ")
        if p1 != p2:
            print("Didn't match, try again.")
            continue
        return p1


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1
    config.ensure_dirs()
    conn = db.connect()
    db.init_db(conn)
    seed_defaults(conn)
    cmd = argv[1]

    if cmd == "create-user" and len(argv) >= 3:
        username = argv[2].strip()
        display = " ".join(argv[3:]).strip() or username
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            print(f"User {username!r} already exists.")
            return 1
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, created_at) "
            "VALUES (?, ?, ?, ?)",
            (username, display, auth.hash_password(_prompt_password()), db.utcnow()))
        conn.commit()
        print(f"Created user {username!r}.")
        return 0

    if cmd == "reset-password" and len(argv) >= 3:
        username = argv[2].strip()
        row = conn.execute("SELECT id FROM users WHERE username = ?",
                           (username,)).fetchone()
        if row is None:
            print(f"No user {username!r}.")
            return 1
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (auth.hash_password(_prompt_password()), row["id"]))
        conn.commit()
        print(f"Password updated for {username!r}.")
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
