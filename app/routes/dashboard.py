"""Dashboard: month summary, budget vs actual bars, alerts, recent activity."""
import re

from fastapi import APIRouter, Depends, Request

from ..deps import current_user, get_conn, render
from ..services import budgets, insights, review, splits as splits_svc, transfers

router = APIRouter()

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def clean_month(month: str | None) -> str:
    if month and MONTH_RE.match(month):
        return month
    return budgets.current_month()


@router.get("/")
def dashboard(request: Request, conn=Depends(get_conn),
              user=Depends(current_user), month: str | None = None):
    m = clean_month(month)
    summary = budgets.month_summary(conn, m)
    alerts = insights.anomalies(conn, m)
    # Everything else on this page is about the chosen month, so this is too —
    # otherwise a stray future-dated row shows up under "latest".
    recent = conn.execute(
        "SELECT t.id, t.date, t.description, t.amount_cents, c.name AS category "
        "FROM transactions t LEFT JOIN categories c ON c.id = t.category_id "
        "WHERE substr(t.date, 1, 7) = ? "
        "ORDER BY t.date DESC, t.id DESC LIMIT 8", (m,)).fetchall()
    has_any_txn = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] > 0
    # From the summary, not the budgets table: a month that inherits its budget
    # has no rows of its own, and asking the table said "no budget set" while
    # the bars underneath were being measured against one.
    has_budget = bool(summary.income_budget or summary.expense_budget)
    total_uncat = review.needs_category_count(conn)
    return render(
        request, conn, "dashboard.html",
        month=m, prev_month=budgets.shift_month(m, -1),
        next_month=budgets.shift_month(m, 1),
        month_label=budgets.month_label(m), summary=summary, alerts=alerts,
        recent=recent, has_any_txn=has_any_txn, has_budget=has_budget,
        budget_from_label=budgets.month_label(summary.budget_from)
        if summary.budget_from else "",
        total_uncat=total_uncat,
        to_confirm=splits_svc.pending_confirmation_count(conn),
        transfer_suggestions=transfers.suggestion_count(conn),
        future_dated=review.future_dated(conn),
        income=insights.expected_income(conn, m))
