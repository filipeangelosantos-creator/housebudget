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


def set_budget_onward(conn, category_id: int, month: str, amount_cents: int) -> int:
    """Set this month's figure and let every later month follow it.

    Carry-forward already means a month with no figure of its own follows the
    last one set, so applying a change forward is a matter of clearing the
    later figures that would override it — not of writing the same number into
    twelve months and having to do it again next year. Returns how many months
    were holding their own figure and now inherit this one.
    """
    set_budget(conn, category_id, month, amount_cents)
    cur = conn.execute("DELETE FROM budgets WHERE category_id = ? AND month > ?",
                       (category_id, month))
    return cur.rowcount


def copy_budgets(conn, from_month: str, to_month: str) -> int:
    """Put one month's budget into another. Returns how many categories moved.

    Reads the effective budget, so copying from a month that inherits copies
    the figures you can see on it rather than the nothing its own row count
    would suggest.

    The target is cleared first. Merging the two would leave the destination
    holding amounts from a month you didn't copy, in categories the source
    never mentioned — a budget belonging to neither month.
    """
    if from_month == to_month:
        return 0
    src, _ = effective_budgets(conn, from_month)
    if not src:
        # Nothing to copy. Clearing the target anyway would make "copy from a
        # month I never budgeted" a way to delete the budget I was looking at.
        return 0
    conn.execute("DELETE FROM budgets WHERE month = ?", (to_month,))
    for cat_id, cents in src.items():
        set_budget(conn, cat_id, to_month, cents)
    conn.commit()
    return len(src)


def latest_budget_month_before(conn, month: str) -> str | None:
    row = conn.execute("SELECT MAX(month) AS m FROM budgets WHERE month < ?",
                       (month,)).fetchone()
    return row["m"] if row and row["m"] else None


def effective_budgets(conn, month: str) -> tuple[dict[int, int], str | None]:
    """This month's budget, falling back to the last month you set one.

    A budget is usually the same every month, so an unset month carries the
    previous one forward rather than reading as "no budget". Saving anything
    for the month makes it that month's own, and it stops inheriting.
    """
    explicit = get_budgets(conn, month)
    if explicit:
        return explicit, None
    source = latest_budget_month_before(conn, month)
    if source:
        return get_budgets(conn, source), source
    return {}, None


def expense_budget_total(conn, month: str) -> int:
    """Everything budgeted for spending this month, carry-forward included.

    Reading the budgets table directly here is a trap: a month that inherits
    its budget has no rows of its own, and the figure silently comes out zero.
    """
    amounts, _ = effective_budgets(conn, month)
    if not amounts:
        return 0
    counted = {r["id"] for r in conn.execute(
        "SELECT c.id FROM categories c JOIN category_groups g ON g.id = c.group_id "
        "WHERE g.kind = 'expense' AND c.excluded = 0")}
    return sum(cents for cat_id, cents in amounts.items() if cat_id in counted)


def actuals_by_category(conn, month: str) -> dict[int | None, int]:
    """Signed sums per category for the month (uncategorized under None).

    Reads txn_allocations so a split transaction contributes each part to its
    own category rather than the whole amount to one.
    """
    rows = conn.execute(
        "SELECT category_id, SUM(amount_cents) AS total FROM txn_allocations "
        "WHERE substr(date, 1, 7) = ? AND is_transfer = 0 GROUP BY category_id",
        (month,)).fetchall()
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
    budget_from: str | None = None      # set when this month inherits a budget

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
    budgets, budget_from = effective_budgets(conn, month)
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

    summary = MonthSummary(month=month, groups=groups, budget_from=budget_from)
    for block in groups:
        if block.kind == "income":
            summary.income_actual += block.actual
            summary.income_budget += block.budget
        else:
            summary.expense_actual += block.actual
            summary.expense_budget += block.budget

    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(t.amount_cents), 0) AS total "
        "FROM transactions t WHERE t.category_id IS NULL AND substr(t.date, 1, 7) = ? "
        "  AND NOT EXISTS (SELECT 1 FROM transfer_links l "
        "                  WHERE l.out_txn_id = t.id OR l.in_txn_id = t.id)",
        (month,)).fetchone()
    summary.uncategorized_count = row["n"]
    summary.uncategorized_amount = row["total"]
    return summary
