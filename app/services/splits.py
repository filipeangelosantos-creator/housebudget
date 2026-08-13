"""Splitting one transaction across categories, plus the "what is this
usually?" suggestion and the mixed-basket warning.

A big-box charge (Costco, Amazon, Target) is one line on the statement but
often several budget categories in reality. The app therefore:
  * suggests the category you have used most often for that merchant,
  * flags such merchants so you confirm rather than silently accept a guess,
  * lets you split the amount across categories when one isn't enough.
"""
from dataclasses import dataclass

# Merchants whose receipts routinely cover several budget categories. Matched
# as substrings against the normalized description / merchant key.
MIXED_BASKET_MERCHANTS = [
    "COSTCO", "WALMART", "WAL-MART", "TARGET", "AMZN", "AMAZON", "SAMS CLUB",
    "BJS WHOLESALE", "MEIJER", "CANADIAN TIRE", "SUPERSTORE", "IKEA",
    "CONTINENTE", "AUCHAN", "CARREFOUR", "JUMBO", "EL CORTE INGLES",
    "TESCO", "SAINSBURY", "ASDA", "KAUFLAND", "LECLERC", "WORTEN",
]

# Only bother asking about amounts worth splitting.
SPLIT_PROMPT_MIN_CENTS = 5000


@dataclass
class Split:
    category_id: int
    amount_cents: int
    note: str = ""


class SplitError(ValueError):
    """Raised when proposed splits don't add up to the transaction."""


def is_mixed_basket_merchant(normalized_desc: str, merchant_key: str) -> bool:
    haystack = f"{normalized_desc} {merchant_key}".upper()
    return any(m in haystack for m in MIXED_BASKET_MERCHANTS)


def is_multi_category_history(conn, merchant_key: str, min_categories: int = 2) -> bool:
    """True when you have already filed this merchant under several categories."""
    n = conn.execute(
        "SELECT COUNT(DISTINCT a.category_id) FROM txn_allocations a "
        "WHERE a.merchant_key = ? AND a.category_id IS NOT NULL",
        (merchant_key,)).fetchone()[0]
    return n >= min_categories


def should_prompt_split(conn, normalized_desc: str, merchant_key: str,
                        amount_cents: int) -> bool:
    if amount_cents >= 0 or abs(amount_cents) < SPLIT_PROMPT_MIN_CENTS:
        return False
    return (is_mixed_basket_merchant(normalized_desc, merchant_key)
            or is_multi_category_history(conn, merchant_key))


def merchant_history(conn, merchant_key: str, exclude_txn_id: int | None = None,
                     limit: int = 4) -> list[dict]:
    """How you have categorized this merchant before, most used first."""
    if not merchant_key:
        return []
    rows = conn.execute(
        "SELECT a.category_id, c.name, g.name AS group_name, "
        "       COUNT(DISTINCT a.txn_id) AS n "
        "FROM txn_allocations a "
        "JOIN categories c ON c.id = a.category_id "
        "JOIN category_groups g ON g.id = c.group_id "
        "WHERE a.merchant_key = ? AND a.category_id IS NOT NULL "
        "  AND a.txn_id != COALESCE(?, -1) AND c.archived = 0 "
        "GROUP BY a.category_id ORDER BY n DESC, c.name LIMIT ?",
        (merchant_key, exclude_txn_id, limit)).fetchall()
    total = sum(r["n"] for r in rows) or 1
    return [{"category_id": r["category_id"], "name": r["name"],
             "group_name": r["group_name"], "count": r["n"],
             "share": round(100 * r["n"] / total)} for r in rows]


def suggest_category(conn, merchant_key: str,
                     exclude_txn_id: int | None = None) -> dict | None:
    """The category you use most for this merchant, if there is a clear one."""
    history = merchant_history(conn, merchant_key, exclude_txn_id)
    return history[0] if history else None


def get_splits(conn, txn_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT s.id, s.category_id, s.amount_cents, s.note, c.name AS category_name, "
        "       g.name AS group_name "
        "FROM transaction_splits s JOIN categories c ON c.id = s.category_id "
        "JOIN category_groups g ON g.id = c.group_id "
        "WHERE s.transaction_id = ? ORDER BY s.sort_order, s.id", (txn_id,)).fetchall()
    return [dict(r) for r in rows]


def has_splits(conn, txn_id: int) -> bool:
    return conn.execute("SELECT 1 FROM transaction_splits WHERE transaction_id = ? LIMIT 1",
                        (txn_id,)).fetchone() is not None


def save_splits(conn, txn_id: int, splits: list[Split]) -> None:
    """Replace this transaction's splits. Must total the transaction amount."""
    txn = conn.execute("SELECT amount_cents FROM transactions WHERE id = ?",
                       (txn_id,)).fetchone()
    if txn is None:
        raise SplitError("Transaction not found.")
    parts = [s for s in splits if s.amount_cents != 0 and s.category_id]
    if len(parts) < 2:
        raise SplitError("A split needs at least two categories with an amount.")
    total = sum(s.amount_cents for s in parts)
    if total != txn["amount_cents"]:
        diff = txn["amount_cents"] - total
        raise SplitError(
            f"Split total is off by {abs(diff) / 100:.2f} — the parts must add up "
            f"to {abs(txn['amount_cents']) / 100:.2f}.")

    conn.execute("DELETE FROM transaction_splits WHERE transaction_id = ?", (txn_id,))
    for i, s in enumerate(parts):
        conn.execute(
            "INSERT INTO transaction_splits (transaction_id, category_id, "
            "amount_cents, note, sort_order) VALUES (?, ?, ?, ?, ?)",
            (txn_id, s.category_id, s.amount_cents, s.note.strip()[:120], i))
    # Keep a representative category on the transaction itself so "is this
    # categorized?" checks and the transaction list still work.
    biggest = max(parts, key=lambda s: abs(s.amount_cents))
    conn.execute(
        "UPDATE transactions SET category_id = ?, needs_review = 0, "
        "classified_by = 'user', rule_id = NULL WHERE id = ?",
        (biggest.category_id, txn_id))
    conn.commit()


def clear_splits(conn, txn_id: int) -> None:
    conn.execute("DELETE FROM transaction_splits WHERE transaction_id = ?", (txn_id,))
    conn.commit()


def confirm_category(conn, txn_id: int, category_id: int | None = None) -> None:
    """Accept the current (or given) category and stop asking about it."""
    if category_id is not None:
        conn.execute(
            "UPDATE transactions SET category_id = ?, needs_review = 0, "
            "classified_by = 'user', rule_id = NULL WHERE id = ?",
            (category_id, txn_id))
    else:
        conn.execute("UPDATE transactions SET needs_review = 0 WHERE id = ?", (txn_id,))
    conn.commit()


def pending_confirmations(conn, limit: int = 50) -> list[dict]:
    """Categorized transactions the app wants a human to confirm or split."""
    rows = conn.execute(
        "SELECT t.id, t.date, t.description, t.amount_cents, t.merchant_key, "
        "       t.category_id, c.name AS category_name, a.name AS account_name "
        "FROM transactions t JOIN accounts a ON a.id = t.account_id "
        "LEFT JOIN categories c ON c.id = t.category_id "
        "WHERE t.needs_review = 1 AND t.category_id IS NOT NULL "
        "ORDER BY ABS(t.amount_cents) DESC, t.date DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def pending_confirmation_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM transactions "
        "WHERE needs_review = 1 AND category_id IS NOT NULL").fetchone()[0]
