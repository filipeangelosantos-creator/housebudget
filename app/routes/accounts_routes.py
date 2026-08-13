"""Bank/credit-card account management."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ..db import utcnow
from ..deps import current_user, get_conn, render, verify_csrf
from ..services import accounts as accounts_svc

router = APIRouter()

ACCOUNT_TYPES = ("checking", "savings", "credit", "cash", "other")


@router.get("/accounts")
def accounts_page(request: Request, conn=Depends(get_conn),
                  user=Depends(current_user)):
    rows = conn.execute(
        "SELECT a.*, COUNT(t.id) AS txn_count, MAX(t.date) AS last_txn "
        "FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id "
        "GROUP BY a.id ORDER BY a.archived, a.name").fetchall()
    # Same-name accounts are almost always an accidental duplicate.
    name_counts = {}
    for r in rows:
        key = r["name"].strip().lower()
        name_counts[key] = name_counts.get(key, 0) + 1
    duplicates = {r["id"] for r in rows if name_counts[r["name"].strip().lower()] > 1}
    return render(request, conn, "accounts.html", rows=rows, types=ACCOUNT_TYPES,
                  duplicates=duplicates,
                  message=request.query_params.get("m", ""),
                  error=request.query_params.get("e", ""))


@router.post("/accounts/{account_id}/delete", dependencies=[Depends(verify_csrf)])
def account_delete(account_id: int, request: Request, conn=Depends(get_conn),
                   user=Depends(current_user)):
    from urllib.parse import quote
    try:
        accounts_svc.delete_account(conn, account_id)
    except accounts_svc.AccountError as e:
        return RedirectResponse(f"/accounts?e={quote(str(e))}", status_code=303)
    return RedirectResponse("/accounts?m=Account+deleted.", status_code=303)


@router.post("/accounts/{account_id}/merge", dependencies=[Depends(verify_csrf)])
def account_merge(account_id: int, request: Request, conn=Depends(get_conn),
                  user=Depends(current_user), target_id: str = Form("")):
    from urllib.parse import quote
    if not target_id.isdigit():
        return RedirectResponse("/accounts?e=Pick+an+account+to+merge+into.",
                                status_code=303)
    try:
        result = accounts_svc.merge_accounts(conn, account_id, int(target_id))
    except accounts_svc.AccountError as e:
        return RedirectResponse(f"/accounts?e={quote(str(e))}", status_code=303)
    msg = f"Merged: {result['moved']} transactions moved"
    if result["duplicates_removed"]:
        msg += f", {result['duplicates_removed']} duplicates removed"
    return RedirectResponse(f"/accounts?m={quote(msg + '.')}", status_code=303)


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
                   type: str = Form(""), action: str = Form("save")):
    if action == "toggle_archived":
        conn.execute("UPDATE accounts SET archived = 1 - archived WHERE id = ?",
                     (account_id,))
        conn.commit()
        return RedirectResponse("/accounts", status_code=303)

    # Type is editable: an account created as "checking" that is really a card
    # otherwise never gets the credit-card sign check at import.
    if name.strip():
        conn.execute("UPDATE accounts SET name = ? WHERE id = ?",
                     (name.strip(), account_id))
    if type in ACCOUNT_TYPES:
        conn.execute("UPDATE accounts SET type = ? WHERE id = ?",
                     (type, account_id))
    conn.commit()
    return RedirectResponse("/accounts?m=Saved.", status_code=303)
