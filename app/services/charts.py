"""Server-side SVG charts (no JS chart library, works offline).

Colors are applied via CSS classes defined in static/style.css so light/dark
mode swap automatically. Bars are thin with rounded data-ends anchored to the
baseline; native <title> elements provide value tooltips.
"""
from html import escape


def _drill(kind: str, month: str, key: str = "") -> str:
    """Attributes that turn a bar into a button opening what it's made of.

    The month travels with the bar rather than being taken from the page, so
    July's bar opens July even while the page header says August. Read by the
    same handler as the drillable rows in the templates.
    """
    attrs = (f' data-drill-kind="{kind}" data-drill-month="{escape(month, quote=True)}"'
             f' role="button" tabindex="0" aria-expanded="false"')
    if key:
        attrs += f' data-drill-key="{escape(key, quote=True)}"'
    return attrs


def _rounded_top_bar(x: float, y: float, w: float, h: float, r: float = 2.5) -> str:
    if h <= 0.5:
        return f"M{x:.1f},{y + h:.1f} h{w:.1f} v-{max(h, 0.5):.1f} h-{w:.1f} Z"
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f} "
            f"v-{h - r:.1f} q0,-{r:.1f} {r:.1f},-{r:.1f} "
            f"h{w - 2 * r:.1f} q{r:.1f},0 {r:.1f},{r:.1f} "
            f"v{h - r:.1f} Z")


def cashflow_chart(flow: list[dict], width: int = 360, height: int = 130) -> str:
    """Grouped bars: income vs spending per month (values in cents).

    Rendered at ~phone width so SVG text keeps a readable size when the
    responsive <svg> scales; on wide screens it scales up proportionally.
    """
    if not flow:
        return ""
    pad_left, pad_bottom, pad_top = 4, 16, 6
    plot_h = height - pad_bottom - pad_top
    max_val = max([f["income"] for f in flow] + [f["spent"] for f in flow] + [1])
    n = len(flow)
    group_w = (width - pad_left * 2) / n
    bar_w = min(10.0, (group_w - 5) / 2)
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Monthly income vs spending" '
             f'preserveAspectRatio="xMidYMid meet">']
    # hairline gridlines at 1/2 and max
    for frac in (0.5, 1.0):
        y = pad_top + plot_h * (1 - frac)
        parts.append(f'<line x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_left}" '
                     f'y2="{y:.1f}" class="chart-grid"/>')
    baseline_y = pad_top + plot_h
    parts.append(f'<line x1="{pad_left}" y1="{baseline_y:.1f}" '
                 f'x2="{width - pad_left}" y2="{baseline_y:.1f}" class="chart-axis"/>')
    for i, f in enumerate(flow):
        cx = pad_left + group_w * i + group_w / 2
        month_label = f["month"][5:7].lstrip("0") + "/" + f["month"][2:4]
        for j, (key, cls) in enumerate((("income", "bar-income"), ("spent", "bar-spent"))):
            val = max(0, f[key])
            h = plot_h * val / max_val
            x = cx - bar_w - 1 + j * (bar_w + 2)
            y = pad_top + plot_h - h
            title = (f"{f['month']}: {'income' if key == 'income' else 'spending'} "
                     f"{val / 100:,.2f}")
            drill = _drill("income" if key == "income" else "spending", f["month"])
            parts.append(f'<path d="{_rounded_top_bar(x, y, bar_w, h)}" class="{cls}"'
                         f'{drill}><title>{escape(title)}</title></path>')
        if n <= 6 or i % 3 == (n - 1) % 3:  # thin out labels, keep the last one
            parts.append(f'<text x="{cx:.1f}" y="{height - 4}" class="chart-label" '
                         f'text-anchor="middle">{escape(month_label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _month_tick(month: str) -> str:
    return month[5:7].lstrip("0") + "/" + month[2:4]


def pace_chart(pace: dict, width: int = 360, height: int = 160) -> str:
    """Cumulative spend this month vs last month, against a straight budget pace.

    Two data series (blue = this month, orange = last month) plus a neutral
    dashed reference line for the budget — a reference is not a series, so it
    stays grey rather than taking a categorical hue.
    """
    days = pace["days"]
    if days < 2:
        return ""
    pad_l, pad_r, pad_t, pad_b = 6, 6, 8, 18
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    peak = max(pace["this"] + pace["prev"] + [pace["budget"], 1])

    def x(day_index: int) -> float:
        return pad_l + plot_w * day_index / (days - 1)

    def y(value: int) -> float:
        return pad_t + plot_h * (1 - value / peak)

    def path(series: list[int], limit: int | None = None) -> str:
        pts = series[:limit] if limit else series
        if not pts:
            return ""
        return "M" + " L".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(pts))

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Cumulative spending this month versus last month" '
             f'preserveAspectRatio="xMidYMid meet">']
    for frac in (0.5, 1.0):
        gy = pad_t + plot_h * (1 - frac)
        parts.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" '
                     f'y2="{gy:.1f}" class="chart-grid"/>')
    parts.append(f'<line x1="{pad_l}" y1="{pad_t + plot_h:.1f}" '
                 f'x2="{width - pad_r}" y2="{pad_t + plot_h:.1f}" class="chart-axis"/>')

    if pace["budget"]:
        parts.append(f'<line x1="{x(0):.1f}" y1="{y(0):.1f}" x2="{x(days - 1):.1f}" '
                     f'y2="{y(pace["budget"]):.1f}" class="chart-ref">'
                     f'<title>Budget pace: {pace["budget"] / 100:,.2f} by day {days}</title></line>')
    if pace["prev"]:
        parts.append(f'<path d="{path(pace["prev"])}" class="line-prev"/>')
    if pace["this"]:
        parts.append(f'<path d="{path(pace["this"], pace["elapsed"])}" class="line-this"/>')
        i = pace["elapsed"] - 1
        parts.append(f'<circle cx="{x(i):.1f}" cy="{y(pace["this"][i]):.1f}" r="4.5" '
                     f'class="dot-this"><title>Day {pace["elapsed"]}: '
                     f'{pace["this"][i] / 100:,.2f} spent</title></circle>')

    for day in (1, days // 2, days):
        parts.append(f'<text x="{x(day - 1):.1f}" y="{height - 4}" class="chart-label" '
                     f'text-anchor="{"start" if day == 1 else ("end" if day == days else "middle")}">'
                     f'{day}</text>')
    parts.append("</svg>")
    return "".join(parts)


def net_bars_chart(nets: list[dict], width: int = 360, height: int = 130) -> str:
    """Monthly surplus/deficit around a zero baseline (diverging blue / red)."""
    if not nets:
        return ""
    pad_l, pad_t, pad_b = 4, 8, 16
    plot_h = height - pad_t - pad_b
    peak = max([abs(n["net"]) for n in nets] + [1])
    n = len(nets)
    slot = (width - pad_l * 2) / n
    bar_w = min(14.0, slot - 5)
    zero_y = pad_t + plot_h * (peak / (2 * peak))  # zero sits mid-plot

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Monthly surplus or deficit" '
             f'preserveAspectRatio="xMidYMid meet">']
    for i, entry in enumerate(nets):
        cx = pad_l + slot * i + slot / 2
        h = (plot_h / 2) * abs(entry["net"]) / peak
        surplus = entry["net"] >= 0
        y = zero_y - h if surplus else zero_y
        cls = "bar-surplus" if surplus else "bar-deficit"
        d = (_rounded_top_bar(cx - bar_w / 2, y, bar_w, h)
             if surplus else _rounded_bottom_bar(cx - bar_w / 2, y, bar_w, h))
        label = "surplus" if surplus else "deficit"
        parts.append(f'<path d="{d}" class="{cls}"{_drill("net", entry["month"])}>'
                     f'<title>{entry["month"]}: '
                     f'{abs(entry["net"]) / 100:,.2f} {label}</title></path>')
        if n <= 6 or i % 3 == (n - 1) % 3:
            parts.append(f'<text x="{cx:.1f}" y="{height - 3}" class="chart-label" '
                         f'text-anchor="middle">{escape(_month_tick(entry["month"]))}</text>')
    parts.append(f'<line x1="{pad_l}" y1="{zero_y:.1f}" x2="{width - pad_l}" '
                 f'y2="{zero_y:.1f}" class="chart-axis"/>')
    parts.append("</svg>")
    return "".join(parts)


def stacked_chart(composition: dict, width: int = 360, height: int = 165) -> str:
    """Monthly spend split by category, stacked. A 2px surface gap separates
    segments so adjacent hues never touch."""
    months = composition["months"]
    series = composition["series"]
    if not months or not series:
        return ""
    pad_l, pad_t, pad_b = 4, 8, 16
    plot_h = height - pad_t - pad_b
    totals = [sum(s["monthly"][i] for s in series) for i in range(len(months))]
    peak = max(totals + [1])
    n = len(months)
    slot = (width - pad_l * 2) / n
    bar_w = min(18.0, slot - 4)
    gap = 2.0

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Monthly spending by category" '
             f'preserveAspectRatio="xMidYMid meet">']
    baseline = pad_t + plot_h
    for i, month in enumerate(months):
        cx = pad_l + slot * i + slot / 2
        y_cursor = baseline
        for si, s in enumerate(series):
            value = s["monthly"][i]
            if value <= 0:
                continue
            h = plot_h * value / peak
            if h < 0.7:
                continue
            top = y_cursor - h
            # "Other" is a roll-up, so it opens as a roll-up: the key is the
            # month the chart is anchored at, which is what decides the ranking
            # and therefore which categories fell into it.
            drill = (_drill("other", month, months[-1]) if s["name"] == "Other"
                     else _drill("category", month, s["name"]))
            parts.append(
                f'<rect x="{cx - bar_w / 2:.1f}" y="{top:.1f}" width="{bar_w:.1f}" '
                f'height="{max(h - gap, 0.7):.1f}" rx="1.5" '
                f'class="series-{si % 7 + 1}"{drill}><title>{escape(month)} · '
                f'{escape(s["name"])}: {value / 100:,.2f}</title></rect>')
            y_cursor = top
        if n <= 6 or i % 3 == (n - 1) % 3:
            parts.append(f'<text x="{cx:.1f}" y="{height - 3}" class="chart-label" '
                         f'text-anchor="middle">{escape(_month_tick(month))}</text>')
    parts.append(f'<line x1="{pad_l}" y1="{baseline:.1f}" x2="{width - pad_l}" '
                 f'y2="{baseline:.1f}" class="chart-axis"/>')
    parts.append("</svg>")
    return "".join(parts)


def _rounded_bottom_bar(x: float, y: float, w: float, h: float, r: float = 2.5) -> str:
    """Bar hanging below a baseline: rounded at the bottom (the data end)."""
    if h <= 0.5:
        return f"M{x:.1f},{y:.1f} h{w:.1f} v{max(h, 0.5):.1f} h-{w:.1f} Z"
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y:.1f} "
            f"v{h - r:.1f} q0,{r:.1f} {r:.1f},{r:.1f} "
            f"h{w - 2 * r:.1f} q{r:.1f},0 {r:.1f},-{r:.1f} "
            f"v-{h - r:.1f} Z")


def spark_bars(series: list[int], width: int = 132, height: int = 34,
               months: list[str] | None = None, category: str = "") -> str:
    """Tiny single-series trend (one category's monthly spend, cents).

    Given the months the bars stand for, each one opens that month's charges
    for the category — the point of a six-month trend is the months that
    aren't this one.
    """
    if not series:
        return ""
    n = len(series)
    max_val = max(series + [1])
    gap = 3
    bar_w = (width - gap * (n - 1)) / n
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="trend" '
             f'preserveAspectRatio="xMidYMid meet">']
    for i, val in enumerate(series):
        h = (height - 2) * max(0, val) / max_val
        x = i * (bar_w + gap)
        y = height - h
        cls = "spark-bar current" if i == n - 1 else "spark-bar"
        month = months[i] if months and i < len(months) else ""
        drill = _drill("category", month, category) if month and category else ""
        label = f"{month}: {val / 100:,.2f}" if month else f"{val / 100:,.2f}"
        parts.append(f'<path d="{_rounded_top_bar(x, y, bar_w, h, 2)}" class="{cls}"'
                     f'{drill}><title>{escape(label)}</title></path>')
    parts.append("</svg>")
    return "".join(parts)
