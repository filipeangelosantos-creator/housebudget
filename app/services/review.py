"""What still needs a human decision.

The count on the dashboard and the rows in the review queue must come from the
same definition, or the dashboard sends you to a page that says there is
nothing to do.
"""
from datetime import date

# Money already matched to the other side of an internal transfer is accounted
# for; it needs no category and must not be counted as outstanding.
UNPAIRED = ("NOT EXISTS (SELECT 1 FROM transfer_links l "
            "WHERE l.out_txn_id = t.id OR l.in_txn_id = t.id)")

NEEDS_CATEGORY = f"t.category_id IS NULL AND {UNPAIRED}"


def needs_category_count(conn, month: str | None = None) -> int:
    where = NEEDS_CATEGORY
    params: list = []
    if month:
        where += " AND substr(t.date, 1, 7) = ?"
        params.append(month)
    return conn.execute(
        f"SELECT COUNT(*) FROM transactions t WHERE {where}", params).fetchone()[0]


def needs_category_rows(conn, limit: int = 50) -> list:
    return conn.execute(
        f"SELECT t.id, t.date, t.description, t.amount_cents, t.merchant_key, "
        f"a.name AS account_name FROM transactions t "
        f"JOIN accounts a ON a.id = t.account_id "
        f"WHERE {NEEDS_CATEGORY} ORDER BY t.date DESC, t.id DESC LIMIT ?",
        (limit,)).fetchall()


def future_dated(conn, today: date | None = None) -> list[dict]:
    """Transactions dated after today, grouped by month.

    A statement cannot contain next year's spending, so these are almost always
    a misread year — and they quietly distort every total until found.
    """
    cutoff = (today or date.today()).isoformat()
    rows = conn.execute(
        "SELECT substr(date, 1, 7) AS month, COUNT(*) AS n, "
        "       MIN(date) AS first_date, MAX(date) AS last_date "
        "FROM transactions WHERE date > ? GROUP BY month ORDER BY month",
        (cutoff,)).fetchall()
    return [dict(r) for r in rows]
