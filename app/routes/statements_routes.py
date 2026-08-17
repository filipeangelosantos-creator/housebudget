"""Which accounts have reported for a month, and saying when one is done."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ..deps import current_user, get_conn, render, verify_csrf
from ..services import budgets, coverage
from .dashboard import clean_month

router = APIRouter()


@router.get("/statements")
def statements_page(request: Request, conn=Depends(get_conn),
                    user=Depends(current_user), month: str | None = None,
                    closed: int | None = None):
    m = clean_month(month)
    return render(request, conn, "statements.html",
                  month=m, month_label=budgets.month_label(m),
                  prev_month=budgets.shift_month(m, -1),
                  next_month=budgets.shift_month(m, 1),
                  cov=coverage.for_month(conn, m),
                  gaps=coverage.months_with_gaps(conn, budgets.shift_month(m, -1)),
                  closed_n=closed or 0)


@router.post("/statements/mark", dependencies=[Depends(verify_csrf)])
def statements_mark(request: Request, conn=Depends(get_conn),
                    user=Depends(current_user), account_id: int = Form(...),
                    month: str = Form(...), state: str = Form(""),
                    note: str = Form(""), back: str = Form("/statements")):
    m = clean_month(month)
    coverage.mark(conn, account_id, m, state, note)
    conn.commit()
    target = back if back.startswith("/") and not back.startswith("//") else "/statements"
    joiner = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{joiner}month={m}", status_code=303)


@router.post("/statements/close-all", dependencies=[Depends(verify_csrf)])
def statements_close_all(request: Request, conn=Depends(get_conn),
                         user=Depends(current_user), month: str = Form(...)):
    m = clean_month(month)
    n = coverage.close_all(conn, m)
    conn.commit()
    return RedirectResponse(f"/statements?month={m}&closed={n}", status_code=303)
