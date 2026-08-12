"""Budget storage and budget-vs-actual math."""
from dataclasses import dataclass, field
from datetime import date


def current_month() -> str:
    return date.today().strftime("%Y-%m")


def shift_month(month: str, delta: int) -> str:
    y, m = int(month[:4]), int(month[5:7])
    total = y * 12 + (m - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def month_label(month: str) -> str:
    names = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]
    return f"{names[int(month[5:7]) - 1]} {month[:4]}"


def get_budgets(conn, month: str) -> dict[int, int]:
    rows = conn.execute("SELECT category_id, amount_cents FROM budgets WHERE month = ?",
                        (month,)).fetchall()
    return {r["category_id"]: r["amount_cents"] for r in rows}


def set_budget(conn, category_id: int, month: str, amount_cents: int) -> None:
    if amount_cents == 0:
        conn.execute("DELETE FROM budgets WHERE category_id = ? AND month = ?",
                     (category_id, month))
    else:
        conn.execute(
            "INSERT INTO budgets (category_id, month, amount_cents) VALUES (?, ?, ?) "
            "ON CONFLICT(category_id, month) DO UPDATE SET amount_cents = excluded.amount_cents",
            (category_id, month, amount_cents))


def copy_budgets(conn, from_month: str, to_month: str) -> int:
    src = get_budgets(conn, from_month)
    for cat_id, cents in src.items():
        set_budget(conn, cat_id, to_month, cents)
    conn.commit()
    return len(src)


def latest_budget_month_before(conn, month: str) -> str | None:
    row = conn.execute("SELECT MAX(month) AS m FROM budgets WHERE month < ?",
                       (month,)).fetchone()
    return row["m"] if row and row["m"] else None


def actuals_by_category(conn, month: str) -> dict[int | None, int]:
    """Signed sums per category for the month (uncategorized under None)."""
    rows = conn.execute(
        "SELECT category_id, SUM(amount_cents) AS total FROM transactions "
        "WHERE substr(date, 1, 7) = ? GROUP BY category_id", (month,)).fetchall()
    return {r["category_id"]: r["total"] or 0 for r in rows}


@dataclass
class CategoryLine:
    id: int
    name: str
    budget: int      # cents, positive
    actual: int      # cents, positive magnitude in the group's direction
    excluded: bool = False

    @property
    def remaining(self) -> int:
        return self.budget - self.actual

    @property
    def pct(self) -> int:
        if self.budget <= 0:
            return 100 if self.actual > 0 else 0
        return round(self.actual * 100 / self.budget)


@dataclass
class GroupBlock:
    id: int
    name: str
    kind: str
    lines: list[CategoryLine] = field(default_factory=list)

    @property
    def budget(self) -> int:
        return sum(l.budget for l in self.lines if not l.excluded)

    @property
    def actual(self) -> int:
        return sum(l.actual for l in self.lines if not l.excluded)

    @property
    def pct(self) -> int:
        if self.budget <= 0:
            return 100 if self.actual > 0 else 0
        return round(self.actual * 100 / self.budget)


@dataclass
class MonthSummary:
    month: str
    groups: list[GroupBlock]
    income_actual: int = 0
    income_budget: int = 0
    expense_actual: int = 0
    expense_budget: int = 0
    uncategorized_amount: int = 0
    uncategorized_count: int = 0

    @property
    def net(self) -> int:
        return self.income_actual - self.expense_actual

    @property
    def savings_rate(self) -> int | None:
        if self.income_actual <= 0:
            return None
        return round(100 * (self.income_actual - self.expense_actual) / self.income_actual)


def month_summary(conn, month: str, include_empty: bool = False) -> MonthSummary:
    """Budget vs actual for one month, grouped. Expense actuals are shown as
    positive 'spent' magnitudes; refunds within a category net out."""
    budgets = get_budgets(conn, month)
    actuals = actuals_by_category(conn, month)

    groups: list[GroupBlock] = []
    for g in conn.execute(
            "SELECT id, name, kind FROM category_groups ORDER BY sort_order, id").fetchall():
        block = GroupBlock(id=g["id"], name=g["name"], kind=g["kind"])
        for c in conn.execute(
                "SELECT id, name, excluded, archived FROM categories "
                "WHERE group_id = ? ORDER BY sort_order, id", (g["id"],)).fetchall():
            raw = actuals.get(c["id"], 0)
            actual = raw if g["kind"] == "income" else -raw
            budget = budgets.get(c["id"], 0)
            if c["archived"] and budget == 0 and raw == 0:
                continue
            if not include_empty and budget == 0 and raw == 0:
                continue
            block.lines.append(CategoryLine(
                id=c["id"], name=c["name"], budget=budget, actual=actual,
                excluded=bool(c["excluded"])))
        if block.lines or include_empty:
            groups.append(block)

    summary = MonthSummary(month=month, groups=groups)
    for block in groups:
        if block.kind == "income":
            summary.income_actual += block.actual
            summary.income_budget += block.budget
        else:
            summary.expense_actual += block.actual
            summary.expense_budget += block.budget

    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS total "
        "FROM transactions WHERE category_id IS NULL AND substr(date, 1, 7) = ?",
        (month,)).fetchone()
    summary.uncategorized_count = row["n"]
    summary.uncategorized_amount = row["total"]
    return summary
