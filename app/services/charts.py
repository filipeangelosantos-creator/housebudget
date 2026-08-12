"""Server-side SVG charts (no JS chart library, works offline).

Colors are applied via CSS classes defined in static/style.css so light/dark
mode swap automatically. Bars are thin with rounded data-ends anchored to the
baseline; native <title> elements provide value tooltips.
"""
from html import escape


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
            parts.append(f'<path d="{_rounded_top_bar(x, y, bar_w, h)}" class="{cls}">'
                         f'<title>{escape(title)}</title></path>')
        if n <= 6 or i % 3 == (n - 1) % 3:  # thin out labels, keep the last one
            parts.append(f'<text x="{cx:.1f}" y="{height - 4}" class="chart-label" '
                         f'text-anchor="middle">{escape(month_label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def spark_bars(series: list[int], width: int = 132, height: int = 34) -> str:
    """Tiny single-series trend (one category's monthly spend, cents)."""
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
        parts.append(f'<path d="{_rounded_top_bar(x, y, bar_w, h, 2)}" class="{cls}">'
                     f'<title>{val / 100:,.2f}</title></path>')
    parts.append("</svg>")
    return "".join(parts)
