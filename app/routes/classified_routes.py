"""Reviewing the categories the app picked, merchant by merchant."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from ..deps import current_user, get_conn, render, verify_csrf
from ..services import classified
from .dashboard import clean_month

router = APIRouter()

SOURCES = ("auto", "user", "all")


def _clean(month: str, account: str, source: str):
    m = clean_month(month) if month and month.strip() not in ("", "all") else None
    return (m, int(account) if account.isdigit() else None,
            source if source in SOURCES else "auto")


@router.get("/classification")
def classification_page(request: Request, conn=Depends(get_conn),
                        user=Depends(current_user), month: str = "all",
                        account: str = "", source: str = "auto",
                        q: str = "", expand: str = ""):
    m, acc, src = _clean(month, account, source)
    groups = classified.merchant_groups(conn, month=m, account=acc, source=src, q=q)
    detail = (classified.group_transactions(conn, expand, month=m, account=acc)
              if expand else [])
    accounts = conn.execute(
        "SELECT id, name FROM accounts WHERE archived = 0 ORDER BY name").fetchall()
    categories = conn.execute(
        "SELECT c.id, c.name, g.name AS group_name FROM categories c "
        "JOIN category_groups g ON g.id = c.group_id WHERE c.archived = 0 "
        "ORDER BY g.sort_order, g.id, c.sort_order, c.id").fetchall()
    return render(request, conn, "classification.html",
                  groups=groups, categories=categories, accounts=accounts,
                  month="" if m is None else m, account=acc, source=src, q=q,
                  expand=expand, detail=detail,
                  covered=sum(g["n"] for g in groups))


@router.post("/classification/save", dependencies=[Depends(verify_csrf)])
async def classification_save(request: Request, conn=Depends(get_conn),
                              user=Depends(current_user)):
    """Apply every merchant whose category was changed on the page."""
    form = await request.form()
    m, acc, src = _clean(str(form.get("f_month", "")), str(form.get("f_account", "")),
                         str(form.get("f_source", "auto")))
    changed = moved = 0
    for key in form.keys():
        if not key.startswith("cat_"):
            continue
        idx = key[4:]
        chosen = str(form.get(key, ""))
        merchant = str(form.get(f"mk_{idx}", ""))
        was = str(form.get(f"was_{idx}", ""))
        if not merchant or not chosen.isdigit() or chosen == was:
            continue
        n = classified.refile(conn, merchant, int(chosen),
                              remember=bool(form.get(f"remember_{idx}")),
                              month=m, account=acc)
        if n:
            changed += 1
            moved += n
    where = f"/classification?month={m or 'all'}&source={src}"
    if acc:
        where += f"&account={acc}"
    if changed:
        where += (f"&m=Re-filed+{moved}+transaction{'' if moved == 1 else 's'}"
                  f"+across+{changed}+merchant{'' if changed == 1 else 's'}.")
    return RedirectResponse(where, status_code=303)
