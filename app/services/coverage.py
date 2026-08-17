"""Whether a month's statements have actually arrived yet.

Statements lag. A card closes on the 11th and lands a week after that, so for
half of any month the totals are a partial picture — and a partial month reads
exactly like an underspend. Worse, the budget check then offers to lower a
budget that was never underspent at all: the rest of the month simply hasn't
been imported.

This works out, per account, how much of a month is really in. The dates can
only ever suggest it, because nothing in a statement says "this is the last one
for August" — so you can overrule the guess either way, and a month you have
marked closed stays closed however the dates read.
"""
import calendar
from dataclasses import dataclass, field
from datetime import date

from ..db import utcnow

# What we can say about one account in one month.
#   closed   you said the statement is in, whatever the dates show
#   in       there are transactions dated past the end of the month, so
#            whatever covers it has been imported
#   partial  the month has transactions but they stop short of its end
#   waiting  nothing for this month at all, or you said one is still to come
#   dormant  archived, or added after the month ended — not expected, not counted
SETTLED = ("closed", "in")
PENDING = ("partial", "waiting")


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = int(month[:4]), int(month[5:7])
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def _as_date(value) -> date | None:
    """A stored date, or None if it isn't one.

    Nothing here is worth a 500: `created_at` has held whatever the release
    that wrote it used, and a row the app can't date simply doesn't get an
    opinion about which months predate it.
    """
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


@dataclass
class Coverage:
    account_id: int
    name: str
    kind: str
    state: str
    last_date: date | None = None     # newest transaction on this account, any month
    stops_at: date | None = None      # newest transaction inside this month
    count: int = 0                    # transactions inside this month
    note: str = ""
    marked: bool = False              # you said so, rather than the app guessing
    archived: bool = False            # the account itself is closed

    @property
    def settled(self) -> bool:
        return self.state in SETTLED

    @property
    def pending(self) -> bool:
        return self.state in PENDING

    @property
    def label(self) -> str:
        return {"closed": "statement in", "in": "looks complete",
                "partial": "partly in", "waiting": "not in yet",
                "dormant": "not expected"}[self.state]


@dataclass
class MonthCoverage:
    month: str
    rows: list[Coverage] = field(default_factory=list)
    in_progress: bool = False         # the month hasn't finished yet

    @property
    def expected(self) -> list[Coverage]:
        return [r for r in self.rows if r.state != "dormant"]

    @property
    def settled(self) -> list[Coverage]:
        return [r for r in self.rows if r.settled]

    @property
    def pending(self) -> list[Coverage]:
        return [r for r in self.rows if r.pending]

    @property
    def complete(self) -> bool:
        """Every account that was expected to report has reported."""
        return bool(self.expected) and not self.pending

    @property
    def pending_names(self) -> str:
        names = [r.name for r in self.pending]
        if len(names) <= 1:
            return "".join(names)
        return ", ".join(names[:-1]) + " and " + names[-1]


def for_month(conn, month: str, today: date | None = None) -> MonthCoverage:
    """One row per account, saying how much of `month` it has reported.

    `today` is injectable so the answer doesn't depend on the wall clock: a
    month that hasn't finished yet is described differently from one that has.
    """
    today = today or date.today()
    first, last = month_bounds(month)
    marks = {r["account_id"]: r for r in conn.execute(
        "SELECT account_id, state, note FROM statement_months WHERE month = ?",
        (month,)).fetchall()}

    rows: list[Coverage] = []
    for a in conn.execute(
            "SELECT a.id, a.name, a.type, a.archived, a.created_at, "
            "(SELECT MAX(t.date) FROM transactions t "
            "   WHERE t.account_id = a.id) AS last_any, "
            "(SELECT MAX(t.date) FROM transactions t WHERE t.account_id = a.id "
            "   AND substr(t.date, 1, 7) = ?) AS last_in, "
            "(SELECT COUNT(*) FROM transactions t WHERE t.account_id = a.id "
            "   AND substr(t.date, 1, 7) = ?) AS n "
            "FROM accounts a ORDER BY a.archived, a.name",
            (month, month)).fetchall():
        last_any, stops_at = _as_date(a["last_any"]), _as_date(a["last_in"])
        mark = marks.get(a["id"])
        row = Coverage(account_id=a["id"], name=a["name"], kind=a["type"],
                       state="waiting", last_date=last_any, stops_at=stops_at,
                       count=a["n"], note=mark["note"] if mark else "",
                       archived=bool(a["archived"]))
        added = _as_date(a["created_at"])
        if mark:
            # What you said beats what the dates suggest, in both directions.
            row.state, row.marked = mark["state"], True
        elif a["archived"]:
            row.state = "dormant"
        elif last_any is not None and last_any > last:
            # Something on this account is dated after the month ended, so
            # whatever covers the month has been through the importer.
            row.state = "in"
        elif stops_at is not None:
            row.state = "partial"
        elif added is not None and added > last:
            # Added after the month ended, and no history was backfilled:
            # asking for a statement that predates the account is just noise.
            row.state = "dormant"
        rows.append(row)

    return MonthCoverage(month=month, rows=rows,
                         in_progress=month >= today.strftime("%Y-%m"))


def mark(conn, account_id: int, month: str, state: str, note: str = "") -> None:
    """Record that a month is closed, or still waiting, for one account.

    Anything other than the two states clears the mark and hands the month back
    to the dates, so changing your mind costs one click rather than leaving a
    stale answer behind forever.
    """
    if state not in ("closed", "waiting"):
        conn.execute("DELETE FROM statement_months WHERE account_id = ? AND month = ?",
                     (account_id, month))
        return
    conn.execute(
        "INSERT INTO statement_months (account_id, month, state, note, marked_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(account_id, month) DO UPDATE SET "
        "state = excluded.state, note = excluded.note, marked_at = excluded.marked_at",
        (account_id, month, state, note.strip()[:120], utcnow()))


def close_all(conn, month: str, today: date | None = None) -> int:
    """Mark every account that hasn't reported as closed for the month.

    For the common case: the month is done, you have imported everything you
    are going to, and saying so account by account is a chore.
    """
    n = 0
    for row in for_month(conn, month, today).rows:
        # Only what is actually outstanding: an account the dates already
        # vouch for needs no mark, and writing one would just be a row to
        # maintain saying what the transactions say anyway.
        if not row.pending:
            continue
        mark(conn, row.account_id, month, "closed")
        n += 1
    return n


def months_with_gaps(conn, upto: str, back: int = 6,
                     today: date | None = None) -> list[MonthCoverage]:
    """The recent months that are still waiting on something, newest first.

    A month you have finished with drops off this list, which is the point: it
    should shrink as you work through it, not stay a permanent wall of amber.
    It stops at the first month you have any statement for, too — every month
    before you started using the app is missing every statement, and saying so
    is true and useless.
    """
    earliest = conn.execute(
        "SELECT MIN(substr(date, 1, 7)) FROM transactions").fetchone()[0]
    out = []
    year, mon = int(upto[:4]), int(upto[5:7])
    for _ in range(max(1, back)):
        m = f"{year:04d}-{mon:02d}"
        if earliest and m < earliest:
            break
        cov = for_month(conn, m, today)
        if cov.pending:
            out.append(cov)
        mon -= 1
        if mon == 0:
            year, mon = year - 1, 12
    return out
