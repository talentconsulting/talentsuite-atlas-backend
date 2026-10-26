"""Price one month of the usage ledger per tenant, for invoicing.

The generator agents add a row to the environment's usage table for every model call: the month, the
tenant (its data repository), the agent, the model deployment and the token counts. This script reads one
month's rows, totals them per tenant and model, prices them with the rate card and writes:

    invoice.csv   one line per tenant: calls, tokens, cost and the price to charge
    detail.csv    one line per tenant and model
    summary.md    the invoice table and a charge-by-tenant chart, also appended to the GitHub job summary
    usage.html    charts of the month's usage: daily charge by tenant, charge by tenant, cost by model

Cost is (uncached input x input rate + cached input x cached rate + output x output rate) per million tokens.
Price is cost x (1 + markup). A model without a rate stops the report rather than pricing it at zero.

Required environment variables:
    USAGE_QUERY_URL   SAS URL for the usage table with read permission.

Optional environment variables:
    BILLING_MONTH     yyyy-mm to report. Defaults to the previous calendar month.
    RATES_FILE        Rate card. Defaults to billing/rates.json.
    OUTPUT_DIR        Where the files are written. Defaults to billing-report.
"""

from __future__ import annotations

import csv
import datetime
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any, Callable, Iterable

import usage_chart

TOKEN_FIELDS = ("InputTokens", "CachedInputTokens", "OutputTokens")


class ReportError(Exception):
    pass


def previous_month(today: datetime.date) -> str:
    return (today.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")


def query_url(table_sas_url: str, month: str, next_keys: tuple[str, str] | None = None) -> str:
    """The entity query for one month's partition, from a SAS URL for the table."""
    parsed = urllib.parse.urlsplit(table_sas_url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("$filter", f"PartitionKey eq '{month}'"))
    if next_keys:
        query += [("NextPartitionKey", next_keys[0]), ("NextRowKey", next_keys[1])]
    path = parsed.path.rstrip("/") + "()"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, urllib.parse.urlencode(query), ""))


def _get(url: str) -> tuple[dict[str, Any], dict[str, str]]:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json;odata=nometadata", "x-ms-version": "2019-02-02"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read()), dict(response.headers)
    except urllib.error.HTTPError as error:
        # The URL carries the SAS token, so it is never printed.
        raise ReportError(f"The usage table returned HTTP {error.code}.") from error
    except urllib.error.URLError as error:
        raise ReportError("The usage table could not be reached.") from error


def fetch_rows(
    table_sas_url: str, month: str, get: Callable[[str], tuple[dict[str, Any], dict[str, str]]] = _get
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    next_keys = None
    while True:
        body, headers = get(query_url(table_sas_url, month, next_keys))
        rows.extend(body.get("value", []))
        headers = {key.lower(): value for key, value in headers.items()}
        partition = headers.get("x-ms-continuation-nextpartitionkey")
        if not partition:
            return rows
        next_keys = (partition, headers.get("x-ms-continuation-nextrowkey", ""))


def load_rates(path: pathlib.Path) -> dict[str, Any]:
    rates = json.loads(path.read_text())
    if not isinstance(rates.get("models"), dict) or not isinstance(rates.get("markup"), (int, float)):
        raise ReportError(f"{path} needs a models object and a numeric markup.")
    return rates


def model_cost(tokens: dict[str, int], rate: dict[str, float]) -> float:
    cached = min(tokens["CachedInputTokens"], tokens["InputTokens"])
    return (
        (tokens["InputTokens"] - cached) * rate["input"]
        + cached * rate.get("cachedInput", rate["input"])
        + tokens["OutputTokens"] * rate["output"]
    ) / 1_000_000


def summarise(rows: Iterable[dict[str, Any]], rates: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    totals: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"Calls": 0, **{field: 0 for field in TOKEN_FIELDS}})
    for row in rows:
        entry = totals[(str(row.get("Tenant", "unassigned")), str(row.get("Model", "")))]
        entry["Calls"] += 1
        for field in TOKEN_FIELDS:
            entry[field] += int(row.get(field, 0) or 0)

    missing = sorted({model for _, model in totals if model not in rates["models"]})
    if missing:
        raise ReportError(f"No rate for model {', '.join(repr(model) for model in missing)} in the rate card.")

    markup = float(rates["markup"])
    detail = []
    for (tenant, model), tokens in sorted(totals.items()):
        cost = model_cost(tokens, rates["models"][model])
        detail.append({"Tenant": tenant, "Model": model, **tokens, "Cost": cost, "Price": cost * (1 + markup)})

    invoice: dict[str, dict[str, Any]] = {}
    for line in detail:
        entry = invoice.setdefault(line["Tenant"], {"Tenant": line["Tenant"], "Calls": 0, **{f: 0 for f in TOKEN_FIELDS}, "Cost": 0.0, "Price": 0.0})
        for field in ("Calls", *TOKEN_FIELDS, "Cost", "Price"):
            entry[field] += line[field]
    return list(invoice.values()), detail


def row_day(row: dict[str, Any]) -> int:
    """Day of the month a row was recorded: from the table's Timestamp, else the time in its RowKey."""
    timestamp = str(row.get("Timestamp", ""))
    if re.match(r"\d{4}-\d{2}-\d{2}", timestamp):
        return int(timestamp[8:10])
    parts = str(row.get("RowKey", "")).split("|")
    return int(parts[1][6:8]) if len(parts) > 1 and parts[1][:8].isdigit() else 1


def daily_charges(rows: Iterable[dict[str, Any]], rates: dict[str, Any]) -> dict[tuple[int, str], float]:
    """The charge per day and tenant. Call after summarise, which rejects models without a rate."""
    markup = float(rates["markup"])
    daily: dict[tuple[int, str], float] = defaultdict(float)
    for row in rows:
        tokens = {field: int(row.get(field, 0) or 0) for field in TOKEN_FIELDS}
        cost = model_cost(tokens, rates["models"][str(row.get("Model", ""))])
        daily[(row_day(row), str(row.get("Tenant", "unassigned")))] += cost * (1 + markup)
    return dict(daily)


def _write_csv(path: pathlib.Path, lines: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for line in lines:
            writer.writerow({key: f"{value:.4f}" if isinstance(value, float) else value for key, value in line.items()})


def markdown(month: str, currency: str, markup: float, invoice: list[dict[str, Any]]) -> str:
    lines = [
        f"## Usage invoice for {month}",
        "",
        f"Prices in {currency}, at cost plus {markup:.0%}.",
        "",
        "| Tenant | Calls | Input tokens | Output tokens | Cost | Price |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for line in invoice:
        lines.append(
            f"| {line['Tenant']} | {line['Calls']:,} | {line['InputTokens']:,} | {line['OutputTokens']:,} "
            f"| {line['Cost']:,.2f} | {line['Price']:,.2f} |"
        )
    if not invoice:
        lines.append("| No usage recorded | | | | | |")
    return "\n".join(lines) + "\n"


def main() -> int:
    table_sas_url = os.environ.get("USAGE_QUERY_URL", "").strip()
    if not table_sas_url:
        print("USAGE_QUERY_URL is required.", file=sys.stderr)
        return 1
    month = os.environ.get("BILLING_MONTH", "").strip() or previous_month(datetime.date.today())
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        print(f"BILLING_MONTH must be yyyy-mm, got {month!r}.", file=sys.stderr)
        return 1
    output = pathlib.Path(os.environ.get("OUTPUT_DIR", "billing-report"))
    try:
        rates = load_rates(pathlib.Path(os.environ.get("RATES_FILE", "billing/rates.json")))
        rows = fetch_rows(table_sas_url, month)
        invoice, detail = summarise(rows, rates)
    except ReportError as error:
        print(error, file=sys.stderr)
        return 1

    output.mkdir(parents=True, exist_ok=True)
    totals = ["Calls", *TOKEN_FIELDS, "Cost", "Price"]
    _write_csv(output / "invoice.csv", invoice, ["Tenant", *totals])
    _write_csv(output / "detail.csv", detail, ["Tenant", "Model", *totals])
    currency, markup = str(rates.get("currency", "")), float(rates["markup"])
    page = usage_chart.html_page(month, currency, markup, invoice, detail, daily_charges(rows, rates))
    (output / "usage.html").write_text(page)
    summary = markdown(month, currency, markup, invoice) + usage_chart.mermaid_chart(currency, invoice)
    (output / "summary.md").write_text(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as handle:
            handle.write(summary)
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
