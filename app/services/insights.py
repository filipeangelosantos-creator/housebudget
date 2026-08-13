"""Insights: cashflow trend, category trends, top merchants, recurring
charges, anomaly detection.

Everything reads the txn_allocations view, so a split transaction contributes
each part to its own category. Excluded categories (transfers, credit-card
payments) and uncategorized money are both left out, which keeps these numbers
identical to the dashboard's — the uncategorized backlog is reported on its own
rather than silently inflating income and spending.
"""
import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

from .budgets import expense_budget_total, shift_month

_EXPENSE_CATS = ("a.category_id IN (SELECT c.id FROM categories c "
                 "JOIN category_groups g ON g.id = c.group_id "
                 "WHERE g.kind = 'expense' AND c.excluded = 0)")

_COUNTED = ("a.is_transfer = 0 "
            "AND a.category_id IN (SELECT id FROM categories WHERE excluded = 0)")


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
           WHERE g.kind = 'expense' AND c.excluded = 0 AND a.is_transfer = 0
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


def days_in_month(month: str) -> int:
    return calendar.monthrange(int(month[:4]), int(month[5:7]))[1]


def _daily_spend(conn, month: str) -> dict[int, int]:
    rows = conn.execute(
        f"""SELECT CAST(substr(a.date, 9, 2) AS INTEGER) AS day,
                   SUM(-a.amount_cents) AS spent
            FROM txn_allocations a
            WHERE substr(a.date,1,7) = ? AND a.amount_cents < 0
              AND a.is_transfer = 0 AND {_EXPENSE_CATS}
            GROUP BY day""", (month,)).fetchall()
    return {r["day"]: r["spent"] for r in rows}


def _cumulative(daily: dict[int, int], days: int) -> list[int]:
    out, running = [], 0
    for d in range(1, days + 1):
        running += daily.get(d, 0)
        out.append(running)
    return out


def spending_pace(conn, month: str, today: date | None = None) -> dict:
    """Cumulative spend through the month, against last month and budget pace.

    Answers 'are we ahead of where we were?' partway through a month, which a
    month-end total can't tell you. `today` is injectable so the projection is
    testable without depending on the wall clock.
    """
    prev = shift_month(month, -1)
    days, prev_days = days_in_month(month), days_in_month(prev)
    this_series = _cumulative(_daily_spend(conn, month), days)
    prev_series = _cumulative(_daily_spend(conn, prev), prev_days)

    budget = expense_budget_total(conn, month)

    today = today or date.today()
    elapsed = today.day if month == today.strftime("%Y-%m") else days
    elapsed = max(1, min(elapsed, days))
    spent = this_series[elapsed - 1] if this_series else 0
    prev_same_day = prev_series[min(elapsed, prev_days) - 1] if prev_series else 0
    # Straight-line budget pace: where you'd be if spending evenly.
    on_pace = round(budget * elapsed / days) if budget else 0
    projected = round(spent * days / elapsed) if elapsed else 0

    return {"days": days, "elapsed": elapsed, "this": this_series,
            "prev": prev_series, "prev_days": prev_days, "budget": budget,
            "spent": spent, "prev_same_day": prev_same_day, "on_pace": on_pace,
            "projected": projected, "month": month, "prev_month": prev}


def monthly_net(conn, month: str, n: int = 12) -> list[dict]:
    """Income minus spending per month — surplus or deficit at a glance."""
    seq = months_back(month, n)
    rows = conn.execute(
        f"""SELECT substr(a.date,1,7) AS m, SUM(a.amount_cents) AS net
            FROM txn_allocations a
            WHERE substr(a.date,1,7) >= ? AND substr(a.date,1,7) <= ? AND {_COUNTED}
            GROUP BY m""", (seq[0], seq[-1])).fetchall()
    by_month = {r["m"]: r["net"] for r in rows}
    return [{"month": m, "net": by_month.get(m, 0)} for m in seq]


def category_composition(conn, month: str, n: int = 12, top: int = 6) -> dict:
    """Monthly spend split by the biggest categories, everything else as Other."""
    seq = months_back(month, n)
    rows = conn.execute(
        f"""SELECT c.name, substr(a.date,1,7) AS m, SUM(-a.amount_cents) AS spent
            FROM txn_allocations a JOIN categories c ON c.id = a.category_id
            WHERE substr(a.date,1,7) >= ? AND substr(a.date,1,7) <= ?
              AND a.amount_cents < 0 AND a.is_transfer = 0 AND {_EXPENSE_CATS}
            GROUP BY c.id, m""", (seq[0], seq[-1])).fetchall()
    totals: dict[str, int] = {}
    per_month: dict[str, dict[str, int]] = {}
    for r in rows:
        totals[r["name"]] = totals.get(r["name"], 0) + r["spent"]
        per_month.setdefault(r["name"], {})[r["m"]] = r["spent"]
    ranked = sorted(totals, key=lambda k: -totals[k])
    keep, rest = ranked[:top], ranked[top:]

    # NB: the per-month list is called "monthly", not "values" — Jinja resolves
    # `series.values` to dict.values() and would silently render nothing.
    series = [{"name": name, "total": totals[name],
               "monthly": [max(0, per_month[name].get(m, 0)) for m in seq]}
              for name in keep]
    if rest:
        other = [sum(max(0, per_month[name].get(m, 0)) for name in rest) for m in seq]
        if any(other):
            series.append({"name": "Other", "total": sum(totals[n] for n in rest),
                           "monthly": other})
    return {"months": seq, "series": series}


@dataclass
class PayStream:
    """A recurring income source and how often it lands."""
    name: str
    category: str
    cadence: str                 # weekly | biweekly | semimonthly | monthly
    typical_cents: int           # what one payday is worth, best estimate
    last_date: date
    days_of_month: list[int]
    recent: list[tuple[date, int]] = field(default_factory=list)  # newest first
    low_cents: int = 0
    high_cents: int = 0

    @property
    def varies(self) -> bool:
        """Whether the amount moves enough to be worth saying out loud.

        Almost no real pay is identical every time — overtime, a bonus, a tax
        band changing. A couple of pounds either way is noise; a tenth of the
        cheque is a range you should budget against, not a single figure.
        """
        spread = self.high_cents - self.low_cents
        return spread > max(500, self.typical_cents // 20)

    @property
    def cadence_label(self) -> str:
        return {"weekly": "every week", "biweekly": "every 2 weeks",
                "semimonthly": "twice a month",
                "monthly": "monthly"}.get(self.cadence, self.cadence)

    def paydays_in(self, month: str) -> list[date]:
        """Which days of `month` this stream is expected to pay on."""
        year, mon = int(month[:4]), int(month[5:7])
        last_day = calendar.monthrange(year, mon)[1]
        first, last = date(year, mon, 1), date(year, mon, last_day)

        if self.cadence in ("semimonthly", "monthly"):
            return [date(year, mon, min(d, last_day))
                    for d in sorted(set(self.days_of_month))]

        step = timedelta(days=7 if self.cadence == "weekly" else 14)
        # Walk right past the start of the month, then step back in — stopping
        # as soon as the cursor is inside would miss the earlier paydays.
        cursor = self.last_date
        while cursor >= first:
            cursor -= step
        while cursor < first:
            cursor += step
        out = []
        while cursor <= last:
            out.append(cursor)
            cursor += step
        return out


def _classify_cadence(dates: list[date]) -> tuple[str, list[int]] | None:
    if len(dates) < 3:
        return None
    gaps = sorted((dates[i + 1] - dates[i]).days for i in range(len(dates) - 1))
    median = gaps[len(gaps) // 2]
    days = [d.day for d in dates]
    if 5 <= median <= 9:
        return "weekly", days
    if 12 <= median <= 18:
        # Twice-monthly pay lands on the same two dates each month; fortnightly
        # pay drifts through the month. Both average about a fortnight.
        distinct = sorted(set(days))
        clustered = len(distinct) <= 2 or (
            len(distinct) <= 4 and max(distinct) - min(distinct) > 20
            and all(min(abs(d - a) for a in (distinct[0], distinct[-1])) <= 2
                    for d in days))
        if clustered:
            return "semimonthly", sorted({distinct[0], distinct[-1]})
        return "biweekly", days
    if 25 <= median <= 35:
        return "monthly", [max(set(days), key=days.count)]
    return None


def _median(values: list[int]) -> int:
    """Lower-middle median, in whole cents. Robust to one odd cheque."""
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2] if ordered else 0


RECENT_PAYDAYS = 6      # window the range is drawn from
TRACKING_PAYDAYS = 3    # window the estimate is drawn from


def pay_streams(conn, month: str, lookback: int = 9) -> list[PayStream]:
    """Recurring income, with how often each one pays and how much.

    Salary paid fortnightly lands three times in some months and twice in
    others, so a flat monthly income figure is always wrong for one of them.

    The amount is never quite the same twice either, so each payday is
    estimated from the last few rather than from the whole history: a raise
    six months ago should not still be dragging the figure down. The spread
    over a longer window is kept alongside, because "about X, between A and B"
    is the honest answer and a single figure is not.
    """
    seq = months_back(month, lookback)
    rows = conn.execute(
        """SELECT a.merchant_key, a.date, a.amount_cents, c.name AS category
           FROM txn_allocations a
           JOIN categories c ON c.id = a.category_id
           JOIN category_groups g ON g.id = c.group_id
           WHERE g.kind = 'income' AND c.excluded = 0 AND a.is_transfer = 0
             AND a.amount_cents > 0
             AND substr(a.date,1,7) >= ? AND substr(a.date,1,7) <= ?
           ORDER BY a.date""", (seq[0], month)).fetchall()

    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(r["merchant_key"], []).append(r)

    streams: list[PayStream] = []
    for merchant, entries in grouped.items():
        # Per payday, not per transaction: two deposits landing the same day
        # (both salaries from one employer, or a cheque paid in two parts) are
        # one payday's money, and averaging them halves the estimate.
        per_day: dict[date, int] = {}
        for e in entries:
            day = date.fromisoformat(e["date"])
            per_day[day] = per_day.get(day, 0) + e["amount_cents"]
        dates = sorted(per_day)
        cadence = _classify_cadence(dates)
        if cadence is None:
            continue
        recent_dates = dates[-RECENT_PAYDAYS:]
        window = [per_day[d] for d in recent_dates]
        streams.append(PayStream(
            name=merchant.title(), category=entries[-1]["category"],
            cadence=cadence[0],
            typical_cents=_median(window[-TRACKING_PAYDAYS:]),
            last_date=dates[-1], days_of_month=cadence[1],
            recent=[(d, per_day[d]) for d in reversed(recent_dates)],
            low_cents=min(window), high_cents=max(window)))
    streams.sort(key=lambda s: -s.typical_cents)
    return streams


def expected_income(conn, month: str, today: date | None = None) -> dict:
    """What recurring income should total this month, given when it lands."""
    streams = pay_streams(conn, month)
    detail = []
    total = low = high = 0
    for stream in streams:
        days = stream.paydays_in(month)
        if not days:
            continue
        amount = stream.typical_cents * len(days)
        total += amount
        low += stream.low_cents * len(days)
        high += stream.high_cents * len(days)
        detail.append({"stream": stream, "paydays": days, "expected": amount,
                       "low": stream.low_cents * len(days),
                       "high": stream.high_cents * len(days)})
    received = conn.execute(
        """SELECT COALESCE(SUM(a.amount_cents), 0) AS total FROM txn_allocations a
           JOIN categories c ON c.id = a.category_id
           JOIN category_groups g ON g.id = c.group_id
           WHERE g.kind = 'income' AND c.excluded = 0 AND a.is_transfer = 0
             AND a.amount_cents > 0 AND substr(a.date,1,7) = ?""",
        (month,)).fetchone()["total"]
    today = today or date.today()
    upcoming = [d for entry in detail for d in entry["paydays"] if d > today]
    return {"total": total, "low": low, "high": high, "detail": detail,
            "received": received, "upcoming": sorted(upcoming),
            "varies": any(e["stream"].varies for e in detail)}


@dataclass
class Mover:
    category: str
    current: int
    average: int

    @property
    def change(self) -> int:
        return self.current - self.average

    @property
    def pct_change(self) -> int:
        return round(100 * self.change / self.average) if self.average else 0


def biggest_movers(conn, month: str, limit: int = 6,
                   min_change_cents: int = 2000) -> list[Mover]:
    """Categories that moved most against their 3-month average, up or down."""
    prev = months_back(shift_month(month, -1), 3)
    rows = conn.execute(
        f"""SELECT c.name, substr(a.date,1,7) AS m, SUM(-a.amount_cents) AS spent
            FROM txn_allocations a JOIN categories c ON c.id = a.category_id
            WHERE substr(a.date,1,7) >= ? AND substr(a.date,1,7) <= ?
              AND a.is_transfer = 0 AND {_EXPENSE_CATS}
            GROUP BY c.id, m""", (prev[0], month)).fetchall()
    current: dict[str, int] = {}
    history: dict[str, list[int]] = {}
    for r in rows:
        if r["m"] == month:
            current[r["name"]] = r["spent"]
        elif r["m"] in prev:
            history.setdefault(r["name"], []).append(r["spent"])

    movers = []
    for name in set(current) | set(history):
        hist = history.get(name, [])
        if not hist:
            continue
        avg = sum(hist) // len(hist)
        cur = current.get(name, 0)
        if abs(cur - avg) >= min_change_cents:
            movers.append(Mover(category=name, current=cur, average=avg))
    movers.sort(key=lambda m: -abs(m.change))
    return movers[:limit]


def year_over_year(conn, month: str) -> dict | None:
    """This month against the same month a year ago, when that data exists."""
    last_year = shift_month(month, -12)
    rows = conn.execute(
        f"""SELECT substr(a.date,1,7) AS m,
                   SUM(CASE WHEN a.amount_cents > 0 THEN a.amount_cents ELSE 0 END) AS income,
                   SUM(CASE WHEN a.amount_cents < 0 THEN -a.amount_cents ELSE 0 END) AS spent
            FROM txn_allocations a
            WHERE substr(a.date,1,7) IN (?, ?) AND {_COUNTED}
            GROUP BY m""", (month, last_year)).fetchall()
    by_month = {r["m"]: r for r in rows}
    if last_year not in by_month or month not in by_month:
        return None
    now, then = by_month[month], by_month[last_year]
    if not then["spent"]:
        return None
    return {"month": month, "last_year": last_year,
            "spent_now": now["spent"], "spent_then": then["spent"],
            "income_now": now["income"], "income_then": then["income"],
            "spent_pct": round(100 * (now["spent"] - then["spent"]) / then["spent"])}


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
           WHERE g.kind = 'expense' AND c.excluded = 0 AND a.is_transfer = 0
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
