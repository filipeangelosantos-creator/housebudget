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
    # The months travel with the series so each bar can say which one it is.
    return [{"name": e["name"], "total": e["total"], "months": seq,
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
            # The names travel with it so the bar can be opened: "Other" is a
            # real figure made of real transactions, and the one segment you
            # can't look inside is the one you can't account for.
            series.append({"name": "Other", "total": sum(totals[n] for n in rest),
                           "monthly": other, "members": rest})
    return {"months": seq, "series": series}


def composition_others(conn, month: str, n: int = 12, top: int = 6) -> list[str]:
    """The categories rolled into "Other" for a chart anchored at `month`."""
    for s in category_composition(conn, month, n, top)["series"]:
        if s["name"] == "Other":
            return s["members"]
    return []


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
    # Set when the dates come from a schedule you declared rather than from
    # walking the fortnight forward off the last payslip.
    declared_dates: list[date] | None = None

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
        if self.declared_dates is not None:
            return self.declared_dates
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
    from . import schedules as sched

    # A declared income schedule replaces the detected stream for that
    # category. Cadence read off a statement is a good guess and no more, and
    # guessing wrong is exactly the "third payday that isn't" problem.
    declared = [s for s in sched.all_schedules(conn)
                if s.kind == "income" and s.amount_cents]
    claimed = {s.category for s in declared}
    streams = [s for s in pay_streams(conn, month) if s.category not in claimed]
    for s in declared:
        streams.append(PayStream(
            name=s.category, category=s.category, cadence=s.cadence,
            typical_cents=s.amount_cents, last_date=s.anchor, days_of_month=[],
            recent=[], low_cents=s.amount_cents, high_cents=s.amount_cents,
            declared_dates=s.dates_in(month)))

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


# --- bills that don't arrive every month -------------------------------------

# Bands are wide because real billing dates wander: a "quarterly" bill lands
# 89 days after the last one, then 94, then 87. Anything outside them is
# irregular spending, which is not a bill you can plan for.
CADENCE_BANDS = [
    (50, 74, "every 2 months", 2),
    (75, 115, "quarterly", 3),
    (150, 215, "every 6 months", 6),
    (320, 400, "yearly", 12),
]

# An interval on its own is not a bill. Three visits to a burger place happen
# to fall 80, 95 and 110 days apart and the middle one is "quarterly" — which
# is how a restaurant ended up filed as a bill to set money aside for. What
# separates a bill is that it repeats on a schedule *for the same amount*: the
# gaps agree with each other and so do the charges.
GAP_SPREAD = 0.20            # every gap this close to the middle one
AMOUNT_SPREAD = 0.20         # every charge this close to the typical one
MIN_BILL_CENTS = 3000        # under this, "set money aside" is noise
# Two sightings are one gap, and one gap is not a schedule. For the long
# cadences that is all the evidence there will ever be, so the bar is higher:
# the amounts have to match closely and the bill has to be worth planning for.
LONE_GAP_SPREAD = 0.10
LONE_GAP_MIN_CENTS = 10000


def _all_near(values: list[int], centre: int, spread: float) -> bool:
    return bool(centre) and all(abs(v - centre) <= centre * spread for v in values)


@dataclass
class PeriodicBill:
    """A bill that arrives less often than monthly.

    Budgeting one of these per month is wrong twice over: the months it misses
    look under budget, and the month it lands looks like overspending. What you
    want is the amount to put aside each month, and when it next falls due.
    """
    merchant: str
    category: str | None
    category_id: int | None
    cadence: str
    months_per: int
    typical_cents: int          # what one bill costs
    last_date: date
    next_due: date
    times_seen: int
    declared: bool = False      # you said so, rather than the app working it out

    @property
    def monthly_cents(self) -> int:
        """What to set aside each month to have it ready when it lands."""
        return round(self.typical_cents / self.months_per)

    @property
    def next_due_month(self) -> str:
        return self.next_due.strftime("%Y-%m")

    def due_in(self, month: str) -> bool:
        return self.next_due_month == month


def _median_int(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else 0


def _declared_bills(conn, today: date) -> list[PeriodicBill]:
    """Schedules you set yourself, as bills. These need no evidence at all —
    you said so — which is the point: a quarterly bill is a quarterly bill from
    the first statement, not from the third."""
    from . import schedules as sched
    out = []
    for s in sched.all_schedules(conn):
        if s.kind != "expense" or not s.is_periodic or not s.amount_cents:
            continue
        last = s.next_after(today - timedelta(days=400))
        while True:
            following = s.next_after(last)
            if following > today:
                break
            last = following
        out.append(PeriodicBill(
            merchant=s.category, category=s.category, category_id=s.category_id,
            cadence=s.label, months_per=sched.months_per(s.cadence),
            typical_cents=s.amount_cents, last_date=last,
            next_due=s.next_after(today), times_seen=0, declared=True))
    return out


def periodic_bills(conn, month: str, lookback: int = 24,
                   today: date | None = None) -> list[PeriodicBill]:
    """Recurring charges whose interval is longer than a month.

    Two sightings are enough for the long cadences and three for the short
    ones: a yearly bill only produces a second data point after a year, so
    demanding three would mean never recognising one.
    """
    seq = months_back(month, lookback)
    rows = conn.execute(
        f"""SELECT a.merchant_key AS merchant, a.date AS date,
                   SUM(a.amount_cents) AS amount_cents,
                   MIN(c.name) AS category, MIN(c.id) AS category_id
            FROM txn_allocations a JOIN categories c ON c.id = a.category_id
            JOIN category_groups g ON g.id = c.group_id
            WHERE g.kind = 'expense' AND c.excluded = 0 AND a.is_transfer = 0
              AND substr(a.date,1,7) >= ? AND substr(a.date,1,7) <= ?
            GROUP BY a.txn_id HAVING SUM(a.amount_cents) < 0
            ORDER BY a.date""", (seq[0], seq[-1])).fetchall()

    grouped: dict[str, list] = {}
    for r in rows:
        if r["merchant"]:
            grouped.setdefault(r["merchant"], []).append(r)

    # What you declared wins outright, and suppresses guessing inside the same
    # category — otherwise saying "Water is quarterly" leaves the app still
    # arguing about the water company.
    out: list[PeriodicBill] = _declared_bills(conn, today or date.today())
    declared_cats = {b.category_id for b in out}
    for merchant, charges in grouped.items():
        if charges[-1]["category_id"] in declared_cats:
            continue
        # One bill per day: a payment split into two lines is still one bill,
        # and counting it twice would halve the apparent interval.
        per_day: dict[date, int] = {}
        for c in charges:
            day = date.fromisoformat(c["date"])
            per_day[day] = per_day.get(day, 0) + -c["amount_cents"]
        dates = sorted(per_day)
        if len(dates) < 2:
            continue
        gaps = sorted((dates[i + 1] - dates[i]).days for i in range(len(dates) - 1))
        gap = gaps[len(gaps) // 2]
        band = next((b for b in CADENCE_BANDS if b[0] <= gap <= b[1]), None)
        if band is None:
            continue
        _, _, label, months_per = band

        amounts = list(per_day.values())
        typical = _median_int(amounts)
        if typical < MIN_BILL_CENTS or not _all_near(amounts, typical, AMOUNT_SPREAD):
            continue
        if len(dates) == 2:
            # One gap: no schedule to check, so the rest of the evidence has to
            # carry it. Never for the short cadences, where three sightings are
            # only a few months of statements away.
            if months_per < 6 or typical < LONE_GAP_MIN_CENTS or \
                    not _all_near(amounts, typical, LONE_GAP_SPREAD):
                continue
        elif not _all_near(gaps, gap, GAP_SPREAD):
            continue

        out.append(PeriodicBill(
            merchant=merchant, category=charges[-1]["category"],
            category_id=charges[-1]["category_id"], cadence=label,
            months_per=months_per, typical_cents=typical,
            last_date=dates[-1], next_due=dates[-1] + timedelta(days=gap),
            times_seen=len(dates)))
    out.sort(key=lambda b: b.next_due)
    return out


# --- is the budget right? ----------------------------------------------------

REVIEW_MONTHS = 6          # window the "what you usually spend" figure comes from
MATERIAL_CENTS = 2000      # below this, a gap is not worth telling you about
TOO_LOW = 1.15             # usual spend this far above budget means the budget is low
TOO_HIGH = 0.70            # ...and this far below means it is holding money idle
# A periodic bill stops a category being judged month by month, so it had
# better be most of what that category is. Pharmacy holding a quarterly
# prescription plus ordinary purchases is still a monthly category, and
# letting the bill speak for it silenced a budget check that was working.
BILL_DOMINATES = 0.60


@dataclass
class BudgetNote:
    """One thing worth knowing about a category's budget this month."""
    kind: str                  # over | unbudgeted | raise | lower | periodic
    category: str
    category_id: int
    budget: int = 0            # the monthly budget in force, carry-forward included
    actual: int = 0            # spent this month
    typical: int = 0           # median month's spend over the window
    suggested: int = 0         # what the monthly budget would have to be
    bill: PeriodicBill | None = None

    @property
    def over_by(self) -> int:
        return max(0, self.actual - self.budget)

    @property
    def change(self) -> int:
        return self.suggested - self.budget


def _monthly_spend_by_category(conn, months: list[str]) -> dict[int, dict[str, int]]:
    rows = conn.execute(
        """SELECT a.category_id AS cid, substr(a.date,1,7) AS m,
                  SUM(-a.amount_cents) AS spent
           FROM txn_allocations a JOIN categories c ON c.id = a.category_id
           JOIN category_groups g ON g.id = c.group_id
           WHERE g.kind = 'expense' AND c.excluded = 0 AND a.is_transfer = 0
             AND substr(a.date,1,7) >= ? AND substr(a.date,1,7) <= ?
           GROUP BY a.category_id, m""", (months[0], months[-1])).fetchall()
    out: dict[int, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["cid"], {})[r["m"]] = max(0, r["spent"])
    return out


def budget_review(conn, month: str) -> list[BudgetNote]:
    """What needs your attention about this month's budget.

    Answers two different questions at once: what has gone over this month,
    and what the budget itself has wrong — a category you never budgeted but
    spend on every month, or one whose figure hasn't matched reality in
    months. Both are "adjust your budget", and neither is visible from a bar
    that only compares one month to one number.

    Bills that don't arrive monthly are handled separately rather than judged
    on a month they were never going to fit: a quarterly bill is 200% over in
    the month it lands and 100% under in the two either side, and reporting
    that four times a year trains you to ignore the page.
    """
    from .budgets import effective_budgets     # circular at module scope

    window = months_back(month, REVIEW_MONTHS)
    budgets_now, _ = effective_budgets(conn, month)
    history = _monthly_spend_by_category(conn, window)
    bills = {b.category_id: b for b in periodic_bills(conn, month)
             if b.category_id is not None}

    names = {r["id"]: r["name"] for r in conn.execute(
        "SELECT c.id, c.name FROM categories c "
        "JOIN category_groups g ON g.id = c.group_id "
        "WHERE g.kind = 'expense' AND c.excluded = 0 AND c.archived = 0")}

    notes: list[BudgetNote] = []
    for cat_id, name in names.items():
        months_seen = history.get(cat_id, {})
        actual = months_seen.get(month, 0)
        budget = budgets_now.get(cat_id, 0)
        if not actual and not budget and not months_seen:
            continue
        # The month in progress is not evidence of a typical month yet.
        past = [months_seen.get(m, 0) for m in window if m != month]
        typical = _median_int([v for v in past if v > 0]) if any(past) else 0

        # Only a bill that is most of the category speaks for it — unless you
        # said so yourself, in which case it is not the app's call to overrule.
        bill = bills.get(cat_id)
        if bill is not None and not bill.declared:
            per_month = sum(months_seen.get(m, 0) for m in window) / len(window)
            if per_month and bill.monthly_cents < per_month * BILL_DOMINATES:
                bill = None
        if bill is not None:
            notes.append(BudgetNote(
                kind="periodic", category=name, category_id=cat_id, budget=budget,
                actual=actual, typical=typical, suggested=bill.monthly_cents,
                bill=bill))
        elif budget and actual > budget and actual - budget >= MATERIAL_CENTS:
            notes.append(BudgetNote(
                kind="over", category=name, category_id=cat_id, budget=budget,
                actual=actual, typical=typical, suggested=max(typical, actual)))
        elif not budget and typical >= MATERIAL_CENTS and len(
                [v for v in past if v > 0]) >= 3:
            notes.append(BudgetNote(
                kind="unbudgeted", category=name, category_id=cat_id, actual=actual,
                typical=typical, suggested=typical))
        elif budget and typical >= budget * TOO_LOW and typical - budget >= MATERIAL_CENTS:
            notes.append(BudgetNote(
                kind="raise", category=name, category_id=cat_id, budget=budget,
                actual=actual, typical=typical, suggested=typical))
        elif budget and typical and typical <= budget * TOO_HIGH and \
                budget - typical >= MATERIAL_CENTS:
            notes.append(BudgetNote(
                kind="lower", category=name, category_id=cat_id, budget=budget,
                actual=actual, typical=typical, suggested=typical))

    order = {"over": 0, "unbudgeted": 1, "raise": 2, "periodic": 3, "lower": 4}
    notes.sort(key=lambda n: (order[n.kind],
                              -(n.over_by or n.typical or n.suggested)))
    return notes
