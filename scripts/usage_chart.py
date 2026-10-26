"""The billing report's charts: a self-contained HTML page and a Mermaid chart for the job summary.

The page shows one month: headline totals, the daily charge stacked by tenant (hover a day for its
breakdown), the charge per tenant, the cost per model, and the invoice as a table. It needs no network
access, so it opens straight from the downloaded artifact. Tenants keep the same colour in every chart;
the six largest get a colour each and the rest share "Other tenants". Unassigned usage is gray.
"""

from __future__ import annotations

import calendar
import html
import json
import math
from typing import Any

# The validated categorical palette (light, dark), in its fixed order.
SERIES = [
    ("#2a78d6", "#3987e5"),
    ("#eb6834", "#d95926"),
    ("#1baf7a", "#199e70"),
    ("#eda100", "#c98500"),
    ("#e87ba4", "#d55181"),
    ("#008300", "#008300"),
]
OTHER = ("#c3c2b7", "#4a4945")
UNASSIGNED = ("#898781", "#a3a198")
OTHER_TENANTS = "Other tenants"
UNASSIGNED_TENANT = "unassigned"

WIDTH, HEIGHT = 720, 260
LEFT, RIGHT, TOP, BOTTOM = 56, 8, 12, 28


def money(value: float) -> str:
    return f"{value:,.2f}" if abs(value) >= 0.995 or value == 0 else f"{value:.4f}"


def axis_label(value: float, step: float) -> str:
    """Tick labels share the decimal places the step needs, so the axis reads 0.00, 0.50, 1.00."""
    decimals = next(places for places in range(6) if abs(round(step, places) - step) < 1e-9)
    if step < 1:
        decimals = max(decimals, 2)
    return f"{value:,.{decimals}f}"


def _nice_step(maximum: float, ticks: int = 4) -> float:
    if maximum <= 0:
        return 1.0
    raw = maximum / ticks
    magnitude = 10 ** math.floor(math.log10(raw))
    for multiple in (1, 2, 2.5, 5, 10):
        if raw <= multiple * magnitude:
            return multiple * magnitude
    return 10 * magnitude


def series_for(invoice: list[dict[str, Any]]) -> list[tuple[str, tuple[str, str]]]:
    """Legend entries in stacking order: largest billable tenants, Other tenants, then unassigned."""
    billable = sorted((line for line in invoice if line["Tenant"] != UNASSIGNED_TENANT), key=lambda line: -line["Price"])
    named = [(line["Tenant"], SERIES[index]) for index, line in enumerate(billable[: len(SERIES)])]
    if len(billable) > len(SERIES):
        named.append((OTHER_TENANTS, OTHER))
    if any(line["Tenant"] == UNASSIGNED_TENANT for line in invoice):
        named.append((UNASSIGNED_TENANT, UNASSIGNED))
    return named


def daily_by_series(daily: dict[tuple[int, str], float], series: list[tuple[str, Any]]) -> dict[int, dict[str, float]]:
    names = {name for name, _ in series}
    days: dict[int, dict[str, float]] = {}
    for (day, tenant), price in daily.items():
        name = tenant if tenant in names else OTHER_TENANTS
        days.setdefault(day, {}).setdefault(name, 0.0)
        days[day][name] += price
    return days


def _column_path(x: float, y: float, width: float, height: float, radius: float) -> str:
    """A column segment whose top corners are rounded."""
    radius = min(radius, height, width / 2)
    return (
        f"M{x:.1f},{y + height:.1f}V{y + radius:.1f}Q{x:.1f},{y:.1f} {x + radius:.1f},{y:.1f}"
        f"H{x + width - radius:.1f}Q{x + width:.1f},{y:.1f} {x + width:.1f},{y + radius:.1f}V{y + height:.1f}Z"
    )


def daily_svg(month: str, days: dict[int, dict[str, float]], series: list[tuple[str, Any]]) -> str:
    year, number = (int(part) for part in month.split("-"))
    day_count = calendar.monthrange(year, number)[1]
    totals = {day: sum(values.values()) for day, values in days.items()}
    step = _nice_step(max(totals.values(), default=0))
    top = step * max(1, -(-max(totals.values(), default=0) // step))
    plot_width, plot_height = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    slot = plot_width / day_count
    bar = min(18.0, slot * 0.64)

    def y_of(value: float) -> float:
        return TOP + plot_height - value / top * plot_height

    parts = [f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="Daily charge by tenant for {month}">']
    tick = 0.0
    while tick <= top + 1e-9:
        y = y_of(tick)
        line_class = "baseline" if tick == 0 else "grid"
        parts.append(f'<line class="{line_class}" x1="{LEFT}" x2="{WIDTH - RIGHT}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{LEFT - 8}" y="{y + 4:.1f}" text-anchor="end">{axis_label(tick, step)}</text>')
        tick += step
    for day in range(1, day_count + 1):
        x = LEFT + (day - 1) * slot + (slot - bar) / 2
        if day == 1 or day % 5 == 0:
            parts.append(f'<text class="tick" x="{x + bar / 2:.1f}" y="{HEIGHT - 8}" text-anchor="middle">{day}</text>')
        stacked = [(name, days.get(day, {}).get(name, 0.0)) for name, _ in series]
        stacked = [(name, value) for name, value in stacked if value > 0]
        base = 0.0
        for index, (name, value) in enumerate(stacked):
            colour = f"var(--s{[n for n, _ in series].index(name)})"
            y, height = y_of(base + value), max(value / top * plot_height, 1.0)
            if index == len(stacked) - 1:
                parts.append(f'<path class="mark" fill="{colour}" d="{_column_path(x, y, bar, height, 4)}"/>')
            else:
                parts.append(f'<rect class="mark" fill="{colour}" x="{x:.1f}" y="{y:.1f}" width="{bar:.1f}" height="{height:.1f}"/>')
            base += value
        if stacked:
            parts.append(
                f'<rect class="hit" data-day="{day}" x="{LEFT + (day - 1) * slot:.1f}" y="{TOP}" '
                f'width="{slot:.1f}" height="{plot_height}"/>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _bars(rows: list[tuple[str, float, str]], currency: str) -> str:
    largest = max((value for _, value, _ in rows), default=0) or 1
    lines = []
    for label, value, colour in rows:
        width = max(value / largest * 100, 0.5)
        lines.append(
            f'<div class="bar-label">{html.escape(label)}</div>'
            f'<div class="bar-track"><div class="bar" style="width:{width:.2f}%;background:{colour}" '
            f'data-tip="{html.escape(label)}: {currency} {money(value)}"></div></div>'
            f'<div class="bar-value">{money(value)}</div>'
        )
    return '<div class="bars">' + "".join(lines) + "</div>"


def html_page(
    month: str,
    currency: str,
    markup: float,
    invoice: list[dict[str, Any]],
    detail: list[dict[str, Any]],
    daily: dict[tuple[int, str], float],
) -> str:
    series = series_for(invoice)
    names = [name for name, _ in series]
    days = daily_by_series(daily, series)
    totals = {
        "price": sum(line["Price"] for line in invoice),
        "cost": sum(line["Cost"] for line in invoice),
        "calls": sum(line["Calls"] for line in invoice),
        "tokens": sum(line["InputTokens"] + line["OutputTokens"] for line in invoice),
    }

    light = "".join(f"--s{index}:{colours[0]};" for index, (_, colours) in enumerate(series))
    dark = "".join(f"--s{index}:{colours[1]};" for index, (_, colours) in enumerate(series))

    by_tenant: dict[str, float] = {}
    for line in invoice:
        name = line["Tenant"] if line["Tenant"] in names else OTHER_TENANTS
        by_tenant[name] = by_tenant.get(name, 0.0) + line["Price"]
    tenant_rows = [(name, by_tenant[name], f"var(--s{names.index(name)})") for name in names if name in by_tenant]
    by_model: dict[str, float] = {}
    for line in detail:
        by_model[line["Model"]] = by_model.get(line["Model"], 0.0) + line["Cost"]
    model_rows = [(model, value, "var(--ink-2)") for model, value in sorted(by_model.items(), key=lambda item: -item[1])]

    legend = "".join(
        f'<span class="key"><span class="swatch" style="background:var(--s{index})"></span>{html.escape(name)}</span>'
        for index, name in enumerate(names)
    )
    table_rows = "".join(
        f"<tr><td>{html.escape(line['Tenant'])}</td><td>{line['Calls']:,}</td><td>{line['InputTokens']:,}</td>"
        f"<td>{line['OutputTokens']:,}</td><td>{money(line['Cost'])}</td><td>{money(line['Price'])}</td></tr>"
        for line in sorted(invoice, key=lambda line: -line["Price"])
    )
    day_data = {str(day): {name: round(value, 6) for name, value in values.items()} for day, values in days.items()}
    colour_index = {name: index for index, name in enumerate(names)}

    if not invoice:
        body = '<p class="empty">No usage was recorded this month.</p>'
    else:
        body = f"""
<section class="tiles">
  <div class="tile"><div class="tile-label">Charge</div><div class="tile-value">{currency} {money(totals['price'])}</div></div>
  <div class="tile"><div class="tile-label">Azure model cost</div><div class="tile-value">{currency} {money(totals['cost'])}</div></div>
  <div class="tile"><div class="tile-label">Model calls</div><div class="tile-value">{totals['calls']:,}</div></div>
  <div class="tile"><div class="tile-label">Tokens</div><div class="tile-value">{totals['tokens']:,}</div></div>
</section>
<section class="card">
  <h2>Daily charge by tenant</h2>
  <div class="legend">{legend}</div>
  <div class="chart">{daily_svg(month, days, series)}</div>
</section>
<div class="pair">
  <section class="card"><h2>Charge by tenant ({currency})</h2>{_bars(tenant_rows, currency)}</section>
  <section class="card"><h2>Azure cost by model ({currency})</h2>{_bars(model_rows, currency)}</section>
</div>
<section class="card">
  <h2>Invoice</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>Tenant</th><th>Calls</th><th>Input tokens</th><th>Output tokens</th><th>Cost</th><th>Charge</th></tr></thead>
    <tbody>{table_rows}</tbody>
  </table></div>
</section>"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Atlas usage {month}</title>
<style>
:root {{
  color-scheme: light;
  --surface: #fcfcfb; --page: #f9f9f7; --ink-1: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  {light}
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) {{
    color-scheme: dark;
    --surface: #1a1a19; --page: #0d0d0d; --ink-1: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    {dark}
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --surface: #1a1a19; --page: #0d0d0d; --ink-1: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  {dark}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--page); color: var(--ink-1); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 1040px; margin: 0 auto; padding: 24px 16px 48px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
h2 {{ font-size: 15px; margin: 0 0 12px; }}
.sub {{ color: var(--ink-2); margin: 0 0 20px; }}
.card, .tile {{ background: var(--surface); border-radius: 10px; box-shadow: 0 0 0 1px var(--ring); }}
.card {{ padding: 16px; margin-bottom: 16px; min-width: 0; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 16px; }}
.tile {{ padding: 14px 16px; min-width: 0; }}
.tile-label {{ color: var(--ink-2); font-size: 13px; }}
.tile-value {{ font-size: 24px; font-weight: 600; margin-top: 2px; }}
.pair {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr)); gap: 16px; margin-bottom: 16px; }}
.pair .card {{ margin-bottom: 0; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 6px 16px; margin-bottom: 8px; color: var(--ink-2); font-size: 13px; }}
.key {{ display: inline-flex; align-items: center; gap: 6px; }}
.swatch {{ width: 10px; height: 10px; border-radius: 3px; }}
.chart {{ overflow-x: auto; }}
.chart svg {{ width: 100%; min-width: 560px; height: auto; display: block; }}
.grid {{ stroke: var(--grid); stroke-width: 1; }}
.baseline {{ stroke: var(--axis); stroke-width: 1; }}
.tick {{ fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }}
.mark {{ stroke: var(--surface); stroke-width: 2; }}
.hit {{ fill: transparent; cursor: default; }}
.hit:hover {{ fill: var(--ink-1); fill-opacity: 0.04; }}
.bars {{ display: grid; grid-template-columns: minmax(0, max-content) minmax(60px, 1fr) auto; gap: 8px 10px; align-items: center; }}
.bar-label {{ color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 260px; min-width: 0; }}
.bar-track {{ height: 14px; }}
.bar {{ height: 100%; border-radius: 0 4px 4px 0; min-width: 2px; }}
.bar-value {{ font-variant-numeric: tabular-nums; color: var(--ink-1); }}
table {{ width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }}
th, td {{ text-align: right; padding: 6px 8px; border-bottom: 1px solid var(--grid); }}
th:first-child, td:first-child {{ text-align: left; }}
th {{ color: var(--ink-2); font-weight: 500; }}
.table-wrap {{ overflow-x: auto; }}
.empty {{ color: var(--ink-2); }}
#tip {{ position: fixed; pointer-events: none; background: var(--surface); color: var(--ink-1); box-shadow: 0 0 0 1px var(--ring), 0 4px 16px rgba(0,0,0,0.12);
  border-radius: 8px; padding: 8px 10px; font-size: 12px; display: none; z-index: 10; min-width: 160px; }}
#tip .row {{ display: flex; justify-content: space-between; gap: 12px; }}
#tip .row span:first-child {{ display: inline-flex; align-items: center; gap: 6px; color: var(--ink-2); }}
#tip .total {{ border-top: 1px solid var(--grid); margin-top: 4px; padding-top: 4px; font-weight: 600; }}
</style>
</head>
<body>
<main>
  <h1>Atlas usage for {month}</h1>
  <p class="sub">Charges in {currency}, at Azure model cost plus {markup:.0%}.</p>
  {body}
</main>
<div id="tip" role="tooltip"></div>
<script>
const days = {json.dumps(day_data)};
const colours = {json.dumps(colour_index)};
const currency = {json.dumps(currency)};
const tip = document.getElementById("tip");
const fmt = v => Math.abs(v) >= 0.995 || v === 0 ? v.toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}}) : v.toFixed(4);
const esc = s => s.replace(/[&<>"]/g, c => ({{"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}})[c]);
function place(event) {{
  const x = Math.min(event.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
  const y = Math.min(event.clientY + 14, window.innerHeight - tip.offsetHeight - 8);
  tip.style.left = x + "px"; tip.style.top = y + "px";
}}
document.querySelectorAll(".hit").forEach(hit => {{
  hit.addEventListener("mousemove", event => {{
    const entries = Object.entries(days[hit.dataset.day] || {{}}).sort((a, b) => b[1] - a[1]);
    const total = entries.reduce((sum, [, v]) => sum + v, 0);
    tip.innerHTML = `<div><strong>{month}-${{hit.dataset.day.padStart(2, "0")}}</strong></div>` +
      entries.map(([name, v]) => `<div class="row"><span><span class="swatch" style="background:var(--s${{colours[name]}})"></span>${{esc(name)}}</span><span>${{fmt(v)}}</span></div>`).join("") +
      `<div class="row total"><span>Total</span><span>${{currency}} ${{fmt(total)}}</span></div>`;
    tip.style.display = "block"; place(event);
  }});
  hit.addEventListener("mouseleave", () => {{ tip.style.display = "none"; }});
}});
document.querySelectorAll(".bar").forEach(bar => {{
  bar.addEventListener("mousemove", event => {{ tip.textContent = bar.dataset.tip; tip.style.display = "block"; place(event); }});
  bar.addEventListener("mouseleave", () => {{ tip.style.display = "none"; }});
}});
</script>
</body>
</html>
"""


def mermaid_chart(currency: str, invoice: list[dict[str, Any]]) -> str:
    """A charge-by-tenant bar chart for the GitHub job summary, which renders Mermaid."""
    lines = sorted(invoice, key=lambda line: -line["Price"])
    if not lines:
        return ""
    labels = ", ".join(json.dumps(line["Tenant"]) for line in lines)
    values = ", ".join(f"{line['Price']:.4f}" for line in lines)
    return (
        "\n```mermaid\nxychart-beta\n"
        f'    title "Charge by tenant ({currency})"\n'
        f"    x-axis [{labels}]\n"
        f'    y-axis "{currency}"\n'
        f"    bar [{values}]\n```\n"
    )
