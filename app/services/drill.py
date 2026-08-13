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
                   a.category_id, c.name AS category, ac.name AS account_name
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


def spending_rows(conn, month: str, day: int = 0) -> list[dict]:
    """Spending in a month, optionally only as far through it as `day`.

    The pace card's figures are cumulative to a point in the month — "spent by
    day 13", "same point last month" — so opening one has to stop at the same
    day, or the rows say a bigger number than the row that was clicked.
    """
    where = (f"substr(a.date,1,7) = ? AND a.amount_cents < 0 AND {_EXPENSE_CATS} "
             f"AND a.is_transfer = 0")
    params: list = [month]
    if day:
        where += " AND a.date <= ?"
        params.append(f"{month}-{min(day, 31):02d}")
    return _fetch(conn, where, params)


def income_rows(conn, month: str) -> list[dict]:
    return _fetch(conn,
                  f"substr(a.date,1,7) = ? AND a.amount_cents > 0 AND {_COUNTED}",
                  [month], order="a.amount_cents DESC")


def other_rows(conn, month: str, anchor: str) -> list[dict]:
    """What "Other" on the composition chart is made of, for one month.

    The ranking that decides which categories are rolled up is computed across
    the whole chart, so it depends on the month the chart is anchored at, not
    on the bar being opened — `anchor` is that month.
    """
    from .insights import composition_others
    names = composition_others(conn, anchor)
    if not names:
        return []
    holes = ",".join("?" * len(names))
    return _fetch(conn,
                  f"substr(a.date,1,7) = ? AND a.amount_cents < 0 "
                  f"AND c.name IN ({holes}) AND {_COUNTED}", [month] + names)


def net_rows(conn, month: str) -> list[dict]:
    """Everything behind one month's surplus or deficit.

    Net is income minus spending, so both sides belong here — ordered by size
    regardless of direction, because what moved the month is the big figure
    either way.
    """
    return _fetch(conn, f"substr(a.date,1,7) = ? AND {_COUNTED}", [month],
                  order="ABS(a.amount_cents) DESC")


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
    "category": lambda conn, month, key, day: category_rows(conn, month, key),
    "merchant": lambda conn, month, key, day: merchant_rows(conn, month, key),
    "recurring": lambda conn, month, key, day: recurring_rows(conn, month, key),
    "other": lambda conn, month, key, day: other_rows(conn, month, key or month),
    "spending": lambda conn, month, key, day: spending_rows(conn, month, day),
    "income": lambda conn, month, key, day: income_rows(conn, month),
    "net": lambda conn, month, key, day: net_rows(conn, month),
    "uncategorized": lambda conn, month, key, day: uncategorized_rows(conn, month),
    "unmatched": lambda conn, month, key, day: unmatched_rows(conn, month),
}


def rows_for(conn, kind: str, month: str, key: str = "", day: int = 0) -> list[dict]:
    fetch = KINDS.get(kind)
    return fetch(conn, month, key, day) if fetch else []


def list_link(kind: str, month: str, key: str = "") -> str:
    """Where "see all of these" goes, for rows beyond the preview limit."""
    if kind == "uncategorized":
        return f"/transactions?month={month}&category=uncat"
    if kind in ("merchant", "recurring"):
        month_part = "all" if kind == "recurring" else month
        return f"/transactions?month={month_part}&q={key}"
    return f"/transactions?month={month}"
