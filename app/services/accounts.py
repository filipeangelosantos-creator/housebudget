"""Removing and merging accounts.

Creating the same account twice during an import is easy to do, and leaves a
duplicate in every account picker. An empty duplicate can just be deleted; one
that already holds transactions has to be merged, and merging has to cope with
the same statement having been imported into both.
"""
from collections import Counter

from . import importer


class AccountError(ValueError):
    """Raised when a delete or merge would lose or confuse data."""


def transaction_count(conn, account_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id = ?",
                        (account_id,)).fetchone()[0]


def delete_account(conn, account_id: int) -> None:
    """Remove an account that holds nothing."""
    if transaction_count(conn, account_id):
        raise AccountError(
            "This account still has transactions. Merge it into another "
            "account instead, or delete its imports first.")
    conn.execute("DELETE FROM import_profiles WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM imports WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()


def merge_accounts(conn, source_id: int, target_id: int) -> dict:
    """Move everything from `source` into `target`, then delete `source`.

    Transactions the target already has are dropped rather than moved, so
    merging two accounts that each received the same statement leaves one copy
    of each transaction. Moved transactions are re-keyed to their identity in
    the target account, so re-importing that statement later still recognises
    them as duplicates.
    """
    if source_id == target_id:
        raise AccountError("Pick a different account to merge into.")
    for acc_id in (source_id, target_id):
        if not conn.execute("SELECT 1 FROM accounts WHERE id = ?",
                            (acc_id,)).fetchone():
            raise AccountError("That account no longer exists.")

    def key_of(row):
        return (row["date"], row["amount_cents"], row["normalized_desc"])

    existing = Counter()
    for row in conn.execute(
            "SELECT date, amount_cents, normalized_desc FROM transactions "
            "WHERE account_id = ?", (target_id,)).fetchall():
        existing[key_of(row)] += 1

    moved = duplicates = 0
    seen = Counter()
    rows = conn.execute(
        "SELECT id, date, amount_cents, normalized_desc, fitid FROM transactions "
        "WHERE account_id = ? ORDER BY date, id", (source_id,)).fetchall()
    for row in rows:
        key = key_of(row)
        seen[key] += 1
        if seen[key] <= existing[key]:
            # The target already holds this one: it is the same transaction
            # imported into both accounts.
            conn.execute("DELETE FROM transactions WHERE id = ?", (row["id"],))
            duplicates += 1
            continue
        seq = existing[key] + (seen[key] - existing[key]) - 1
        conn.execute(
            "UPDATE transactions SET account_id = ?, dedupe_hash = ? WHERE id = ?",
            (target_id,
             importer.dedupe_hash_for(target_id, row["date"], row["amount_cents"],
                                      row["normalized_desc"], seq, row["fitid"]),
             row["id"]))
        moved += 1

    conn.execute("UPDATE imports SET account_id = ? WHERE account_id = ?",
                 (target_id, source_id))
    # Layout memory: keep the target's when both learned the same statement.
    conn.execute(
        "DELETE FROM import_profiles WHERE account_id = ? AND header_sig IN "
        "(SELECT header_sig FROM import_profiles WHERE account_id = ?)",
        (source_id, target_id))
    conn.execute("UPDATE import_profiles SET account_id = ? WHERE account_id = ?",
                 (target_id, source_id))
    conn.execute("DELETE FROM accounts WHERE id = ?", (source_id,))
    conn.commit()
    return {"moved": moved, "duplicates_removed": duplicates}
