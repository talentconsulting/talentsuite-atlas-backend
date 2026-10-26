import json
import unittest
from unittest import mock

import generator
from generator import GenerationError, SourceLocation, generate_from_text, parse_input, source_prompt, validate_catalog


SOURCE = "https://github.com/source/app/tree/main/src"
FILES = ["https://github.com/source/app/blob/main/src/Clients/AccountsClient.cs"]
CONTAINERS = [{
    "id": "app",
    "name": "App",
    "type": "api",
    "evidence": [{"sourceFile": "src/Clients/AccountsClient.cs", "reason": "API entry point."}],
}]
CATALOG = {
    "repository": "source/app",
    "ref": "main",
    "path": "src",
    "systemName": "App",
    "containers": json.loads(json.dumps(CONTAINERS)),
    "dependencies": [{
        "sourceId": "app",
        "name": "Accounts API",
        "kind": "http-api",
        "classification": "internal",
        "direction": "outbound",
        "client": "AccountsClient",
        "technology": "HttpClient",
        "configurationKeys": ["AccountsApi:BaseUrl"],
        "authentication": {"type": "oauth2", "configurationKeys": ["AccountsApi:Identifier"]},
        "operations": [{
            "method": "GET", "methodName": "GetAccount", "path": "/accounts/{id}",
            "sourceFile": "src/Clients/AccountsClient.cs",
        }],
        "resources": [],
        "evidence": [{"sourceFile": "src/Clients/AccountsClient.cs", "reason": "Typed HTTP client registration."}],
        "confidence": "high",
    }],
}


class GeneratorTests(unittest.TestCase):
    def test_input_requires_selected_same_repository_files(self):
        self.assertEqual(FILES, parse_input(json.dumps({"sourceUrl": SOURCE, "sourceFiles": FILES}))["sourceFiles"])
        with self.assertRaises(GenerationError):
            parse_input(json.dumps({"sourceUrl": SOURCE}))
        with self.assertRaises(GenerationError):
            parse_input(json.dumps({"sourceUrl": SOURCE, "sourceFiles": ["https://github.com/other/app/blob/main/X.cs"]}))

    def test_source_prompt_explicitly_requests_json(self):
        prompt = source_prompt(SourceLocation("source", "app", "main", "src"), {"src/Client.cs": "HttpClient client;"})
        self.assertIn("JSON", prompt)

    def test_validates_catalog_contract_and_source_evidence(self):
        result = validate_catalog(
            json.loads(json.dumps(CATALOG)),
            SourceLocation("source", "app", "main", "src"),
            {"src/Clients/AccountsClient.cs"},
        )
        self.assertEqual("Accounts API", result["dependencies"][0]["name"])
        bad = json.loads(json.dumps(CATALOG))
        bad["dependencies"][0]["evidence"][0]["sourceFile"] = "src/NotSupplied.cs"
        with self.assertRaisesRegex(GenerationError, "supplied source"):
            validate_catalog(bad, source_paths={"src/Clients/AccountsClient.cs"})

    def test_operations_require_a_methodname_field(self):
        bad = json.loads(json.dumps(CATALOG))
        del bad["dependencies"][0]["operations"][0]["methodName"]
        with self.assertRaisesRegex(GenerationError, "invalid shape"):
            validate_catalog(bad, source_paths={"src/Clients/AccountsClient.cs"})

    def test_operations_methodname_may_be_null(self):
        catalog = json.loads(json.dumps(CATALOG))
        catalog["dependencies"][0]["operations"][0]["methodName"] = None
        result = validate_catalog(catalog, source_paths={"src/Clients/AccountsClient.cs"})
        self.assertIsNone(result["dependencies"][0]["operations"][0]["methodName"])

    def test_a_redis_cache_dependency_is_kept_in_the_output(self):
        catalog = json.loads(json.dumps(CATALOG))
        catalog["dependencies"].append({
            "sourceId": "app",
            "name": "Redis", "kind": "cache", "classification": "internal", "direction": "outbound",
            "client": None, "technology": "StackExchange.Redis",
            "configurationKeys": ["Redis:ConnectionString"],
            "authentication": {"type": None, "configurationKeys": []},
            "operations": [], "resources": [],
            "evidence": [{
                "sourceFile": "src/Clients/AccountsClient.cs",
                "reason": "AddStackExchangeRedisCache registration found.",
            }],
            "confidence": "high",
        })
        result = validate_catalog(catalog, source_paths={"src/Clients/AccountsClient.cs"})
        names = {dependency["name"] for dependency in result["dependencies"]}
        self.assertIn("Redis", names)

    def test_dependencies_get_a_c4_style_source_target_and_action(self):
        result = validate_catalog(
            json.loads(json.dumps(CATALOG)), source_paths={"src/Clients/AccountsClient.cs"}
        )
        dependency = result["dependencies"][0]
        self.assertEqual("app", dependency["sourceId"])
        self.assertEqual("http-api-accounts-api", dependency["targetId"])
        self.assertEqual("Calls the API of", dependency["description"])

    def test_target_ids_are_deduplicated_when_names_slugify_the_same(self):
        catalog = json.loads(json.dumps(CATALOG))
        clashing = json.loads(json.dumps(CATALOG["dependencies"][0]))
        clashing["name"] = "Accounts-API"
        catalog["dependencies"].append(clashing)

        result = validate_catalog(catalog, source_paths={"src/Clients/AccountsClient.cs"})

        target_ids = [dependency["targetId"] for dependency in result["dependencies"]]
        self.assertEqual(len(target_ids), len(set(target_ids)))

    def test_rejects_literal_endpoint_hostnames(self):
        bad = json.loads(json.dumps(CATALOG))
        bad["dependencies"][0]["operations"][0]["path"] = "https://accounts.example/api"
        with self.assertRaisesRegex(GenerationError, "hostname"):
            validate_catalog(bad, source_paths={"src/Clients/AccountsClient.cs"})

    def test_a_database_dependency_is_kept_with_a_c4_style_action(self):
        catalog = json.loads(json.dumps(CATALOG))
        database = json.loads(json.dumps(CATALOG["dependencies"][0]))
        database.update({
            "name": "ProviderCommitmentsDbContext",
            "kind": "database",
            "client": "ProviderCommitmentsDbContext",
            "technology": "Entity Framework Core",
        })
        catalog["dependencies"].append(database)

        result = validate_catalog(catalog, source_paths={"src/Clients/AccountsClient.cs"})

        names = {dependency["name"] for dependency in result["dependencies"]}
        self.assertIn("ProviderCommitmentsDbContext", names)
        kept = next(d for d in result["dependencies"] if d["name"] == "ProviderCommitmentsDbContext")
        self.assertEqual("app", kept["sourceId"])
        self.assertEqual("Reads from and writes to", kept["description"])

    def test_message_broker_object_storage_and_cloud_service_dependencies_are_kept(self):
        catalog = json.loads(json.dumps(CATALOG))
        extras = [
            ("OrderQueue", "message-broker", "Publishes messages to and consumes messages from"),
            ("Document Storage", "object-storage", "Stores objects in"),
            ("Key Vault", "cloud-service", "Uses"),
        ]
        for name, kind, _ in extras:
            entry = json.loads(json.dumps(CATALOG["dependencies"][0]))
            entry.update({"name": name, "kind": kind, "client": None, "operations": []})
            catalog["dependencies"].append(entry)

        result = validate_catalog(catalog, source_paths={"src/Clients/AccountsClient.cs"})

        by_name = {dependency["name"]: dependency for dependency in result["dependencies"]}
        for name, _, description in extras:
            self.assertIn(name, by_name)
            self.assertEqual(description, by_name[name]["description"])

    def test_filters_kind_other_dependencies_from_model_output(self):
        catalog = json.loads(json.dumps(CATALOG))
        other = json.loads(json.dumps(CATALOG["dependencies"][0]))
        other.update({"name": "Miscellaneous", "kind": "other", "client": None, "operations": []})
        catalog["dependencies"].append(other)

        result = validate_catalog(catalog, source_paths={"src/Clients/AccountsClient.cs"})

        self.assertEqual(["Accounts API"], [dependency["name"] for dependency in result["dependencies"]])

    def test_two_containers_can_each_depend_on_the_same_target(self):
        catalog = json.loads(json.dumps(CATALOG))
        catalog["containers"].append({
            "id": "app-jobs", "name": "App Jobs", "type": "job",
            "evidence": [{"sourceFile": "src/Clients/AccountsClient.cs", "reason": "Background job entry point."}],
        })
        second = json.loads(json.dumps(CATALOG["dependencies"][0]))
        second["sourceId"] = "app-jobs"
        catalog["dependencies"].append(second)

        result = validate_catalog(catalog, source_paths={"src/Clients/AccountsClient.cs"})

        self.assertEqual(2, len(result["dependencies"]))
        source_ids = {dependency["sourceId"] for dependency in result["dependencies"]}
        target_ids = {dependency["targetId"] for dependency in result["dependencies"]}
        self.assertEqual({"app", "app-jobs"}, source_ids)
        self.assertEqual(1, len(target_ids))

    def test_the_same_container_cannot_claim_the_same_dependency_twice(self):
        catalog = json.loads(json.dumps(CATALOG))
        catalog["dependencies"].append(json.loads(json.dumps(CATALOG["dependencies"][0])))
        with self.assertRaisesRegex(GenerationError, "duplicates a dependency"):
            validate_catalog(catalog, source_paths={"src/Clients/AccountsClient.cs"})

    def test_message_handler_is_not_a_valid_container_type(self):
        # A real run confused the container-type vocabulary with the dependency-kind vocabulary and
        # emitted a container with type "message-handler" -- that word must stay exclusive to the
        # dependency-kind vocabulary (message-broker) now.
        catalog = json.loads(json.dumps(CATALOG))
        catalog["containers"][0]["type"] = "message-handler"
        with self.assertRaisesRegex(GenerationError, "type is invalid"):
            validate_catalog(catalog, source_paths={"src/Clients/AccountsClient.cs"})

    def test_a_dependency_with_an_unrecognized_kind_is_dropped_not_fatal(self):
        # A real run recurred even after tightening the prompt: for a batch made entirely of
        # message-handler files, the model still occasionally invents a kind matching the
        # container-type vocabulary (such as "message-handler"). Dropping the one bad item is far
        # better than failing the whole batch's otherwise-valid dependencies over it.
        catalog = json.loads(json.dumps(CATALOG))
        catalog["dependencies"][0]["kind"] = "message-handler"

        result = validate_catalog(catalog, source_paths={"src/Clients/AccountsClient.cs"})

        self.assertEqual([], result["dependencies"])

    def test_dependency_sourceid_must_reference_a_known_container(self):
        catalog = json.loads(json.dumps(CATALOG))
        catalog["dependencies"][0]["sourceId"] = "unknown-container"
        with self.assertRaisesRegex(GenerationError, "unknown container"):
            validate_catalog(catalog, source_paths={"src/Clients/AccountsClient.cs"})

    def test_containers_must_be_a_non_empty_array(self):
        catalog = json.loads(json.dumps(CATALOG))
        catalog["containers"] = []
        with self.assertRaisesRegex(GenerationError, "non-empty array"):
            validate_catalog(catalog, source_paths={"src/Clients/AccountsClient.cs"})

    def test_container_type_must_be_valid(self):
        catalog = json.loads(json.dumps(CATALOG))
        catalog["containers"][0]["type"] = "database"
        with self.assertRaisesRegex(GenerationError, "type is invalid"):
            validate_catalog(catalog, source_paths={"src/Clients/AccountsClient.cs"})

    def test_container_ids_must_be_unique(self):
        catalog = json.loads(json.dumps(CATALOG))
        catalog["containers"].append(json.loads(json.dumps(catalog["containers"][0])))
        with self.assertRaisesRegex(GenerationError, "duplicated"):
            validate_catalog(catalog, source_paths={"src/Clients/AccountsClient.cs"})

    def test_generate_downloads_only_selected_files(self):
        loaded = []

        def load(location, files, tenant=None):
            loaded.extend(files)
            return {"src/Clients/AccountsClient.cs": "class AccountsClient { HttpClient client; }"}

        result = generate_from_text(
            json.dumps({"sourceUrl": SOURCE, "sourceFiles": FILES}),
            completion=lambda prompt: json.dumps(CATALOG),
            source_loader=load,
        )
        self.assertEqual(FILES, loaded)
        self.assertEqual("Accounts API", result["dependencies"][0]["name"])


class TenantTests(unittest.TestCase):
    def test_passes_the_tenant_to_the_source_loader(self):
        seen = []

        def load(location, files, tenant=None):
            seen.append(tenant)
            return {"src/Clients/AccountsClient.cs": "class AccountsClient {}"}

        generate_from_text(json.dumps({"sourceUrl": SOURCE, "sourceFiles": FILES, "tenant": "tenant/atlas"}),
                           completion=lambda prompt: json.dumps(CATALOG), source_loader=load)
        self.assertEqual(["tenant/atlas"], seen)

    def test_tenant_is_optional(self):
        self.assertIsNone(parse_input(json.dumps({"sourceUrl": SOURCE, "sourceFiles": FILES}))["tenant"])

    def test_rejects_a_tenant_that_is_not_a_non_empty_string(self):
        for tenant in (1, "", "  "):
            with self.assertRaisesRegex(GenerationError, "tenant"):
                parse_input(json.dumps({"sourceUrl": SOURCE, "sourceFiles": FILES, "tenant": tenant}))

    def test_still_rejects_other_keys(self):
        with self.assertRaises(GenerationError):
            parse_input(json.dumps({"sourceUrl": SOURCE, "sourceFiles": FILES, "other": 1}))


class SourceDownloadTests(unittest.TestCase):
    def download(self, token):
        requests = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return b"class AccountsClient {}"

        def urlopen(request, timeout):
            requests.append(request)
            return Response()

        with mock.patch.object(generator.github_app, "read_token", return_value=token) as read_token, \
                mock.patch.object(generator.urllib.request, "urlopen", side_effect=urlopen):
            generator._download_selected_sources(generator.parse_source_url(SOURCE), FILES, "tenant/atlas")
        read_token.assert_called_once_with("tenant/atlas")
        return requests[0]

    def test_the_tenants_token_reads_through_the_contents_api(self):
        request = self.download("installation-token")
        self.assertEqual("https://api.github.com/repos/source/app/contents/src/Clients/AccountsClient.cs?ref=main",
                         request.full_url)
        self.assertEqual("Bearer installation-token", request.get_header("Authorization"))
        self.assertEqual("application/vnd.github.raw", request.get_header("Accept"))

    def test_without_a_token_the_raw_file_is_read(self):
        request = self.download(None)
        self.assertEqual("https://raw.githubusercontent.com/source/app/main/src/Clients/AccountsClient.cs", request.full_url)
        self.assertIsNone(request.get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
