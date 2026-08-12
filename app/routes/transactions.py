"""Transaction list/filter, quick categorization, edit page with rule
learning, review queue for uncategorized, and manual entry."""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..db import utcnow
from ..deps import current_user, get_conn, parse_money_input, render, verify_csrf
from ..services import classify
from .dashboard import clean_month

router = APIRouter()

PAGE_SIZE = 100


def _categories_grouped(conn):
    return conn.execute(
        "SELECT c.id, c.name, g.name AS group_name, g.kind "
        "FROM categories c JOIN category_groups g ON g.id = c.group_id "
        "WHERE c.archived = 0 ORDER BY g.sort_order, g.id, c.sort_order, c.id"
    ).fetchall()


def _txn_or_404(conn, txn_id: int):
    row = conn.execute(
        "SELECT t.*, a.name AS account_name FROM transactions t "
        "JOIN accounts a ON a.id = t.account_id WHERE t.id = ?", (txn_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    return row


@router.get("/transactions")
def transactions_list(request: Request, conn=Depends(get_conn),
                      user=Depends(current_user), month: str | None = None,
                      account: int | None = None, category: str | None = None,
                      q: str = "", page: int = 1):
    where, params = [], []
    # No param -> current month; explicit "all" or a cleared month input -> all months
    show_all_months = month is not None and month.strip() in ("all", "")
    m = None if show_all_months else clean_month(month)
    if m:
        where.append("substr(t.date,1,7) = ?")
        params.append(m)
    if account:
        where.append("t.account_id = ?")
        params.append(account)
    if category == "uncat":
        where.append("t.category_id IS NULL")
    elif category and category.isdigit():
        where.append("t.category_id = ?")
        params.append(int(category))
    if q.strip():
        where.append("(t.description LIKE ? OR t.normalized_desc LIKE ?)")
        like = f"%{q.strip()}%"
        params.extend([like, like.upper()])
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM transactions t {where_sql}", params).fetchone()[0]
    total_sum = conn.execute(
        f"SELECT COALESCE(SUM(t.amount_cents),0) FROM transactions t {where_sql}",
        params).fetchone()[0]
    page = max(1, page)
    rows = conn.execute(
        f"SELECT t.id, t.date, t.description, t.amount_cents, t.category_id, "
        f"a.name AS account_name, a.type AS account_type, c.name AS category_name "
        f"FROM transactions t JOIN accounts a ON a.id = t.account_id "
        f"LEFT JOIN categories c ON c.id = t.category_id "
        f"{where_sql} ORDER BY t.date DESC, t.id DESC LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()

    accounts = conn.execute(
        "SELECT id, name FROM accounts WHERE archived = 0 ORDER BY name").fetchall()
    from ..services.budgets import current_month, month_label, shift_month
    m_for_nav = m or current_month()
    return render(request, conn, "transactions.html",
                  rows=rows, total=total, total_sum=total_sum, page=page,
                  pages=max(1, -(-total // PAGE_SIZE)),
                  month=month or m_for_nav, show_all_months=show_all_months,
                  month_label="All months" if show_all_months else month_label(m_for_nav),
                  prev_month=shift_month(m_for_nav, -1),
                  next_month=shift_month(m_for_nav, 1),
                  account=account, category=category or "", q=q,
                  accounts=accounts, categories=_categories_grouped(conn))


@router.get("/transactions/new")
def txn_new_page(request: Request, conn=Depends(get_conn), user=Depends(current_user)):
    accounts = conn.execute(
        "SELECT id, name FROM accounts WHERE archived = 0 ORDER BY name").fetchall()
    return render(request, conn, "txn_new.html", accounts=accounts,
                  categories=_categories_grouped(conn), today=date.today().isoformat())


@router.post("/transactions/new", dependencies=[Depends(verify_csrf)])
def txn_new_submit(request: Request, conn=Depends(get_conn),
                   user=Depends(current_user), account_id: int = Form(...),
                   txn_date: str = Form(...), amount: str = Form(...),
                   direction: str = Form("expense"), description: str = Form(...),
                   category_id: str = Form(""), notes: str = Form("")):
    cents = abs(parse_money_input(amount))
    if cents == 0:
        raise HTTPException(status_code=400, detail="Amount is required")
    if direction == "expense":
        cents = -cents
    desc = description.strip() or "(manual entry)"
    norm = classify.normalize_desc(desc)
    conn.execute(
        "INSERT INTO transactions (account_id, date, amount_cents, description, "
        "normalized_desc, merchant_key, category_id, dedupe_hash, notes, manual, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (account_id, txn_date[:10], cents, desc, norm, classify.merchant_key(desc),
         int(category_id) if category_id.isdigit() else None,
         f"manual|{uuid.uuid4().hex}", notes.strip(), utcnow()))
    conn.commit()
    return RedirectResponse("/transactions", status_code=303)


@router.get("/transactions/{txn_id}")
def txn_edit_page(txn_id: int, request: Request, conn=Depends(get_conn),
                  user=Depends(current_user), back: str = "/transactions"):
    txn = _txn_or_404(conn, txn_id)
    similar_uncat = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL AND "
        "merchant_key = ? AND id != ?", (txn["merchant_key"], txn_id)).fetchone()[0]
    return render(request, conn, "txn_edit.html", txn=txn,
                  categories=_categories_grouped(conn),
                  suggested_pattern=classify.suggest_pattern(txn["description"]),
                  similar_uncat=similar_uncat,
                  back=back if back.startswith("/") else "/transactions")


@router.post("/transactions/{txn_id}", dependencies=[Depends(verify_csrf)])
def txn_edit_submit(txn_id: int, request: Request, conn=Depends(get_conn),
                    user=Depends(current_user), category_id: str = Form(""),
                    notes: str = Form(""), remember: str = Form(""),
                    pattern: str = Form(""), delete: str = Form(""),
                    back: str = Form("/transactions")):
    txn = _txn_or_404(conn, txn_id)
    if not back.startswith("/") or back.startswith("//"):
        back = "/transactions"
    if delete:
        conn.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
        conn.commit()
        return RedirectResponse(back, status_code=303)

    cat = int(category_id) if category_id.isdigit() else None
    conn.execute("UPDATE transactions SET category_id = ?, notes = ? WHERE id = ?",
                 (cat, notes.strip(), txn_id))
    conn.commit()
    if remember and cat and pattern.strip():
        rule_id = classify.create_rule(conn, pattern.strip(), cat)
        conn.commit()
        classify.apply_rules_to_uncategorized(conn, only_rule_id=rule_id)
    return RedirectResponse(back, status_code=303)


@router.get("/review")
def review_page(request: Request, conn=Depends(get_conn), user=Depends(current_user)):
    rows = conn.execute(
        "SELECT t.id, t.date, t.description, t.amount_cents, t.merchant_key, "
        "a.name AS account_name FROM transactions t "
        "JOIN accounts a ON a.id = t.account_id "
        "WHERE t.category_id IS NULL ORDER BY t.date DESC, t.id DESC LIMIT 50"
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL").fetchone()[0]
    suggestions = {r["id"]: classify.suggest_pattern(r["description"]) for r in rows}
    return render(request, conn, "review.html", rows=rows, total=total,
                  suggestions=suggestions, categories=_categories_grouped(conn))


@router.post("/review/{txn_id}", dependencies=[Depends(verify_csrf)])
def review_submit(txn_id: int, request: Request, conn=Depends(get_conn),
                  user=Depends(current_user), category_id: str = Form(""),
                  remember: str = Form(""), pattern: str = Form("")):
    txn = _txn_or_404(conn, txn_id)
    if not category_id.isdigit():
        return RedirectResponse("/review", status_code=303)
    cat = int(category_id)
    conn.execute("UPDATE transactions SET category_id = ? WHERE id = ?", (cat, txn_id))
    conn.commit()
    if remember:
        p = pattern.strip() or classify.suggest_pattern(txn["description"])
        rule_id = classify.create_rule(conn, p, cat)
        conn.commit()
        classify.apply_rules_to_uncategorized(conn, only_rule_id=rule_id)
    return RedirectResponse("/review", status_code=303)
