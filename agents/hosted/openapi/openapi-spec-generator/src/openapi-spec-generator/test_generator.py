import json
import unittest
from unittest import mock

import generator
from generator import (
    GenerationError,
    generate_from_text,
    load_sources,
    parse_github_blob_url,
    parse_input,
)


API_URL = "https://github.com/example/catalog/blob/main/src/Api/BidsController.cs"
DTO_URL = "https://github.com/example/catalog/blob/main/src/Contracts/BidResponse.cs"


class InputTests(unittest.TestCase):
    def test_accepts_one_discovery_result(self):
        payload = parse_input(
            json.dumps({"apiFile": API_URL, "supportingFiles": [DTO_URL]})
        )
        self.assertEqual(payload["apiFile"], API_URL)
        self.assertEqual(payload["supportingFiles"], [DTO_URL])

    def test_rejects_extra_properties(self):
        with self.assertRaises(GenerationError):
            parse_input(
                json.dumps(
                    {"apiFile": API_URL, "supportingFiles": [], "domain": "catalog"}
                )
            )


class TenantTests(unittest.TestCase):
    def test_accepts_an_optional_tenant(self):
        payload = parse_input(
            json.dumps({"apiFile": API_URL, "supportingFiles": [], "tenant": "tenant/atlas"})
        )
        self.assertEqual(payload["tenant"], "tenant/atlas")
        self.assertIsNone(parse_input(json.dumps({"apiFile": API_URL, "supportingFiles": []}))["tenant"])

    def test_rejects_a_tenant_that_is_not_a_non_empty_string(self):
        for tenant in (1, "", "  "):
            with self.assertRaisesRegex(GenerationError, "tenant"):
                parse_input(json.dumps({"apiFile": API_URL, "supportingFiles": [], "tenant": tenant}))

    def test_passes_the_tenant_to_every_fetch(self):
        seen = []
        load_sources(
            parse_input(json.dumps({"apiFile": API_URL, "supportingFiles": [DTO_URL], "tenant": "tenant/atlas"})),
            fetcher=lambda source, tenant: seen.append((source.path, tenant)) or "source",
        )
        self.assertEqual(
            [("src/Api/BidsController.cs", "tenant/atlas"), ("src/Contracts/BidResponse.cs", "tenant/atlas")],
            seen,
        )


class SourceDownloadTests(unittest.TestCase):
    def download(self, token):
        requests = []

        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                return b"class BidsController {}"

        def urlopen(request, timeout):
            requests.append(request)
            return Response()

        with mock.patch.object(generator.github_app, "read_token", return_value=token) as read_token, \
                mock.patch.object(generator.urllib.request, "urlopen", side_effect=urlopen):
            content = generator._download_source(parse_github_blob_url(API_URL), "tenant/atlas")
        read_token.assert_called_once_with("tenant/atlas")
        self.assertEqual("class BidsController {}", content)
        return requests[0]

    def test_the_tenants_token_reads_through_the_contents_api(self):
        request = self.download("installation-token")
        self.assertEqual(
            "https://api.github.com/repos/example/catalog/contents/src/Api/BidsController.cs?ref=main",
            request.full_url,
        )
        self.assertEqual("Bearer installation-token", request.get_header("Authorization"))
        self.assertEqual("application/vnd.github.raw", request.get_header("Accept"))

    def test_without_a_token_the_raw_file_is_read(self):
        request = self.download(None)
        self.assertEqual(
            "https://raw.githubusercontent.com/example/catalog/main/src/Api/BidsController.cs",
            request.full_url,
        )
        self.assertIsNone(request.get_header("Authorization"))


class SourceTests(unittest.TestCase):
    def test_parses_github_blob_url(self):
        source = parse_github_blob_url(API_URL)
        self.assertEqual(source.owner, "example")
        self.assertEqual(source.repository, "catalog")
        self.assertEqual(source.ref, "main")
        self.assertEqual(source.path, "src/Api/BidsController.cs")

    def test_rejects_supporting_file_from_another_ref(self):
        with self.assertRaises(GenerationError) as raised:
            load_sources(
                {
                    "apiFile": API_URL,
                    "supportingFiles": [DTO_URL.replace("/main/", "/feature/")],
                },
                fetcher=lambda source, tenant: "source",
            )
        self.assertEqual(raised.exception.code, "source_mismatch")


class GenerationTests(unittest.TestCase):
    def test_returns_the_openapi_document_without_a_wrapper(self):
        expected = {
            "openapi": "3.1.0",
            "info": {"title": "Bids API", "version": "1.0.0"},
            "paths": {"/bids": {"get": {"responses": {"200": {"description": "OK"}}}}},
            "components": {"schemas": {}},
        }

        captured_prompt = []
        actual = generate_from_text(
            json.dumps({"apiFile": API_URL, "supportingFiles": [DTO_URL]}),
            fetcher=lambda source, tenant: f"source for {source.path}",
            completion=lambda prompt: captured_prompt.append(prompt) or json.dumps(expected),
        )

        self.assertEqual(actual, expected)
        self.assertNotIn("specification", actual)
        self.assertIn("JSON", captured_prompt[0])

    def test_orders_paths_near_the_top_and_components_last(self):
        model_document = {
            "components": {"schemas": {}},
            "tags": [{"name": "Bids"}],
            "info": {"title": "Bids API", "version": "1.0.0"},
            "openapi": "3.1.0",
            "servers": [{"url": "https://api.example.com"}],
            "paths": {"/bids": {"get": {"responses": {"200": {"description": "OK"}}}}},
        }

        actual = generate_from_text(
            json.dumps({"apiFile": API_URL, "supportingFiles": []}),
            fetcher=lambda source, tenant: "source",
            completion=lambda prompt: json.dumps(model_document),
        )

        self.assertEqual(
            ["openapi", "info", "paths", "servers", "tags", "components"],
            list(actual),
        )

    def test_rejects_a_non_openapi_model_response(self):
        with self.assertRaises(GenerationError) as raised:
            generate_from_text(
                json.dumps({"apiFile": API_URL, "supportingFiles": []}),
                fetcher=lambda source, tenant: "source",
                completion=lambda prompt: json.dumps({"message": "done"}),
            )
        self.assertEqual(raised.exception.code, "invalid_model_output")


if __name__ == "__main__":
    unittest.main()
