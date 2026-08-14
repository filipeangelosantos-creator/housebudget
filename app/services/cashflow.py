"""Where the money actually is, and where it is going.

A statement says what moved, never what is there now. Everything else in this
app is built from movements, which makes it very good at "you spent 179 on
groceries" and incapable of "you have 412 until Friday". The balance is the one
fact an import cannot supply, so you supply it once and the transactions carry
it forward from there.

Forward of today it stops being arithmetic on the past and becomes the plan:
the schedules you declared say what lands and when, so the question the page
answers is the one worth asking — does the balance survive the month, and if
not, which day does it break on.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..db import utcnow
from . import schedules as sched


@dataclass
class AccountBalance:
    account_id: int
    name: str
    type: str
    as_of: date | None            # the day you told us about
    stated_cents: int             # what you said it was then
    since_cents: int = 0          # movements imported since that day
    known: bool = False

    @property
    def current_cents(self) -> int:
        """What it should be now: what you said, plus what has moved since."""
        return self.stated_cents + self.since_cents


def set_balance(conn, account_id: int, as_of: str, balance_cents: int) -> None:
    conn.execute(
        "INSERT INTO account_balances (account_id, as_of, balance_cents, created_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(account_id, as_of) "
        "DO UPDATE SET balance_cents = excluded.balance_cents",
        (account_id, as_of, balance_cents, utcnow()))


def clear_balance(conn, account_id: int) -> None:
    conn.execute("DELETE FROM account_balances WHERE account_id = ?", (account_id,))


def balances(conn, today: date | None = None) -> list[AccountBalance]:
    """Every live account, with what it is worth now where that is known.

    Movements are counted from the day *after* the stated one: a balance is
    what the bank showed at the end of that day, so the transactions on it are
    already in the figure you typed.
    """
    today = today or date.today()
    out = []
    for a in conn.execute(
            "SELECT id, name, type FROM accounts WHERE archived = 0 "
            "ORDER BY id").fetchall():
        row = conn.execute(
            "SELECT as_of, balance_cents FROM account_balances "
            "WHERE account_id = ? ORDER BY as_of DESC LIMIT 1", (a["id"],)).fetchone()
        entry = AccountBalance(account_id=a["id"], name=a["name"], type=a["type"],
                               as_of=None, stated_cents=0)
        if row:
            entry.known = True
            entry.as_of = date.fromisoformat(row["as_of"])
            entry.stated_cents = row["balance_cents"]
            # Everything counts here, transfers included: moving money to the
            # card really does leave the current account.
            entry.since_cents = conn.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions "
                "WHERE account_id = ? AND date > ? AND date <= ?",
                (a["id"], row["as_of"], today.isoformat())).fetchone()[0]
        out.append(entry)
    return out


def on_hand(conn, today: date | None = None) -> tuple[int, int, int]:
    """(total, accounts with a balance, accounts without one)."""
    known = [b for b in balances(conn, today) if b.known]
    total = sum(b.current_cents for b in known)
    missing = sum(1 for b in balances(conn, today) if not b.known)
    return total, len(known), missing


# How close an imported row has to be to a projected one to be the same bill.
# Direct debits wander by a couple of days around weekends.
SAME_BILL_DAYS = 4


@dataclass
class Event:
    when: date
    label: str
    amount_cents: int             # signed
    kind: str                     # 'scheduled' | 'imported'
    balance_after: int = 0


@dataclass
class Forecast:
    start_cents: int
    events: list[Event] = field(default_factory=list)
    end_cents: int = 0
    low_cents: int = 0
    low_on: date | None = None
    horizon_end: date | None = None
    accounts_known: int = 0
    accounts_missing: int = 0

    @property
    def goes_negative(self) -> bool:
        return self.low_cents < 0


def forecast(conn, days: int = 90, today: date | None = None) -> Forecast:
    """Balance day by day from here, using what you have declared.

    Only declared schedules are projected. Detection is fine for telling you
    what your spending looks like and much too loose to spend against — a
    forecast built on "we think this repeats" is a guess wearing a number's
    clothes.
    """
    today = today or date.today()
    horizon = today + timedelta(days=days)
    total, known, missing = on_hand(conn, today)

    # Anything already imported with a future date is real, not a projection —
    # a bill the bank has posted early still leaves the account.
    events: list[Event] = []
    already: dict[int, list[date]] = {}
    for r in conn.execute(
            "SELECT t.date, t.description, t.amount_cents, t.category_id "
            "FROM transactions t JOIN accounts a ON a.id = t.account_id "
            "WHERE a.archived = 0 AND t.date > ? AND t.date <= ? "
            "  AND t.account_id IN (SELECT account_id FROM account_balances) "
            "ORDER BY t.date", (today.isoformat(), horizon.isoformat())).fetchall():
        when = date.fromisoformat(r["date"])
        events.append(Event(when=when, label=r["description"],
                            amount_cents=r["amount_cents"], kind="imported"))
        if r["category_id"]:
            already.setdefault(r["category_id"], []).append(when)

    for s in sched.all_schedules(conn):
        if not s.amount_cents:
            continue
        sign = 1 if s.kind == "income" else -1
        cursor = s.next_after(today - timedelta(days=1))
        while cursor <= horizon:
            # A schedule says a bill is coming; an import says it has already
            # been taken. Counting both is the same money twice, and a forecast
            # that overstates the outgoings is one nobody trusts twice.
            landed = any(abs((d - cursor).days) <= SAME_BILL_DAYS
                         for d in already.get(s.category_id, []))
            if not landed:
                events.append(Event(when=cursor, label=s.title,
                                    amount_cents=sign * s.amount_cents,
                                    kind="scheduled"))
            cursor = s.next_after(cursor)

    events.sort(key=lambda e: (e.when, -e.amount_cents))
    running = total
    low, low_on = total, today
    for e in events:
        running += e.amount_cents
        e.balance_after = running
        if running < low:
            low, low_on = running, e.when
    return Forecast(start_cents=total, events=events, end_cents=running,
                    low_cents=low, low_on=low_on, horizon_end=horizon,
                    accounts_known=known, accounts_missing=missing)
