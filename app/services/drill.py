"""The transactions behind a number on the insights page.

Every figure there is an aggregate, and an aggregate you can't open is a claim
you have to take on trust — "Home Maintenance is 712% above usual" is only
useful once you can see which four charges did that.

Each kind here matches how the corresponding number was computed, so the rows
add up to the figure shown. In particular the money ones read txn_allocations,
which is what every aggregate on that page reads: one row per unsplit
transaction, one row per part of a split.
"""
from .insights import _COUNTED, _EXPENSE_CATS

LIMIT = 60


def _fetch(conn, where: str, params: list, order: str = "a.amount_cents") -> list[dict]:
    rows = conn.execute(
        f"""SELECT a.txn_id, a.date, a.description, a.amount_cents, a.is_split,
                   c.name AS category, ac.name AS account_name
            FROM txn_allocations a
            LEFT JOIN categories c ON c.id = a.category_id
            JOIN accounts ac ON ac.id = a.account_id
            WHERE {where}
            ORDER BY {order} LIMIT ?""", params + [LIMIT]).fetchall()
    return [dict(r) for r in rows]


def category_rows(conn, month: str, name: str) -> list[dict]:
    return _fetch(conn,
                  f"substr(a.date,1,7) = ? AND c.name = ? AND {_COUNTED}",
                  [month, name])


def merchant_rows(conn, month: str, key: str) -> list[dict]:
    return _fetch(conn,
                  f"substr(a.date,1,7) = ? AND a.merchant_key = ? AND {_COUNTED}",
                  [month, key])


def recurring_rows(conn, month: str, key: str) -> list[dict]:
    """A subscription is interesting across months, not just this one."""
    return _fetch(conn, f"a.merchant_key = ? AND {_COUNTED}", [key],
                  order="a.date DESC")


def spending_rows(conn, month: str) -> list[dict]:
    return _fetch(conn,
                  f"substr(a.date,1,7) = ? AND a.amount_cents < 0 AND {_EXPENSE_CATS} "
                  f"AND a.is_transfer = 0", [month])


def income_rows(conn, month: str) -> list[dict]:
    return _fetch(conn,
                  f"substr(a.date,1,7) = ? AND a.amount_cents > 0 AND {_COUNTED}",
                  [month], order="a.amount_cents DESC")


def uncategorized_rows(conn, month: str) -> list[dict]:
    return _fetch(conn, "substr(a.date,1,7) = ? AND a.category_id IS NULL", [month])


def unmatched_rows(conn, month: str) -> list[dict]:
    """Transfers and card payments whose other side was never imported."""
    return _fetch(
        conn,
        "substr(a.date,1,7) = ? AND a.is_transfer = 0 "
        "AND a.category_id IN (SELECT id FROM categories WHERE excluded = 1)",
        [month])


KINDS = {
    "category": category_rows,
    "merchant": merchant_rows,
    "recurring": recurring_rows,
    "spending": lambda conn, month, key: spending_rows(conn, month),
    "income": lambda conn, month, key: income_rows(conn, month),
    "uncategorized": lambda conn, month, key: uncategorized_rows(conn, month),
    "unmatched": lambda conn, month, key: unmatched_rows(conn, month),
}


def rows_for(conn, kind: str, month: str, key: str = "") -> list[dict]:
    fetch = KINDS.get(kind)
    return fetch(conn, month, key) if fetch else []


def list_link(kind: str, month: str, key: str = "") -> str:
    """Where "see all of these" goes, for rows beyond the preview limit."""
    if kind == "uncategorized":
        return f"/transactions?month={month}&category=uncat"
    if kind in ("merchant", "recurring"):
        month_part = "all" if kind == "recurring" else month
        return f"/transactions?month={month_part}&q={key}"
    return f"/transactions?month={month}"
