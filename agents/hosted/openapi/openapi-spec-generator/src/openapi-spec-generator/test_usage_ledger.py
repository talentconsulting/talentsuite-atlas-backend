import datetime
import json
import os
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

import usage_ledger

NOW = datetime.datetime(2026, 10, 26, 14, 30, 5, 123456, tzinfo=datetime.timezone.utc)
LEDGER_URL = "https://ledger.table.core.windows.net/usage?sv=2023&sig=secret"


def response(input_tokens=1200, cached_tokens=200, output_tokens=300):
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
        )
    )


class EntityTests(unittest.TestCase):
    def test_records_tokens_for_the_tenant_and_month(self):
        entity = usage_ledger.usage_entity(response(), "openapi-spec-generator", "gpt-4o", "acme/catalog", NOW)
        self.assertEqual(entity["PartitionKey"], "2026-10")
        self.assertTrue(entity["RowKey"].startswith("acme:catalog|20261026T143005123456Z|"))
        self.assertEqual(entity["Tenant"], "acme/catalog")
        self.assertEqual(entity["Agent"], "openapi-spec-generator")
        self.assertEqual(entity["Model"], "gpt-4o")
        self.assertEqual(
            (entity["InputTokens"], entity["CachedInputTokens"], entity["OutputTokens"]),
            (1200, 200, 300),
        )

    def test_a_request_without_a_tenant_is_unassigned(self):
        entity = usage_ledger.usage_entity(response(), "agent", "gpt-4o", None, NOW)
        self.assertEqual(entity["Tenant"], usage_ledger.UNASSIGNED_TENANT)

    def test_missing_usage_counts_as_zero(self):
        entity = usage_ledger.usage_entity(SimpleNamespace(), "agent", "gpt-4o", "acme/catalog", NOW)
        self.assertEqual((entity["InputTokens"], entity["CachedInputTokens"], entity["OutputTokens"]), (0, 0, 0))

    def test_row_keys_are_unique(self):
        first = usage_ledger.usage_entity(response(), "agent", "gpt-4o", "acme/catalog", NOW)
        second = usage_ledger.usage_entity(response(), "agent", "gpt-4o", "acme/catalog", NOW)
        self.assertNotEqual(first["RowKey"], second["RowKey"])


class BeginTests(unittest.TestCase):
    def test_takes_the_tenant_from_the_request(self):
        usage_ledger.begin(json.dumps({"tenant": " acme/catalog ", "apiFile": "x"}))
        self.assertEqual(usage_ledger._tenant.get(), "acme/catalog")

    def test_clears_the_tenant_when_the_request_has_none(self):
        usage_ledger.begin(json.dumps({"tenant": "acme/catalog"}))
        usage_ledger.begin(json.dumps({"apiFile": "x"}))
        self.assertIsNone(usage_ledger._tenant.get())

    def test_ignores_input_that_is_not_a_json_object(self):
        usage_ledger.begin("not json")
        self.assertIsNone(usage_ledger._tenant.get())
        usage_ledger.begin("[1, 2]")
        self.assertIsNone(usage_ledger._tenant.get())


class RecordTests(unittest.TestCase):
    def test_does_nothing_without_a_ledger_url(self):
        with mock.patch.dict(os.environ, {"USAGE_LEDGER_URL": ""}), mock.patch("urllib.request.urlopen") as urlopen:
            usage_ledger.record(response(), "agent", "gpt-4o")
        urlopen.assert_not_called()

    def test_posts_the_entity_to_the_ledger(self):
        usage_ledger.begin(json.dumps({"tenant": "acme/catalog"}))
        with mock.patch.dict(os.environ, {"USAGE_LEDGER_URL": LEDGER_URL}), mock.patch("urllib.request.urlopen") as urlopen:
            usage_ledger.record(response(), "openapi-spec-generator", "gpt-4o")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, LEDGER_URL)
        self.assertEqual(request.get_method(), "POST")
        body = json.loads(request.data)
        self.assertEqual((body["Tenant"], body["Agent"], body["OutputTokens"]), ("acme/catalog", "openapi-spec-generator", 300))

    def test_a_failed_write_is_logged_without_the_url_and_not_raised(self):
        error = urllib.error.HTTPError(LEDGER_URL, 403, "Forbidden", {}, None)
        with mock.patch.dict(os.environ, {"USAGE_LEDGER_URL": LEDGER_URL}), mock.patch(
            "urllib.request.urlopen", side_effect=error
        ), self.assertLogs("usage_ledger", "WARNING") as logs:
            usage_ledger.record(response(), "agent", "gpt-4o")
        self.assertIn("HTTP 403", logs.output[0])
        self.assertNotIn("secret", logs.output[0])

    def test_a_network_failure_is_not_raised(self):
        with mock.patch.dict(os.environ, {"USAGE_LEDGER_URL": LEDGER_URL}), mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("down")
        ), self.assertLogs("usage_ledger", "WARNING"):
            usage_ledger.record(response(), "agent", "gpt-4o")


if __name__ == "__main__":
    unittest.main()
