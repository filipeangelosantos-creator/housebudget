"""The plan: what you tell the app about when money moves.

Everything else on the site infers cadence from statements, which is a good
guess and never a certainty. This is where a guess becomes a fact.
"""
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from ..deps import (current_user, get_conn, parse_money_input, render,
                    verify_csrf)
from ..services import insights, schedules
from .dashboard import clean_month
from .transactions import _categories_grouped

router = APIRouter()


@router.get("/plan")
def plan_page(request: Request, conn=Depends(get_conn), user=Depends(current_user),
              category: int | None = None, edit: int | None = None,
              month: str | None = None):
    m = clean_month(month)
    declared = schedules.all_schedules(conn)
    claimed = {s.category_id for s in declared}
    editing = schedules.get(conn, edit) if edit else None

    # What the app worked out on its own and you haven't confirmed. Turning a
    # detection into a declaration is the cheapest way to fill this page in,
    # and it is also the only way to correct one that is wrong.
    suggestions = []
    for b in insights.periodic_bills(conn, m):
        if b.declared or b.category_id in claimed:
            continue
        suggestions.append({
            "category_id": b.category_id, "category": b.category,
            "what": b.merchant.title(), "cadence": _cadence_for(b.months_per),
            "amount_cents": b.typical_cents, "anchor": b.last_date.isoformat(),
            "why": f"{b.times_seen} charges, {b.cadence}"})
    for s in insights.pay_streams(conn, m):
        cat = conn.execute("SELECT id FROM categories WHERE name = ?",
                           (s.category,)).fetchone()
        if not cat or cat["id"] in claimed:
            continue
        suggestions.append({
            "category_id": cat["id"], "category": s.category,
            "what": s.name, "cadence": s.cadence,
            "amount_cents": s.typical_cents, "anchor": s.last_date.isoformat(),
            "why": f"paid {s.cadence_label}"})

    return render(request, conn, "plan.html",
                  month=m, declared=declared, suggestions=suggestions,
                  categories=_categories_grouped(conn),
                  editing=editing,
                  preset_category=(editing.category_id if editing
                                   else (category or 0)),
                  cadences=schedules.CADENCES,
                  today=date.today().isoformat(), today_date=date.today())


def _cadence_for(months: int) -> str:
    return {1: "monthly", 2: "every_2_months", 3: "quarterly",
            6: "semiannual", 12: "yearly"}.get(months, "monthly")


@router.post("/plan/save", dependencies=[Depends(verify_csrf)])
def plan_save(request: Request, conn=Depends(get_conn), user=Depends(current_user),
              category_id: int = Form(...), cadence: str = Form(...),
              amount: str = Form(""), anchor_date: str = Form(""),
              name: str = Form(""), note: str = Form(""),
              schedule_id: str = Form(""), back: str = Form("/plan")):
    if cadence not in schedules.CADENCES:
        return RedirectResponse("/plan", status_code=303)
    anchor = anchor_date if _is_date(anchor_date) else date.today().isoformat()
    schedules.save(conn, category_id, cadence, parse_money_input(amount), anchor,
                   name=name, note=note,
                   schedule_id=int(schedule_id) if schedule_id.isdigit() else None)
    conn.commit()
    return RedirectResponse(_safe(back), status_code=303)


@router.post("/plan/remove", dependencies=[Depends(verify_csrf)])
def plan_remove(request: Request, conn=Depends(get_conn), user=Depends(current_user),
                schedule_id: int = Form(...), back: str = Form("/plan")):
    schedules.clear(conn, schedule_id)
    conn.commit()
    return RedirectResponse(_safe(back), status_code=303)


def _is_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
        return True
    except ValueError:
        return False


def _safe(path: str) -> str:
    return path if path.startswith("/") and not path.startswith("//") else "/plan"
