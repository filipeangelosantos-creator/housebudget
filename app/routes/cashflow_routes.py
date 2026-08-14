"""Balances you enter, and the forecast that follows from them."""
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ..deps import (current_user, get_conn, parse_money_input, render,
                    verify_csrf)
from ..services import cashflow, schedules

router = APIRouter()

HORIZONS = (30, 60, 90, 180)


@router.get("/cashflow")
def cashflow_page(request: Request, conn=Depends(get_conn),
                  user=Depends(current_user), days: int = 90):
    horizon = days if days in HORIZONS else 90
    return render(request, conn, "cashflow.html",
                  accounts=cashflow.balances(conn),
                  forecast=cashflow.forecast(conn, horizon),
                  days=horizon, horizons=HORIZONS,
                  scheduled=len([s for s in schedules.all_schedules(conn)
                                 if s.amount_cents]),
                  today=date.today().isoformat())


@router.post("/cashflow/balances", dependencies=[Depends(verify_csrf)])
async def save_balances(request: Request, conn=Depends(get_conn),
                        user=Depends(current_user)):
    """Record what each account is worth today.

    A blank box clears the account rather than reading as zero — "I don't know"
    and "there is nothing in it" are different answers, and a forecast that
    treats the first as the second is confidently wrong.
    """
    form = await request.form()
    as_of = str(form.get("as_of", ""))
    try:
        date.fromisoformat(as_of)
    except ValueError:
        as_of = date.today().isoformat()
    for key, value in form.items():
        if not key.startswith("bal_"):
            continue
        account_id = key[4:]
        if not account_id.isdigit():
            continue
        text = str(value).strip()
        if not text:
            cashflow.clear_balance(conn, int(account_id))
            continue
        cashflow.set_balance(conn, int(account_id), as_of,
                             parse_money_input(text))
    conn.commit()
    return RedirectResponse("/cashflow", status_code=303)
