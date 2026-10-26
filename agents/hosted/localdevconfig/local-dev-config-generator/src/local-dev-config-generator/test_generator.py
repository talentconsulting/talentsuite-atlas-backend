import json
import unittest
from unittest import mock

import generator
from generator import (
    GenerationError,
    generate_from_text,
    parse_input,
    parse_source_url,
    validate_catalog,
)


SOURCE_URL = "https://github.com/source/catalog/tree/main/src"
FILE_COMPOSE = "https://github.com/source/catalog/blob/main/src/docker-compose.yml"
FILE_SETTINGS = "https://github.com/source/catalog/blob/main/src/appsettings.json"

SOURCE_PATHS = {"src/docker-compose.yml", "src/appsettings.json"}

CATALOG = {
    "repository": "source/catalog",
    "ref": "main",
    "path": "src",
    "localServices": [
        {
            "name": "Redis",
            "kind": "cache",
            "technology": "redis",
            "configurationKeys": ["ConnectionStrings:Redis"],
            "evidence": [
                {
                    "sourceFile": "src/appsettings.json",
                    "reason": "ConnectionStrings:Redis configured; AddStackExchangeRedisCache registered",
                }
            ],
        }
    ],
    "configurationKeys": [
        {
            "key": "ConnectionStrings:Redis",
            "sourceFile": "src/appsettings.json",
            "reason": "Redis connection string entry",
        }
    ],
}


class InputTests(unittest.TestCase):
    def test_accepts_exact_source_url_and_source_files_contract(self):
        payload = parse_input(json.dumps({"sourceUrl": SOURCE_URL, "sourceFiles": [FILE_COMPOSE, FILE_SETTINGS]}))
        self.assertEqual(SOURCE_URL, payload["sourceUrl"])
        self.assertEqual([FILE_COMPOSE, FILE_SETTINGS], payload["sourceFiles"])

    def test_rejects_missing_source_files_field(self):
        with self.assertRaises(GenerationError):
            parse_input(json.dumps({"sourceUrl": SOURCE_URL}))

    def test_rejects_missing_source_url_field(self):
        with self.assertRaises(GenerationError):
            parse_input(json.dumps({"sourceFiles": [FILE_COMPOSE]}))

    def test_rejects_extra_input_fields(self):
        with self.assertRaises(GenerationError):
            parse_input(json.dumps({"sourceUrl": SOURCE_URL, "sourceFiles": [FILE_COMPOSE], "extra": True}))

    def test_rejects_source_file_from_a_different_repository(self):
        with self.assertRaises(GenerationError):
            parse_input(json.dumps({
                "sourceUrl": SOURCE_URL,
                "sourceFiles": ["https://github.com/other/catalog/blob/main/src/docker-compose.yml"],
            }))

    def test_rejects_source_file_from_a_different_ref(self):
        with self.assertRaises(GenerationError):
            parse_input(json.dumps({
                "sourceUrl": SOURCE_URL,
                "sourceFiles": ["https://github.com/source/catalog/blob/other/src/docker-compose.yml"],
            }))

    def test_rejects_empty_source_files(self):
        with self.assertRaises(GenerationError):
            parse_input(json.dumps({"sourceUrl": SOURCE_URL, "sourceFiles": []}))

    def test_rejects_more_than_100_source_files(self):
        too_many = [f"https://github.com/source/catalog/blob/main/src/file{i}.txt" for i in range(101)]
        with self.assertRaises(GenerationError):
            parse_input(json.dumps({"sourceUrl": SOURCE_URL, "sourceFiles": too_many}))

    def test_rejects_duplicate_source_files(self):
        with self.assertRaises(GenerationError):
            parse_input(json.dumps({"sourceUrl": SOURCE_URL, "sourceFiles": [FILE_COMPOSE, FILE_COMPOSE]}))

    def test_requires_a_github_tree_url(self):
        with self.assertRaises(GenerationError):
            parse_source_url("https://github.com/source/catalog/blob/main/src/docker-compose.yml")


class ValidateCatalogTests(unittest.TestCase):
    def test_validate_catalog_accepts_minimal_valid_document(self):
        catalog = json.loads(json.dumps(CATALOG))
        self.assertEqual(CATALOG, validate_catalog(catalog, source_paths=SOURCE_PATHS))

    def test_validate_catalog_accepts_empty_local_services_and_configuration_keys(self):
        empty = {"repository": "source/catalog", "ref": "main", "path": "src", "localServices": [], "configurationKeys": []}
        self.assertEqual(empty, validate_catalog(json.loads(json.dumps(empty))))

    def test_validate_catalog_matches_supplied_location(self):
        catalog = json.loads(json.dumps(CATALOG))
        self.assertEqual(
            CATALOG,
            validate_catalog(catalog, location=("source/catalog", "main", "src"), source_paths=SOURCE_PATHS),
        )

    def test_validate_catalog_rejects_mismatched_location(self):
        catalog = json.loads(json.dumps(CATALOG))
        with self.assertRaises(GenerationError):
            validate_catalog(catalog, location=("other/catalog", "main", "src"), source_paths=SOURCE_PATHS)

    def test_validate_catalog_rejects_wrong_key_set_at_every_level(self):
        top_level = json.loads(json.dumps(CATALOG))
        top_level["extra"] = True
        with self.assertRaisesRegex(GenerationError, "invalid_model_output|Model output requires exactly"):
            validate_catalog(top_level, source_paths=SOURCE_PATHS)

        missing_service_field = json.loads(json.dumps(CATALOG))
        del missing_service_field["localServices"][0]["technology"]
        with self.assertRaisesRegex(GenerationError, "invalid shape"):
            validate_catalog(missing_service_field, source_paths=SOURCE_PATHS)

        extra_config_key_field = json.loads(json.dumps(CATALOG))
        extra_config_key_field["configurationKeys"][0]["extra"] = "nope"
        with self.assertRaisesRegex(GenerationError, "invalid shape"):
            validate_catalog(extra_config_key_field, source_paths=SOURCE_PATHS)

    def test_validate_catalog_rejects_invalid_kind_enum_value(self):
        catalog = json.loads(json.dumps(CATALOG))
        catalog["localServices"][0]["kind"] = "queue"
        with self.assertRaisesRegex(GenerationError, "kind is invalid"):
            validate_catalog(catalog, source_paths=SOURCE_PATHS)

    def test_validate_catalog_rejects_duplicate_local_service_identity(self):
        catalog = json.loads(json.dumps(CATALOG))
        duplicate = json.loads(json.dumps(catalog["localServices"][0]))
        duplicate["name"] = "REDIS"  # same (kind, name.strip().lower()) identity as the first entry
        catalog["localServices"].append(duplicate)
        with self.assertRaisesRegex(GenerationError, "duplicates a local service identity"):
            validate_catalog(catalog, source_paths=SOURCE_PATHS)

    def test_validate_catalog_rejects_local_service_key_not_declared_at_top_level(self):
        catalog = json.loads(json.dumps(CATALOG))
        catalog["localServices"][0]["configurationKeys"].append("ConnectionStrings:Undeclared")
        with self.assertRaisesRegex(GenerationError, "undeclared top-level configuration key"):
            validate_catalog(catalog, source_paths=SOURCE_PATHS)

    def test_validate_catalog_rejects_source_file_not_in_supplied_bundle(self):
        catalog = json.loads(json.dumps(CATALOG))
        catalog["configurationKeys"][0]["sourceFile"] = "src/not-supplied.json"
        with self.assertRaisesRegex(GenerationError, "must reference a supplied source file"):
            validate_catalog(catalog, source_paths=SOURCE_PATHS)

    def test_validate_catalog_rejects_evidence_source_file_not_in_supplied_bundle(self):
        catalog = json.loads(json.dumps(CATALOG))
        catalog["localServices"][0]["evidence"][0]["sourceFile"] = "src/not-supplied.json"
        with self.assertRaisesRegex(GenerationError, "must reference a supplied source file"):
            validate_catalog(catalog, source_paths=SOURCE_PATHS)

    def test_validate_catalog_rejects_key_containing_a_scheme_or_whitespace(self):
        scheme_catalog = json.loads(json.dumps(CATALOG))
        scheme_catalog["configurationKeys"][0]["key"] = "redis://localhost:6379"
        with self.assertRaisesRegex(GenerationError, "not a URL or value"):
            validate_catalog(scheme_catalog, source_paths=SOURCE_PATHS)

        whitespace_catalog = json.loads(json.dumps(CATALOG))
        whitespace_catalog["configurationKeys"][0]["key"] = "Connection Strings:Redis"
        with self.assertRaisesRegex(GenerationError, "not a URL or value"):
            validate_catalog(whitespace_catalog, source_paths=SOURCE_PATHS)

    def test_validate_catalog_sorts_local_services_and_configuration_keys_deterministically(self):
        catalog = {
            "repository": "source/catalog",
            "ref": "main",
            "path": "src",
            "localServices": [
                {
                    "name": "SQL Server",
                    "kind": "database",
                    "technology": "sqlserver",
                    "configurationKeys": ["ConnectionStrings:Sql"],
                    "evidence": [],
                },
                {
                    "name": "Redis",
                    "kind": "cache",
                    "technology": "redis",
                    "configurationKeys": ["Cache:Port", "Cache:Host"],
                    "evidence": [],
                },
            ],
            "configurationKeys": [
                {"key": "ConnectionStrings:Sql", "sourceFile": "b.json", "reason": "sql"},
                {"key": "Cache:Host", "sourceFile": "a.json", "reason": "cache host"},
                {"key": "Cache:Port", "sourceFile": "a.json", "reason": "cache port"},
            ],
        }

        result = validate_catalog(json.loads(json.dumps(catalog)))

        self.assertEqual(["Redis", "SQL Server"], [item["name"] for item in result["localServices"]])
        self.assertEqual(["Cache:Host", "Cache:Port"], result["localServices"][0]["configurationKeys"])
        self.assertEqual(
            ["Cache:Host", "Cache:Port", "ConnectionStrings:Sql"],
            [item["key"] for item in result["configurationKeys"]],
        )


class GenerateFromTextTests(unittest.TestCase):
    def test_generate_from_text_uses_injected_completion_and_source_loader(self):
        prompts = []

        def completion(prompt):
            prompts.append(prompt)
            return json.dumps(CATALOG)

        def source_loader(location, source_files, tenant=None):
            self.assertEqual([FILE_COMPOSE, FILE_SETTINGS], source_files)
            return {
                "src/docker-compose.yml": "services:\n  redis:\n    image: redis",
                "src/appsettings.json": '{"ConnectionStrings": {"Redis": "..."}}',
            }

        result = generate_from_text(
            json.dumps({"sourceUrl": SOURCE_URL, "sourceFiles": [FILE_COMPOSE, FILE_SETTINGS]}),
            completion=completion,
            source_loader=source_loader,
        )

        self.assertEqual(CATALOG, result)
        self.assertIn("docker-compose.yml", prompts[0])
        self.assertIn("appsettings.json", prompts[0])

    def test_generate_from_text_rejects_non_json_model_output(self):
        with self.assertRaisesRegex(GenerationError, "valid JSON"):
            generate_from_text(
                json.dumps({"sourceUrl": SOURCE_URL, "sourceFiles": [FILE_COMPOSE]}),
                completion=lambda prompt: "not-json",
                source_loader=lambda location, source_files, tenant=None: {"src/docker-compose.yml": "services: {}"},
            )


class TenantTests(unittest.TestCase):
    def test_accepts_an_optional_tenant(self):
        payload = parse_input(json.dumps({"sourceUrl": SOURCE_URL, "sourceFiles": [FILE_COMPOSE], "tenant": "tenant/atlas"}))
        self.assertEqual("tenant/atlas", payload["tenant"])
        self.assertIsNone(parse_input(json.dumps({"sourceUrl": SOURCE_URL, "sourceFiles": [FILE_COMPOSE]}))["tenant"])

    def test_rejects_a_tenant_that_is_not_a_non_empty_string(self):
        for tenant in (1, "", "  "):
            with self.subTest(tenant=tenant), self.assertRaisesRegex(GenerationError, "tenant"):
                parse_input(json.dumps({"sourceUrl": SOURCE_URL, "sourceFiles": [FILE_COMPOSE], "tenant": tenant}))

    def test_generate_passes_the_tenant_to_the_source_loader(self):
        seen = []

        def source_loader(location, source_files, tenant=None):
            seen.append(tenant)
            return {"src/appsettings.json": "{}"}

        generate_from_text(
            json.dumps({"sourceUrl": SOURCE_URL, "sourceFiles": [FILE_SETTINGS], "tenant": "tenant/atlas"}),
            completion=lambda prompt: json.dumps({
                "repository": "source/catalog", "ref": "main", "path": "src", "localServices": [], "configurationKeys": [],
            }),
            source_loader=source_loader,
        )
        self.assertEqual(["tenant/atlas"], seen)


class SourceDownloadTests(unittest.TestCase):
    def download(self, token):
        requests = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return b"services: {}"

        def urlopen(request, timeout):
            requests.append(request)
            return Response()

        with mock.patch.object(generator.github_app, "read_token", return_value=token) as read_token, \
                mock.patch.object(generator.urllib.request, "urlopen", side_effect=urlopen):
            generator._download_selected_sources(parse_source_url(SOURCE_URL), [FILE_COMPOSE], "tenant/atlas")
        read_token.assert_called_once_with("tenant/atlas")
        return requests[0]

    def test_the_tenants_token_reads_through_the_contents_api(self):
        request = self.download("installation-token")
        self.assertEqual("https://api.github.com/repos/source/catalog/contents/src/docker-compose.yml?ref=main", request.full_url)
        self.assertEqual("Bearer installation-token", request.get_header("Authorization"))
        self.assertEqual("application/vnd.github.raw", request.get_header("Accept"))

    def test_without_a_token_the_raw_file_is_read(self):
        request = self.download(None)
        self.assertEqual("https://raw.githubusercontent.com/source/catalog/main/src/docker-compose.yml", request.full_url)
        self.assertIsNone(request.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
