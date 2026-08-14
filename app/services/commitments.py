"""Money that is already spoken for.

A savings balance climbing while a card has to be clear by a fixed date is not
spare money, and a page that shows the two side by side without saying so
invites spending the same money twice.

Two shapes, and they are subtracted differently — which is the whole care of
this module:

  A **payoff** is a debt that must reach zero by a date. The debt is already in
  the household total, because a card you owe on is a negative balance. Taking
  it off again would count it twice. What a payoff adds is the *deadline*: this
  much, over this many months, is what it costs to get there.

  A **goal** is money you hold for something not yet bought. Nothing has
  subtracted it yet, so it genuinely comes off what is free to spend.
"""
from dataclasses import dataclass
from datetime import date

from ..db import utcnow
from . import cashflow


def _months_between(start: date, end: date) -> int:
    """Whole months from start to end, never less than one.

    Zero would divide by nothing, and a deadline this month means the whole
    amount is due this month — which is exactly what one gives you.
    """
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day < start.day:
        months -= 1
    return max(1, months)


@dataclass
class Commitment:
    id: int
    name: str
    kind: str                    # 'payoff' | 'goal'
    account_id: int | None
    account_name: str
    due: date
    outstanding_cents: int       # what still has to be found
    months_left: int
    note: str = ""

    @property
    def per_month_cents(self) -> int:
        return round(self.outstanding_cents / self.months_left)

    @property
    def overdue(self) -> bool:
        return self.outstanding_cents > 0 and self.months_left <= 1

    @property
    def reduces_free_money(self) -> bool:
        """A goal is money you are holding; a payoff is a debt already netted
        off the total. Only the first comes off what is free to spend."""
        return self.kind == "goal"


def save(conn, name: str, kind: str, due_date: str, account_id: int | None = None,
         target_cents: int = 0, note: str = "",
         commitment_id: int | None = None) -> int:
    if kind not in ("payoff", "goal"):
        raise ValueError(f"unknown commitment kind: {kind}")
    values = (name.strip(), kind, account_id, abs(target_cents), due_date,
              note.strip())
    if commitment_id:
        conn.execute(
            "UPDATE commitments SET name = ?, kind = ?, account_id = ?, "
            "target_cents = ?, due_date = ?, note = ? WHERE id = ?",
            values + (commitment_id,))
        return commitment_id
    cur = conn.execute(
        "INSERT INTO commitments (name, kind, account_id, target_cents, "
        "due_date, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        values + (utcnow(),))
    return cur.lastrowid


def remove(conn, commitment_id: int) -> None:
    conn.execute("DELETE FROM commitments WHERE id = ?", (commitment_id,))


def all_commitments(conn, today: date | None = None) -> list[Commitment]:
    today = today or date.today()
    owed = {b.account_id: b.current_cents for b in cashflow.balances(conn, today)}
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM accounts")}

    out = []
    for r in conn.execute(
            "SELECT * FROM commitments ORDER BY due_date, id").fetchall():
        due = date.fromisoformat(r["due_date"])
        if r["kind"] == "payoff":
            # What is actually still owed, read off the account rather than
            # typed once: a card you are still spending on owes more each month,
            # and a payoff plan against a stale figure is fiction.
            balance = owed.get(r["account_id"])
            outstanding = -balance if balance is not None and balance < 0 else 0
        else:
            outstanding = r["target_cents"]
        out.append(Commitment(
            id=r["id"], name=r["name"], kind=r["kind"], account_id=r["account_id"],
            account_name=names.get(r["account_id"], ""), due=due,
            outstanding_cents=outstanding,
            months_left=_months_between(today, due), note=r["note"]))
    return out


@dataclass
class Position:
    on_hand_cents: int           # everything, debts already netted off
    committed_cents: int         # goals: held for something not yet bought
    free_cents: int              # what is genuinely spare
    per_month_cents: int         # what every commitment needs each month
    items: list[Commitment]
    surplus_cents: int = 0       # what the plan leaves over each month
    planned_income_cents: int = 0
    planned_outgoings_cents: int = 0
    has_plan: bool = False

    @property
    def has_debt_deadline(self) -> bool:
        return any(c.kind == "payoff" and c.outstanding_cents for c in self.items)

    @property
    def shortfall_cents(self) -> int:
        """How far the month falls short of what the deadlines need.

        Knowing a payoff costs 617 a month is only half an answer; the half
        that matters is whether the month has 617 spare. Zero when it does.
        """
        return max(0, self.per_month_cents - self.surplus_cents)

    @property
    def on_track(self) -> bool:
        return self.has_plan and self.per_month_cents > 0 and not self.shortfall_cents


def position(conn, today: date | None = None) -> Position:
    """What you have, what of it is spoken for, and whether the month covers it."""
    from . import schedules as sched

    today = today or date.today()
    items = all_commitments(conn, today)
    on_hand = cashflow.on_hand(conn, today)[0]
    committed = sum(c.outstanding_cents for c in items if c.reduces_free_money)

    # Only declared schedules, for the same reason the forecast uses only those:
    # a surplus computed from guesses would be a licence to spend.
    lines = [s for s in sched.all_schedules(conn) if s.amount_cents]
    income = sum(s.monthly_cents for s in lines if s.kind == "income")
    outgoings = sum(s.monthly_cents for s in lines if s.kind != "income")
    return Position(on_hand_cents=on_hand, committed_cents=committed,
                    free_cents=on_hand - committed,
                    per_month_cents=sum(c.per_month_cents for c in items),
                    items=items, surplus_cents=income - outgoings,
                    planned_income_cents=income,
                    planned_outgoings_cents=outgoings, has_plan=bool(lines))
