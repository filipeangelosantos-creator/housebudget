"""Transaction list/filter, edit page with rule learning, the review queue
(uncategorized plus mixed-basket confirmations), splitting one transaction
across categories, and manual entry."""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..db import utcnow
from ..deps import current_user, get_conn, parse_money_input, render, verify_csrf
from ..services import classify, review, splits as splits_svc
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
                      account: str = "", category: str | None = None,
                      q: str = "", page: str = "1"):
    # The filter form submits account= and page= as empty strings for "all" /
    # unset; typing these as int made FastAPI reject the request outright.
    account = int(account) if account.isdigit() else None
    page = max(1, int(page)) if page.isdigit() else 1
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
    rows = conn.execute(
        f"SELECT t.id, t.date, t.description, t.amount_cents, t.category_id, "
        f"t.needs_review, a.name AS account_name, a.type AS account_type, "
        f"c.name AS category_name, "
        f"(SELECT COUNT(*) FROM transaction_splits s WHERE s.transaction_id = t.id) "
        f"  AS n_splits "
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


def _safe_back(back: str) -> str:
    return back if back.startswith("/") and not back.startswith("//") else "/transactions"


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
                  splits=splits_svc.get_splits(conn, txn_id),
                  history=splits_svc.merchant_history(conn, txn["merchant_key"], txn_id),
                  mixed_basket=splits_svc.is_mixed_basket_merchant(
                      txn["normalized_desc"], txn["merchant_key"]),
                  back=_safe_back(back))


@router.post("/transactions/{txn_id}", dependencies=[Depends(verify_csrf)])
def txn_edit_submit(txn_id: int, request: Request, conn=Depends(get_conn),
                    user=Depends(current_user), category_id: str = Form(""),
                    notes: str = Form(""), remember: str = Form(""),
                    pattern: str = Form(""), delete: str = Form(""),
                    back: str = Form("/transactions")):
    _txn_or_404(conn, txn_id)
    back = _safe_back(back)
    if delete:
        conn.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
        conn.commit()
        return RedirectResponse(back, status_code=303)

    cat = int(category_id) if category_id.isdigit() else None
    if cat is not None and splits_svc.has_splits(conn, txn_id):
        # Choosing a single category replaces the split allocation.
        splits_svc.clear_splits(conn, txn_id)
    conn.execute(
        "UPDATE transactions SET category_id = ?, notes = ?, needs_review = 0 "
        "WHERE id = ?", (cat, notes.strip(), txn_id))
    conn.commit()
    if remember and cat and pattern.strip():
        rule_id = classify.create_rule(conn, pattern.strip(), cat)
        conn.commit()
        classify.apply_rules_to_uncategorized(conn, only_rule_id=rule_id)
    return RedirectResponse(back, status_code=303)


# --- splitting one transaction across categories -----------------------------

@router.get("/transactions/{txn_id}/split")
def txn_split_page(txn_id: int, request: Request, conn=Depends(get_conn),
                   user=Depends(current_user), back: str = "/transactions",
                   error: str = ""):
    txn = _txn_or_404(conn, txn_id)
    existing = splits_svc.get_splits(conn, txn_id)
    history = splits_svc.merchant_history(conn, txn["merchant_key"], txn_id)
    if not existing:
        # Pre-fill the first row with the current/most-likely category and the
        # full amount, so you only type the part you're carving off.
        first_cat = txn["category_id"] or (history[0]["category_id"] if history else None)
        existing = [{"category_id": first_cat, "amount_cents": txn["amount_cents"],
                     "note": ""}]
    return render(request, conn, "txn_split.html", txn=txn, splits=existing,
                  categories=_categories_grouped(conn), history=history,
                  error=error, back=_safe_back(back),
                  rows_to_show=max(3, len(existing) + 1))


@router.post("/transactions/{txn_id}/split", dependencies=[Depends(verify_csrf)])
async def txn_split_submit(txn_id: int, request: Request, conn=Depends(get_conn),
                           user=Depends(current_user)):
    txn = _txn_or_404(conn, txn_id)
    form = await request.form()
    back = _safe_back(str(form.get("back", "/transactions")))

    if form.get("action") == "unsplit":
        splits_svc.clear_splits(conn, txn_id)
        return RedirectResponse(back, status_code=303)

    sign = -1 if txn["amount_cents"] < 0 else 1
    parts: list[splits_svc.Split] = []
    for key in sorted(k for k in form.keys() if k.startswith("cat_")):
        idx = key[4:]
        cat_raw = str(form.get(f"cat_{idx}", ""))
        amount_raw = str(form.get(f"amt_{idx}", ""))
        if not cat_raw.isdigit():
            continue
        cents = abs(parse_money_input(amount_raw))
        if cents == 0:
            continue
        parts.append(splits_svc.Split(
            category_id=int(cat_raw), amount_cents=sign * cents,
            note=str(form.get(f"note_{idx}", ""))))
    try:
        splits_svc.save_splits(conn, txn_id, parts)
    except splits_svc.SplitError as e:
        from urllib.parse import quote
        return RedirectResponse(
            f"/transactions/{txn_id}/split?back={quote(back, safe='/?=&')}"
            f"&error={quote(str(e))}", status_code=303)
    return RedirectResponse(back, status_code=303)


@router.post("/transactions/{txn_id}/confirm", dependencies=[Depends(verify_csrf)])
def txn_confirm(txn_id: int, request: Request, conn=Depends(get_conn),
                user=Depends(current_user), category_id: str = Form(""),
                remember: str = Form(""), pattern: str = Form(""),
                back: str = Form("/review")):
    txn = _txn_or_404(conn, txn_id)
    cat = int(category_id) if category_id.isdigit() else txn["category_id"]
    splits_svc.confirm_category(conn, txn_id, cat)
    if remember and cat and pattern.strip():
        rule_id = classify.create_rule(conn, pattern.strip(), cat)
        conn.commit()
        classify.apply_rules_to_uncategorized(conn, only_rule_id=rule_id)
    return RedirectResponse(_safe_back(back), status_code=303)


@router.get("/review")
def review_page(request: Request, conn=Depends(get_conn), user=Depends(current_user)):
    rows = review.needs_category_rows(conn)
    total = review.needs_category_count(conn)
    suggestions = {r["id"]: classify.suggest_pattern(r["description"]) for r in rows}
    # What the app guessed for each one, from how you've filed that merchant before
    guesses = {r["id"]: splits_svc.suggest_category(conn, r["merchant_key"], r["id"])
               for r in rows}
    confirms = splits_svc.pending_confirmations(conn)
    for c in confirms:
        c["history"] = splits_svc.merchant_history(conn, c["merchant_key"], c["id"])
        c["pattern"] = classify.suggest_pattern(c["description"])
    return render(request, conn, "review.html", rows=rows, total=total,
                  suggestions=suggestions, guesses=guesses, confirms=confirms,
                  confirm_total=splits_svc.pending_confirmation_count(conn),
                  categories=_categories_grouped(conn))


def _apply_review_choice(conn, txn_id: int, category_id: int,
                         remember: bool, pattern: str) -> None:
    txn = conn.execute("SELECT description FROM transactions WHERE id = ?",
                       (txn_id,)).fetchone()
    if txn is None:
        return
    conn.execute(
        "UPDATE transactions SET category_id = ?, needs_review = 0 WHERE id = ?",
        (category_id, txn_id))
    conn.commit()
    if remember:
        p = pattern.strip() or classify.suggest_pattern(txn["description"])
        rule_id = classify.create_rule(conn, p, category_id)
        conn.commit()
        classify.apply_rules_to_uncategorized(conn, only_rule_id=rule_id)


@router.post("/review/save", dependencies=[Depends(verify_csrf)])
async def review_save(request: Request, conn=Depends(get_conn),
                      user=Depends(current_user)):
    """One submit for the whole queue. `only=<id>` saves a single row (the
    per-row button); `only=all` saves every row that has a category chosen."""
    form = await request.form()
    only = str(form.get("only", "all"))
    for key in form.keys():
        if not key.startswith("cat_"):
            continue
        txn_id = key[4:]
        if not txn_id.isdigit() or (only != "all" and txn_id != only):
            continue
        value = str(form.get(key, ""))
        if not value.isdigit():
            continue
        _apply_review_choice(
            conn, int(txn_id), int(value),
            remember=bool(form.get(f"remember_{txn_id}")),
            pattern=str(form.get(f"pattern_{txn_id}", "")))
    return RedirectResponse("/review", status_code=303)


@router.post("/review/{txn_id}", dependencies=[Depends(verify_csrf)])
def review_submit(txn_id: int, request: Request, conn=Depends(get_conn),
                  user=Depends(current_user), category_id: str = Form(""),
                  remember: str = Form(""), pattern: str = Form("")):
    _txn_or_404(conn, txn_id)
    if category_id.isdigit():
        _apply_review_choice(conn, txn_id, int(category_id), bool(remember), pattern)
    return RedirectResponse("/review", status_code=303)
