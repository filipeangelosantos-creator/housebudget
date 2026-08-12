"""Bank/credit-card account management."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ..db import utcnow
from ..deps import current_user, get_conn, render, verify_csrf

router = APIRouter()

ACCOUNT_TYPES = ("checking", "savings", "credit", "cash", "other")


@router.get("/accounts")
def accounts_page(request: Request, conn=Depends(get_conn),
                  user=Depends(current_user)):
    rows = conn.execute(
        "SELECT a.*, COUNT(t.id) AS txn_count, MAX(t.date) AS last_txn "
        "FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id "
        "GROUP BY a.id ORDER BY a.archived, a.name").fetchall()
    return render(request, conn, "accounts.html", rows=rows, types=ACCOUNT_TYPES)


@router.post("/accounts/add", dependencies=[Depends(verify_csrf)])
def account_add(request: Request, conn=Depends(get_conn), user=Depends(current_user),
                name: str = Form(...), type: str = Form("checking")):
    if name.strip():
        conn.execute("INSERT INTO accounts (name, type, created_at) VALUES (?, ?, ?)",
                     (name.strip(), type if type in ACCOUNT_TYPES else "checking",
                      utcnow()))
        conn.commit()
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/{account_id}/update", dependencies=[Depends(verify_csrf)])
def account_update(account_id: int, request: Request, conn=Depends(get_conn),
                   user=Depends(current_user), name: str = Form(""),
                   action: str = Form("rename")):
    if action == "rename" and name.strip():
        conn.execute("UPDATE accounts SET name = ? WHERE id = ?",
                     (name.strip(), account_id))
    elif action == "toggle_archived":
        conn.execute("UPDATE accounts SET archived = 1 - archived WHERE id = ?",
                     (account_id,))
    conn.commit()
    return RedirectResponse("/accounts", status_code=303)
