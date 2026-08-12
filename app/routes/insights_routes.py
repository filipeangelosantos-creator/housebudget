"""Insights page: pace, trends, composition, merchants, recurring, anomalies."""
from fastapi import APIRouter, Depends, Request

from ..deps import current_user, get_conn, render
from ..services import budgets, insights, transfers
from ..services.charts import (cashflow_chart, net_bars_chart, pace_chart,
                               spark_bars, stacked_chart)
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
                  unmatched=transfers.unmatched_transfers(conn, m),
                  months_with_data=months_with_data,
                  is_current_month=is_current_month,
                  summary=summary)
