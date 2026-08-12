"""Insights page: trends, merchants, recurring charges, anomalies."""
from fastapi import APIRouter, Depends, Request

from ..deps import current_user, get_conn, render
from ..services import budgets, insights
from ..services.charts import cashflow_chart, spark_bars
from .dashboard import clean_month

router = APIRouter()


@router.get("/insights")
def insights_page(request: Request, conn=Depends(get_conn),
                  user=Depends(current_user), month: str | None = None):
    m = clean_month(month)
    flow = insights.cashflow(conn, m, 12)
    trends = insights.category_trends(conn, m, 6)
    for t in trends:
        t["svg"] = spark_bars(t["series"])
    summary = budgets.month_summary(conn, m)
    return render(request, conn, "insights.html",
                  month=m, month_label=budgets.month_label(m),
                  prev_month=budgets.shift_month(m, -1),
                  next_month=budgets.shift_month(m, 1),
                  cashflow=flow, cashflow_svg=cashflow_chart(flow),
                  trends=trends,
                  merchants=insights.top_merchants(conn, m),
                  largest=insights.largest_transactions(conn, m),
                  recurring=insights.recurring_charges(conn, m),
                  alerts=insights.anomalies(conn, m),
                  summary=summary)
