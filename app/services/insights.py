"""Insights: cashflow trend, category trends, top merchants, recurring
charges, anomaly detection.

Everything reads the txn_allocations view, so a split transaction contributes
each part to its own category. Excluded categories (transfers, credit-card
payments) and uncategorized money are both left out, which keeps these numbers
identical to the dashboard's — the uncategorized backlog is reported on its own
rather than silently inflating income and spending.
"""
from dataclasses import dataclass

from .budgets import shift_month

_COUNTED = "a.category_id IN (SELECT id FROM categories WHERE excluded = 0)"


def months_back(month: str, n: int) -> list[str]:
    """n months ending at `month`, oldest first."""
    return [shift_month(month, -(n - 1 - i)) for i in range(n)]


def cashflow(conn, month: str, n: int = 12) -> list[dict]:
    seq = months_back(month, n)
    rows = conn.execute(
        f"""SELECT substr(a.date,1,7) AS m,
               SUM(CASE WHEN a.amount_cents > 0 THEN a.amount_cents ELSE 0 END) AS income,
               SUM(CASE WHEN a.amount_cents < 0 THEN -a.amount_cents ELSE 0 END) AS spent
            FROM txn_allocations a
            WHERE substr(a.date,1,7) >= ? AND substr(a.date,1,7) <= ? AND {_COUNTED}
            GROUP BY m""", (seq[0], seq[-1])).fetchall()
    by_month = {r["m"]: r for r in rows}
    return [{"month": m,
             "income": by_month[m]["income"] if m in by_month else 0,
             "spent": by_month[m]["spent"] if m in by_month else 0}
            for m in seq]


def category_trends(conn, month: str, n: int = 6, top: int = 8) -> list[dict]:
    """Per-category monthly spend for the top spending categories."""
    seq = months_back(month, n)
    rows = conn.execute(
        """SELECT a.category_id, c.name, substr(a.date,1,7) AS m,
                  SUM(-a.amount_cents) AS spent
           FROM txn_allocations a JOIN categories c ON c.id = a.category_id
           JOIN category_groups g ON g.id = c.group_id
           WHERE g.kind = 'expense' AND c.excluded = 0
             AND substr(a.date,1,7) >= ? AND substr(a.date,1,7) <= ?
           GROUP BY a.category_id, m""", (seq[0], seq[-1])).fetchall()
    per_cat: dict[int, dict] = {}
    for r in rows:
        entry = per_cat.setdefault(
            r["category_id"], {"name": r["name"], "months": {}, "total": 0})
        entry["months"][r["m"]] = r["spent"]
        entry["total"] += r["spent"]
    ranked = sorted(per_cat.values(), key=lambda e: -e["total"])[:top]
    return [{"name": e["name"], "total": e["total"],
             "series": [max(0, e["months"].get(m, 0)) for m in seq]} for e in ranked]


def top_merchants(conn, month: str, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        f"""SELECT a.merchant_key AS merchant, COUNT(DISTINCT a.txn_id) AS n,
                   SUM(-a.amount_cents) AS spent
            FROM txn_allocations a
            WHERE substr(a.date,1,7) = ? AND a.amount_cents < 0 AND {_COUNTED}
            GROUP BY a.merchant_key ORDER BY spent DESC LIMIT ?""",
        (month, limit)).fetchall()
    return [dict(r) for r in rows]


def largest_transactions(conn, month: str, limit: int = 8) -> list[dict]:
    """Biggest single charges, shown whole (a split one names its parts)."""
    rows = conn.execute(
        f"""SELECT a.txn_id AS id, a.date, a.description,
                   SUM(a.amount_cents) AS amount_cents,
                   COUNT(DISTINCT a.category_id) AS n_categories,
                   MIN(c.name) AS category
            FROM txn_allocations a LEFT JOIN categories c ON c.id = a.category_id
            WHERE substr(a.date,1,7) = ? AND {_COUNTED}
            GROUP BY a.txn_id HAVING SUM(a.amount_cents) < 0
            ORDER BY SUM(a.amount_cents) ASC LIMIT ?""", (month, limit)).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        if row["n_categories"] > 1:
            row["category"] = f"split across {row['n_categories']} categories"
        out.append(row)
    return out


@dataclass
class Recurring:
    merchant: str
    monthly_cents: int
    months_seen: int
    category: str | None
    last_date: str


def recurring_charges(conn, month: str, lookback: int = 5) -> list[Recurring]:
    """Merchants charging a similar amount in >= 3 of the last months —
    your subscriptions and recurring bills."""
    seq = months_back(month, lookback)
    rows = conn.execute(
        f"""SELECT a.merchant_key AS merchant, substr(a.date,1,7) AS m,
                   SUM(a.amount_cents) AS amount_cents, a.date AS date,
                   MIN(c.name) AS category
            FROM txn_allocations a LEFT JOIN categories c ON c.id = a.category_id
            WHERE substr(a.date,1,7) >= ? AND substr(a.date,1,7) <= ? AND {_COUNTED}
            GROUP BY a.txn_id HAVING SUM(a.amount_cents) < 0
            ORDER BY a.date""", (seq[0], seq[-1])).fetchall()
    grouped: dict[str, list] = {}
    for r in rows:
        if r["merchant"]:
            grouped.setdefault(r["merchant"], []).append(r)

    out: list[Recurring] = []
    for merchant, txns in grouped.items():
        months = {t["m"] for t in txns}
        if len(months) < 3:
            continue
        amounts = sorted(-t["amount_cents"] for t in txns)
        median = amounts[len(amounts) // 2]
        if median <= 0:
            continue
        tolerance = max(200, median * 0.2)
        close_months = {t["m"] for t in txns
                        if abs(-t["amount_cents"] - median) <= tolerance}
        if len(close_months) < 3:
            continue
        out.append(Recurring(
            merchant=merchant, monthly_cents=median, months_seen=len(months),
            category=txns[-1]["category"], last_date=txns[-1]["date"]))
    out.sort(key=lambda r: -r.monthly_cents)
    return out


@dataclass
class Anomaly:
    category_id: int
    category: str
    current: int
    average: int

    @property
    def pct_change(self) -> int:
        return round(100 * (self.current - self.average) / self.average) if self.average else 0


def anomalies(conn, month: str, factor: float = 1.5,
              min_diff_cents: int = 5000) -> list[Anomaly]:
    """Expense categories well above their average of the previous 3 months."""
    prev = months_back(shift_month(month, -1), 3)
    rows = conn.execute(
        """SELECT c.id, c.name, substr(a.date,1,7) AS m, SUM(-a.amount_cents) AS spent
           FROM txn_allocations a JOIN categories c ON c.id = a.category_id
           JOIN category_groups g ON g.id = c.group_id
           WHERE g.kind = 'expense' AND c.excluded = 0
             AND substr(a.date,1,7) >= ? AND substr(a.date,1,7) <= ?
           GROUP BY c.id, m""", (prev[0], month)).fetchall()
    current: dict[int, dict] = {}
    history: dict[int, list[int]] = {}
    names: dict[int, str] = {}
    for r in rows:
        names[r["id"]] = r["name"]
        if r["m"] == month:
            current[r["id"]] = r["spent"]
        elif r["m"] in prev:
            history.setdefault(r["id"], []).append(r["spent"])

    out = []
    for cat_id, spent in current.items():
        hist = history.get(cat_id)
        if not hist:
            continue
        avg = sum(hist) // len(hist)
        if avg > 0 and spent >= avg * factor and spent - avg >= min_diff_cents:
            out.append(Anomaly(category_id=cat_id, category=names[cat_id],
                               current=spent, average=avg))
    out.sort(key=lambda a: -(a.current - a.average))
    return out
