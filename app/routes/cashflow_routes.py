"""Balances you enter, and the forecast that follows from them."""
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ..deps import (current_user, get_conn, parse_money_input, render,
                    verify_csrf)
from ..services import cashflow, commitments, schedules

router = APIRouter()

HORIZONS = (30, 60, 90, 180)


@router.get("/cashflow")
def cashflow_page(request: Request, conn=Depends(get_conn),
                  user=Depends(current_user), days: int = 90):
    horizon = days if days in HORIZONS else 90
    return render(request, conn, "cashflow.html",
                  accounts=cashflow.balances(conn),
                  forecast=cashflow.forecast(conn, horizon),
                  position=commitments.position(conn),
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


@router.post("/cashflow/commitments", dependencies=[Depends(verify_csrf)])
def save_commitment(request: Request, conn=Depends(get_conn),
                    user=Depends(current_user), name: str = Form(""),
                    kind: str = Form("payoff"), due_date: str = Form(""),
                    account_id: str = Form(""), target: str = Form(""),
                    note: str = Form(""), commitment_id: str = Form(""),
                    delete: str = Form("")):
    if delete and commitment_id.isdigit():
        commitments.remove(conn, int(commitment_id))
        conn.commit()
        return RedirectResponse("/cashflow", status_code=303)
    try:
        date.fromisoformat(due_date)
    except ValueError:
        return RedirectResponse("/cashflow", status_code=303)
    if kind not in ("payoff", "goal") or not name.strip():
        return RedirectResponse("/cashflow", status_code=303)
    commitments.save(
        conn, name, kind, due_date,
        account_id=int(account_id) if account_id.isdigit() else None,
        target_cents=parse_money_input(target), note=note,
        commitment_id=int(commitment_id) if commitment_id.isdigit() else None)
    conn.commit()
    return RedirectResponse("/cashflow", status_code=303)
