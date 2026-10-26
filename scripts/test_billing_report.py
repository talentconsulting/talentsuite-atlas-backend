import datetime
import json
import os
import pathlib
import tempfile
import unittest
import urllib.parse
from unittest import mock

import billing_report
import usage_chart

TABLE_URL = "https://ledger.table.core.windows.net/usage?sv=2023&sp=r&sig=abc%2Bdef"
RATES = {
    "currency": "USD",
    "markup": 0.25,
    "models": {
        "gpt-4o": {"input": 2.50, "cachedInput": 1.25, "output": 10.00},
        "gpt-4.1": {"input": 2.00, "cachedInput": 0.50, "output": 8.00},
    },
}


def row(tenant, model, input_tokens, output_tokens, cached=0):
    return {"Tenant": tenant, "Model": model, "InputTokens": input_tokens, "CachedInputTokens": cached, "OutputTokens": output_tokens}


class MonthTests(unittest.TestCase):
    def test_previous_month_crosses_the_year(self):
        self.assertEqual(billing_report.previous_month(datetime.date(2027, 1, 15)), "2026-12")
        self.assertEqual(billing_report.previous_month(datetime.date(2026, 11, 1)), "2026-10")


class QueryTests(unittest.TestCase):
    def test_queries_one_partition_and_keeps_the_sas(self):
        url = urllib.parse.urlsplit(billing_report.query_url(TABLE_URL, "2026-10"))
        self.assertEqual(url.path, "/usage()")
        query = dict(urllib.parse.parse_qsl(url.query))
        self.assertEqual(query["$filter"], "PartitionKey eq '2026-10'")
        self.assertEqual(query["sig"], "abc+def")

    def test_follows_continuation_headers(self):
        pages = [
            ({"value": [row("a/b", "gpt-4o", 1, 1)]}, {"x-ms-continuation-NextPartitionKey": "2026-10", "x-ms-continuation-NextRowKey": "k2"}),
            ({"value": [row("a/b", "gpt-4o", 2, 2)]}, {}),
        ]
        urls = []

        def get(url):
            urls.append(url)
            return pages[len(urls) - 1]

        rows = billing_report.fetch_rows(TABLE_URL, "2026-10", get=get)
        self.assertEqual(len(rows), 2)
        second = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(urls[1]).query))
        self.assertEqual((second["NextPartitionKey"], second["NextRowKey"]), ("2026-10", "k2"))


class SummaryTests(unittest.TestCase):
    def test_prices_each_tenant_at_cost_plus_markup(self):
        rows = [
            row("acme/catalog", "gpt-4o", 1_000_000, 100_000, cached=200_000),
            row("acme/catalog", "gpt-4.1", 500_000, 50_000),
            row("globex/atlas", "gpt-4o", 10_000, 1_000),
        ]
        invoice, detail = billing_report.summarise(rows, RATES)
        acme_4o = next(line for line in detail if line["Tenant"] == "acme/catalog" and line["Model"] == "gpt-4o")
        # 800k uncached x 2.50 + 200k cached x 1.25 + 100k output x 10.00, per million.
        self.assertAlmostEqual(acme_4o["Cost"], 2.0 + 0.25 + 1.0)
        acme = next(line for line in invoice if line["Tenant"] == "acme/catalog")
        self.assertEqual(acme["Calls"], 2)
        self.assertAlmostEqual(acme["Cost"], 3.25 + 1.0 + 0.4)
        self.assertAlmostEqual(acme["Price"], acme["Cost"] * 1.25)
        self.assertEqual([line["Tenant"] for line in invoice], ["acme/catalog", "globex/atlas"])

    def test_a_model_without_a_rate_stops_the_report(self):
        with self.assertRaisesRegex(billing_report.ReportError, "gpt-5"):
            billing_report.summarise([row("acme/catalog", "gpt-5", 1, 1)], RATES)

    def test_no_usage_gives_an_empty_invoice(self):
        self.assertEqual(billing_report.summarise([], RATES), ([], []))


class ChartTests(unittest.TestCase):
    def test_takes_the_day_from_the_timestamp_or_the_row_key(self):
        self.assertEqual(billing_report.row_day({"Timestamp": "2026-10-26T15:53:25.74Z"}), 26)
        self.assertEqual(billing_report.row_day({"RowKey": "acme:catalog|20261003T101010000000Z|abc"}), 3)

    def test_daily_charges_are_priced_per_day_and_tenant(self):
        rows = [
            {**row("acme/catalog", "gpt-4o", 1_000_000, 0), "Timestamp": "2026-10-02T09:00:00Z"},
            {**row("acme/catalog", "gpt-4o", 1_000_000, 0), "Timestamp": "2026-10-02T17:00:00Z"},
            {**row("globex/atlas", "gpt-4o", 1_000_000, 0), "Timestamp": "2026-10-03T09:00:00Z"},
        ]
        daily = billing_report.daily_charges(rows, RATES)
        self.assertAlmostEqual(daily[(2, "acme/catalog")], 2 * 2.5 * 1.25)
        self.assertAlmostEqual(daily[(3, "globex/atlas")], 2.5 * 1.25)

    def test_the_six_largest_tenants_keep_a_colour_and_the_rest_are_other(self):
        invoice = [{"Tenant": f"t{index}/repo", "Price": float(index)} for index in range(9)]
        invoice.append({"Tenant": "unassigned", "Price": 100.0})
        names = [name for name, _ in usage_chart.series_for(invoice)]
        self.assertEqual(names, ["t8/repo", "t7/repo", "t6/repo", "t5/repo", "t4/repo", "t3/repo", "Other tenants", "unassigned"])

    def test_the_page_escapes_tenant_names_and_draws_each_day(self):
        rows = [{**row("<acme>/catalog", "gpt-4o", 1_000_000, 0), "Timestamp": "2026-10-05T09:00:00Z"}]
        invoice, detail = billing_report.summarise(rows, RATES)
        page = usage_chart.html_page("2026-10", "USD", 0.25, invoice, detail, billing_report.daily_charges(rows, RATES))
        self.assertIn("&lt;acme&gt;/catalog", page)
        self.assertNotIn("<acme>", page.split("<script>")[0])
        self.assertIn('data-day="5"', page)

    def test_an_empty_month_says_so(self):
        self.assertIn("No usage was recorded", usage_chart.html_page("2026-10", "USD", 0.0, [], [], {}))

    def test_axis_labels_share_their_decimal_places(self):
        self.assertEqual([usage_chart.axis_label(0.5 * index, 0.5) for index in range(3)], ["0.00", "0.50", "1.00"])
        self.assertEqual([usage_chart.axis_label(2.5 * index, 2.5) for index in range(3)], ["0.0", "2.5", "5.0"])


class MainTests(unittest.TestCase):
    def test_writes_the_invoice_detail_and_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            rates_file = pathlib.Path(directory, "rates.json")
            rates_file.write_text(json.dumps(RATES))
            environment = {
                "USAGE_QUERY_URL": TABLE_URL,
                "BILLING_MONTH": "2026-10",
                "RATES_FILE": str(rates_file),
                "OUTPUT_DIR": str(pathlib.Path(directory, "out")),
                "GITHUB_STEP_SUMMARY": "",
            }
            with mock.patch.dict(os.environ, environment), mock.patch.object(
                billing_report, "fetch_rows", return_value=[row("acme/catalog", "gpt-4o", 1_000_000, 0)]
            ), mock.patch("builtins.print"):
                self.assertEqual(billing_report.main(), 0)
            invoice = pathlib.Path(directory, "out", "invoice.csv").read_text().splitlines()
            self.assertEqual(invoice[0], "Tenant,Calls,InputTokens,CachedInputTokens,OutputTokens,Cost,Price")
            self.assertEqual(invoice[1], "acme/catalog,1,1000000,0,0,2.5000,3.1250")
            summary = pathlib.Path(directory, "out", "summary.md").read_text()
            self.assertIn("| acme/catalog | 1 |", summary)
            self.assertIn("```mermaid", summary)
            self.assertIn("Daily charge by tenant", pathlib.Path(directory, "out", "usage.html").read_text())

    def test_rejects_a_malformed_month(self):
        with mock.patch.dict(os.environ, {"USAGE_QUERY_URL": TABLE_URL, "BILLING_MONTH": "2026-13"}), mock.patch(
            "sys.stderr"
        ):
            self.assertEqual(billing_report.main(), 1)


if __name__ == "__main__":
    unittest.main()
