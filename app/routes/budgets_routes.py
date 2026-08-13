"""Budget editor per month + category management."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ..deps import current_user, get_conn, parse_money_input, render, verify_csrf
from ..services import budgets, insights
from .dashboard import clean_month

router = APIRouter()


@router.get("/budgets")
def budgets_page(request: Request, conn=Depends(get_conn),
                 user=Depends(current_user), month: str | None = None,
                 copied: int | None = None, copied_from: str | None = None):
    """`copied` / `copied_from` report the result of a copy that just ran, so
    landing on a month that didn't change says why instead of looking broken."""
    m = clean_month(month)
    summary = budgets.month_summary(conn, m, include_empty=True)
    has_budget = any(l.budget for g in summary.groups for l in g.lines)
    prior = budgets.latest_budget_month_before(conn, m)
    # Keyed by category so each input can say "this one is quarterly" where you
    # are typing the figure, rather than only on a page you might not open.
    bills = {b.category_id: b for b in insights.periodic_bills(conn, m)
             if b.category_id is not None}
    return render(request, conn, "budgets.html",
                  month=m, month_label=budgets.month_label(m),
                  prev_month=budgets.shift_month(m, -1),
                  next_month=budgets.shift_month(m, 1),
                  prior_label=budgets.month_label(summary.budget_from)
                  if summary.budget_from else "",
                  summary=summary, has_budget=has_budget, prior_month=prior,
                  bills=bills,
                  prior_month_label=budgets.month_label(prior) if prior else "",
                  copied=copied,
                  copied_from=clean_month(copied_from) if copied_from else "",
                  copied_from_label=budgets.month_label(clean_month(copied_from))
                  if copied_from else "",
                  income=insights.expected_income(conn, m))


@router.post("/budgets/save", dependencies=[Depends(verify_csrf)])
async def budgets_save(request: Request, conn=Depends(get_conn),
                       user=Depends(current_user)):
    form = await request.form()
    m = clean_month(str(form.get("month", "")))
    for key, value in form.items():
        if key.startswith("cat_"):
            cat_id = key[4:]
            if cat_id.isdigit():
                budgets.set_budget(conn, int(cat_id), m,
                                   abs(parse_money_input(str(value))))
    conn.commit()
    return RedirectResponse(f"/budgets?month={m}", status_code=303)


@router.post("/budgets/use-expected", dependencies=[Depends(verify_csrf)])
def budgets_use_expected(request: Request, conn=Depends(get_conn),
                         user=Depends(current_user), month: str = Form(...),
                         basis: str = Form("expected")):
    """Budget each income category for the paydays that actually land this
    month, rather than a flat figure that is wrong every other month.

    `basis` picks which end of the range: the usual estimate, or the low one
    for people who would rather not plan around money that might not arrive.
    """
    m = clean_month(month)
    field = "low" if basis == "low" else "expected"
    per_category: dict[str, int] = {}
    for entry in insights.expected_income(conn, m)["detail"]:
        per_category[entry["stream"].category] = (
            per_category.get(entry["stream"].category, 0) + entry[field])
    # Anything inherited from a previous month must become explicit now, or
    # saving one category would drop the rest.
    existing, inherited_from = budgets.effective_budgets(conn, m)
    if inherited_from:
        for cat_id, cents in existing.items():
            budgets.set_budget(conn, cat_id, m, cents)
    for name, cents in per_category.items():
        row = conn.execute("SELECT id FROM categories WHERE name = ?",
                           (name,)).fetchone()
        if row:
            budgets.set_budget(conn, row["id"], m, cents)
    conn.commit()
    return RedirectResponse(f"/budgets?month={m}", status_code=303)


@router.post("/budgets/copy", dependencies=[Depends(verify_csrf)])
def budgets_copy(request: Request, conn=Depends(get_conn),
                 user=Depends(current_user), month: str = Form(...),
                 from_month: str = Form(...)):
    """Copy a budget into `month` from `from_month`, in either direction.

    The same endpoint serves both: pulling an older month in is a copy whose
    target is this month, pushing this month forward is one whose source is.
    Either way it lands on the month that changed, so the result is in front
    of you rather than somewhere you have to go and check.
    """
    src, dest = clean_month(from_month), clean_month(month)
    n = budgets.copy_budgets(conn, src, dest)
    return RedirectResponse(f"/budgets?month={dest}&copied={n}&copied_from={src}",
                            status_code=303)


# --- categories --------------------------------------------------------------

@router.get("/categories")
def categories_page(request: Request, conn=Depends(get_conn),
                    user=Depends(current_user)):
    groups = conn.execute(
        "SELECT * FROM category_groups ORDER BY sort_order, id").fetchall()
    cats = conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM transactions t WHERE t.category_id = c.id) "
        "AS txn_count FROM categories c ORDER BY c.sort_order, c.id").fetchall()
    by_group: dict[int, list] = {}
    for c in cats:
        by_group.setdefault(c["group_id"], []).append(c)
    return render(request, conn, "categories.html", groups=groups, by_group=by_group)


@router.post("/categories/add", dependencies=[Depends(verify_csrf)])
def category_add(request: Request, conn=Depends(get_conn),
                 user=Depends(current_user), group_id: int = Form(...),
                 name: str = Form(...)):
    if name.strip():
        order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM categories WHERE group_id = ?",
            (group_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO categories (group_id, name, sort_order) VALUES (?, ?, ?)",
            (group_id, name.strip(), order))
        conn.commit()
    return RedirectResponse("/categories", status_code=303)


@router.post("/categories/{cat_id}/update", dependencies=[Depends(verify_csrf)])
def category_update(cat_id: int, request: Request, conn=Depends(get_conn),
                    user=Depends(current_user), name: str = Form(""),
                    action: str = Form("rename")):
    if action == "rename" and name.strip():
        conn.execute("UPDATE categories SET name = ? WHERE id = ?",
                     (name.strip(), cat_id))
    elif action == "toggle_excluded":
        conn.execute("UPDATE categories SET excluded = 1 - excluded WHERE id = ?",
                     (cat_id,))
    elif action == "toggle_archived":
        conn.execute("UPDATE categories SET archived = 1 - archived WHERE id = ?",
                     (cat_id,))
    elif action == "delete":
        used = conn.execute("SELECT COUNT(*) FROM transactions WHERE category_id = ?",
                            (cat_id,)).fetchone()[0]
        if used == 0:
            conn.execute("DELETE FROM rules WHERE category_id = ?", (cat_id,))
            conn.execute("DELETE FROM budgets WHERE category_id = ?", (cat_id,))
            conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    conn.commit()
    return RedirectResponse("/categories", status_code=303)


@router.post("/groups/add", dependencies=[Depends(verify_csrf)])
def group_add(request: Request, conn=Depends(get_conn), user=Depends(current_user),
              name: str = Form(...), kind: str = Form("expense")):
    if name.strip():
        order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 FROM category_groups").fetchone()[0]
        conn.execute(
            "INSERT INTO category_groups (name, kind, sort_order) VALUES (?, ?, ?)",
            (name.strip(), kind if kind in ("income", "expense") else "expense", order))
        conn.commit()
    return RedirectResponse("/categories", status_code=303)
