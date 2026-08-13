"""What you have told the app about when money moves, rather than what it guessed.

Detection from statements is good enough to suggest things and never good
enough to be certain: pay every other week reads as "sometimes three paydays",
a mortgage on the 1st that clears on the 3rd twice a year reads as drift, and a
quarterly bill needs three sightings before it is even visible. Every one of
those shows up as a category swinging for no reason.

A schedule is the answer to that: you say how often it happens and roughly how
much, and everything downstream stops guessing for that category.
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from ..db import utcnow

# months_per is 0 for the cadences that don't divide into months — those are
# counted by walking the calendar instead.
CADENCES: dict[str, tuple[str, int]] = {
    "weekly": ("every week", 0),
    "biweekly": ("every 2 weeks", 0),
    "semimonthly": ("twice a month", 0),
    "monthly": ("every month", 1),
    "every_2_months": ("every 2 months", 2),
    "quarterly": ("every 3 months", 3),
    "semiannual": ("every 6 months", 6),
    "yearly": ("once a year", 12),
}

# What you would sensibly put on an income category versus a spending one.
INCOME_CADENCES = ["weekly", "biweekly", "semimonthly", "monthly"]
EXPENSE_CADENCES = ["monthly", "every_2_months", "quarterly", "semiannual",
                    "yearly", "weekly", "biweekly", "semimonthly"]


def label(cadence: str) -> str:
    return CADENCES.get(cadence, (cadence, 0))[0]


def months_per(cadence: str) -> int:
    return CADENCES.get(cadence, ("", 0))[1]


def _clamp(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


@dataclass
class Schedule:
    id: int
    category_id: int
    category: str
    kind: str                 # 'income' or 'expense', from the category's group
    name: str                 # whose pay, or which of the category's bills
    cadence: str
    amount_cents: int         # per occurrence
    anchor: date
    note: str = ""

    @property
    def label(self) -> str:
        return label(self.cadence)

    @property
    def title(self) -> str:
        """What to call this line. Two salaries need telling apart; one doesn't."""
        return f"{self.category} — {self.name}" if self.name else self.category

    def dates_in(self, month: str) -> list[date]:
        """Every date this schedule lands on during `month`."""
        year, mon = int(month[:4]), int(month[5:7])
        last = calendar.monthrange(year, mon)[1]
        first_day, last_day = date(year, mon, 1), date(year, mon, last)

        if self.cadence in ("weekly", "biweekly"):
            step = timedelta(days=7 if self.cadence == "weekly" else 14)
            cursor = self.anchor
            # Walk clear of the month, then step back in — stopping as soon as
            # the cursor is inside would miss the earlier dates.
            while cursor >= first_day:
                cursor -= step
            while cursor < first_day:
                cursor += step
            out = []
            while cursor <= last_day:
                out.append(cursor)
                cursor += step
            return out

        if self.cadence == "semimonthly":
            second = self.anchor.day + 15
            days = sorted({self.anchor.day, second if second <= 31 else second - 31})
            return [_clamp(year, mon, d) for d in days]

        step = months_per(self.cadence)
        if step <= 0:
            return []
        # Count whole months from the anchor; it lands here only when the gap
        # divides evenly, which is what makes a quarterly bill skip two months.
        gap = (year - self.anchor.year) * 12 + (mon - self.anchor.month)
        if gap % step:
            return []
        return [_clamp(year, mon, self.anchor.day)]

    def next_after(self, day: date) -> date:
        """The first occurrence strictly after `day`."""
        month = f"{day.year:04d}-{day.month:02d}"
        for _ in range(14):                      # a yearly schedule needs 13
            for d in self.dates_in(month):
                if d > day:
                    return d
            year, mon = int(month[:4]), int(month[5:7])
            month = f"{year + mon // 12:04d}-{mon % 12 + 1:02d}"
        return day

    def expected_in(self, month: str) -> int:
        return self.amount_cents * len(self.dates_in(month))

    @property
    def monthly_cents(self) -> int:
        """What one month of this costs on average — the set-aside figure."""
        step = months_per(self.cadence)
        if step:
            return round(self.amount_cents / step)
        per_year = {"weekly": 52, "biweekly": 26, "semimonthly": 24}
        return round(self.amount_cents * per_year.get(self.cadence, 12) / 12)

    @property
    def is_periodic(self) -> bool:
        """Arrives less often than monthly, so a monthly figure misreads it."""
        return months_per(self.cadence) > 1


def _row_to_schedule(r) -> Schedule:
    return Schedule(id=r["id"], category_id=r["category_id"],
                    category=r["category_name"], kind=r["kind"], name=r["name"],
                    cadence=r["cadence"], amount_cents=r["amount_cents"],
                    anchor=date.fromisoformat(r["anchor_date"]), note=r["note"])


_SELECT = """SELECT s.*, c.name AS category_name, g.kind FROM schedules s
             JOIN categories c ON c.id = s.category_id
             JOIN category_groups g ON g.id = c.group_id"""


def all_schedules(conn) -> list[Schedule]:
    return [_row_to_schedule(r) for r in conn.execute(
        _SELECT + " ORDER BY g.kind DESC, c.sort_order, c.id, s.id").fetchall()]


def by_category(conn) -> dict[int, list[Schedule]]:
    """Many per category: two people paid out of one Salary line, or a category
    holding both a monthly bill and a yearly one."""
    out: dict[int, list[Schedule]] = {}
    for s in all_schedules(conn):
        out.setdefault(s.category_id, []).append(s)
    return out


def for_category(conn, category_id: int) -> list[Schedule]:
    return [_row_to_schedule(r) for r in conn.execute(
        _SELECT + " WHERE s.category_id = ? ORDER BY s.id",
        (category_id,)).fetchall()]


def get(conn, schedule_id: int) -> Schedule | None:
    row = conn.execute(_SELECT + " WHERE s.id = ?", (schedule_id,)).fetchone()
    return _row_to_schedule(row) if row else None


def save(conn, category_id: int, cadence: str, amount_cents: int, anchor: str,
         name: str = "", note: str = "", schedule_id: int | None = None) -> int:
    """Add a line, or change one. Returns its id."""
    if cadence not in CADENCES:
        raise ValueError(f"unknown cadence: {cadence}")
    values = (category_id, name.strip(), cadence, abs(amount_cents), anchor,
              note.strip())
    if schedule_id:
        conn.execute(
            "UPDATE schedules SET category_id = ?, name = ?, cadence = ?, "
            "amount_cents = ?, anchor_date = ?, note = ? WHERE id = ?",
            values + (schedule_id,))
        return schedule_id
    cur = conn.execute(
        "INSERT INTO schedules (category_id, name, cadence, amount_cents, "
        "anchor_date, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        values + (utcnow(),))
    return cur.lastrowid


def clear(conn, schedule_id: int) -> None:
    conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
