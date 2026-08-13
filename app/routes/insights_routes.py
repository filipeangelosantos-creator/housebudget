"""Insights page: pace, trends, composition, merchants, recurring, anomalies."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse

from ..deps import current_user, get_conn, render, verify_csrf
from ..services import budgets, drill, insights, splits as splits_svc, transfers
from ..services.charts import (cashflow_chart, net_bars_chart, pace_chart,
                               spark_bars, stacked_chart)
from .dashboard import clean_month
from .transactions import _categories_grouped

router = APIRouter()


def _ordinal(day: int) -> str:
    """13 -> '13th'. The teens all take 'th', which the last digit alone gets
    wrong for 11, 12 and 13."""
    if 11 <= day % 100 <= 13:
        return f"{day}th"
    return f"{day}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th') }"


@router.get("/insights")
def insights_page(request: Request, conn=Depends(get_conn),
                  user=Depends(current_user), month: str | None = None,
                  applied: int | None = None):
    m = clean_month(month)
    applied_row = conn.execute("SELECT name FROM categories WHERE id = ?",
                               (applied,)).fetchone() if applied else None
    flow = insights.cashflow(conn, m, 12)
    trends = insights.category_trends(conn, m, 6)
    for t in trends:
        t["svg"] = spark_bars(t["series"], months=t["months"], category=t["name"])
    pace = insights.spending_pace(conn, m)
    nets = insights.monthly_net(conn, m, 12)
    composition = insights.category_composition(conn, m, 12)
    summary = budgets.month_summary(conn, m)

    months_with_data = conn.execute(
        "SELECT COUNT(DISTINCT substr(date,1,7)) FROM transactions").fetchone()[0]
    is_current_month = m == budgets.current_month()
    return render(request, conn, "insights.html",
                  month=m, month_label=budgets.month_label(m),
                  prev_month=budgets.shift_month(m, -1),
                  next_month=budgets.shift_month(m, 1),
                  prev_month_label=budgets.month_label(budgets.shift_month(m, -1)),
                  cashflow=flow, cashflow_svg=cashflow_chart(flow),
                  pace=pace, pace_svg=pace_chart(pace),
                  nets=nets, net_svg=net_bars_chart(nets),
                  composition=composition, stacked_svg=stacked_chart(composition),
                  trends=trends,
                  movers=insights.biggest_movers(conn, m),
                  yoy=insights.year_over_year(conn, m),
                  merchants=insights.top_merchants(conn, m),
                  largest=insights.largest_transactions(conn, m),
                  recurring=insights.recurring_charges(conn, m),
                  alerts=insights.anomalies(conn, m),
                  review=insights.budget_review(conn, m),
                  bills=insights.periodic_bills(conn, m),
                  applied_label=applied_row["name"] if applied_row else "",
                  unmatched=transfers.unmatched_transfers(conn, m),
                  months_with_data=months_with_data,
                  is_current_month=is_current_month,
                  summary=summary)


@router.get("/insights/drill")
def insights_drill(request: Request, conn=Depends(get_conn),
                   user=Depends(current_user), kind: str = "", key: str = "",
                   month: str | None = None, page_month: str | None = None,
                   day: int = 0):
    """The transactions behind one figure, as a fragment the page splices in.

    HTML rather than JSON so the rows are rendered by the same template the
    rest of the app uses, and so a link out to Activity comes for free.

    `month` is the period the figure covers, which is not always the month the
    page is showing: a bar for July opens July while the header still says
    August. `page_month` is the header's month, and only decides whether the
    panel has to spell out which period these rows are from.
    """
    m = clean_month(month)
    day = day if 1 <= day <= 31 else 0
    rows = drill.rows_for(conn, kind, m, key, day)
    # A subscription is deliberately shown across every month it appears in,
    # so labelling it with one month would be a lie. A part-month figure has to
    # say where it stops, or the rows read as the whole month's.
    period = "" if kind == "recurring" else budgets.month_label(m)
    if period and day:
        period += f", to the {_ordinal(day)}"
    return render(request, conn, "_drill.html",
                  rows=rows, truncated=len(rows) >= drill.LIMIT, kind=kind,
                  title="Other" if kind == "other" else
                  (key if kind in ("category", "merchant", "recurring") else ""),
                  period=period, month=m, month_label=budgets.month_label(m),
                  elsewhere=bool(page_month) and clean_month(page_month) != m,
                  categories=_categories_grouped(conn),
                  more_link=drill.list_link(kind, m, key),
                  back=f"/insights?month={clean_month(page_month or m)}")


@router.post("/insights/drill/categorize", dependencies=[Depends(verify_csrf)])
def insights_drill_categorize(request: Request, conn=Depends(get_conn),
                              user=Depends(current_user), txn_id: int = Form(...),
                              category_id: str = Form("")):
    """Re-file one transaction from inside a drill panel.

    Only the category: the full edit page owns notes, rules and splitting. A
    split transaction is refused rather than quietly collapsed into one
    category — that would throw away the parts, and from a select tucked in a
    panel there is nothing to warn you it happened.
    """
    row = conn.execute("SELECT id FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if splits_svc.has_splits(conn, txn_id):
        raise HTTPException(status_code=409,
                            detail="This one is split — open it to change the split.")
    cat = int(category_id) if category_id.isdigit() else None
    conn.execute(
        "UPDATE transactions SET category_id = ?, needs_review = 0, "
        "classified_by = 'user', rule_id = NULL WHERE id = ?", (cat, txn_id))
    conn.commit()
    name = conn.execute("SELECT name FROM categories WHERE id = ?",
                        (cat,)).fetchone() if cat else None
    return JSONResponse({"ok": True, "category": name["name"] if name else ""})
