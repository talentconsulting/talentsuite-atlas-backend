import json
import threading
import unittest
from unittest.mock import patch

from orchestrator import (
    ManifestError,
    _fetch_json,
    build_generator_input,
    parse_blob_url,
    parse_request,
    run_manifest,
)


MANIFEST_URL = "https://github.com/org/catalogue/blob/main/manifest.json"
BLOB = parse_blob_url(MANIFEST_URL)
APP_ENTRY = {
    "github-repo": "https://github.com/source/app",
    "dbschema": {"path-to-scan": "tree/main/src", "last-commit-hash-scanned": "a" * 40},
    "eventcatalog": {"path-to-scan": "tree/main/src", "last-commit-hash-scanned": "a" * 40},
    "service-dependencies": {"path-to-scan": "tree/main/src", "last-commit-hash-scanned": "a" * 40},
    "specs": {"path-to-scan": "tree/main/src", "last-commit-hash-scanned": "a" * 40},
}
DATABASE = {"database": {"name": "app", "engine": None}, "tables": [{"name": "Orders"}], "types": []}
EVENTS = {"repository": "source/app", "ref": "main", "path": "src", "commands": [], "events": []}
DEPENDENCIES = {"repository": "source/app", "ref": "main", "path": "src", "dependencies": []}
CONTROLLERS = [{"name": "OrdersController.openapi.json"}, {"name": "README.md"}]
SUMMARY = {
    "repository": "source/app",
    "name": "App",
    "description": "Manages orders.",
    "domain": "Orders",
    "capabilities": ["Order management"],
    "confidence": "high",
}


def fetch_fixture(overrides=None):
    urls = {
        "https://raw.githubusercontent.com/org/catalogue/main/manifest.json": [APP_ENTRY],
        "https://raw.githubusercontent.com/org/catalogue/main/app/db-schema/database.schema.json": DATABASE,
        "https://raw.githubusercontent.com/org/catalogue/main/app/event-catalog/events-and-commands.json": EVENTS,
        "https://raw.githubusercontent.com/org/catalogue/main/app/service-dependencies/service-dependencies.json": DEPENDENCIES,
        "https://api.github.com/repos/org/catalogue/contents/app/open-api?ref=main": CONTROLLERS,
    }
    if overrides:
        urls.update(overrides)
    return lambda url: urls.get(url)


class RequestTests(unittest.TestCase):
    def test_requires_source_url(self):
        with self.assertRaises(ManifestError):
            parse_request(json.dumps({}))

    def test_defer_publication_defaults_to_false(self):
        result = parse_request(json.dumps({"sourceUrl": MANIFEST_URL}))
        self.assertFalse(result["deferPublication"])

    def test_rejects_unsupported_fields(self):
        with self.assertRaises(ManifestError):
            parse_request(json.dumps({"sourceUrl": MANIFEST_URL, "extra": True}))


class BuildGeneratorInputTests(unittest.TestCase):
    def test_fetches_every_declared_catalog_and_lists_controllers(self):
        result = build_generator_input(APP_ENTRY, BLOB, fetch_fixture())
        self.assertEqual("source/app", result["repository"])
        self.assertEqual(DATABASE, result["database"])
        self.assertEqual(EVENTS, result["events"])
        self.assertEqual(DEPENDENCIES, result["dependencies"])
        self.assertEqual(["OrdersController"], result["apiControllers"])

    def test_skips_catalogs_the_manifest_does_not_declare(self):
        sparse_entry = {"github-repo": "https://github.com/source/app"}
        result = build_generator_input(sparse_entry, BLOB, fetch_fixture())
        self.assertIsNone(result["database"])
        self.assertIsNone(result["events"])
        self.assertIsNone(result["dependencies"])
        self.assertEqual([], result["apiControllers"])

    def test_treats_a_missing_declared_file_as_null(self):
        fetch = fetch_fixture({"https://raw.githubusercontent.com/org/catalogue/main/app/db-schema/database.schema.json": None})
        result = build_generator_input(APP_ENTRY, BLOB, fetch)
        self.assertIsNone(result["database"])

    def test_rejects_an_entry_without_a_repository(self):
        with self.assertRaises(ManifestError):
            build_generator_input({}, BLOB, fetch_fixture())


class RunManifestTests(unittest.TestCase):
    def test_generates_and_publishes_once(self):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append(name)
            if name == "generator":
                self.assertEqual("source/app", payload["repository"])
                self.assertEqual(BLOB.tenant, payload["tenant"])
                return SUMMARY
            self.assertEqual("publisher", name)
            self.assertEqual("system-summaries.json", payload["targetPath"])
            self.assertEqual([SUMMARY], payload["fileContent"]["systems"])
            return {"success": True, "status": "created", "pullRequestUrl": "https://github.com/org/catalogue/pull/1"}

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL, "deferPublication": False},
            "generator", "publisher", "gpt-4o", fetch=fetch_fixture(), invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual(1, result["generatedSystemCount"])
        self.assertEqual(["generator", "publisher"], calls)

    def test_deferred_run_skips_publication(self):
        def invoke(project, name, model, payload, max_attempts=2):
            self.assertEqual("generator", name)
            return SUMMARY

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL, "deferPublication": True},
            "generator", "publisher", "gpt-4o", fetch=fetch_fixture(), invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertIsNone(result["pullRequest"])
        self.assertEqual([SUMMARY], result["systems"])

    def test_a_failing_repository_does_not_block_others(self):
        second_entry = {**APP_ENTRY, "github-repo": "https://github.com/source/billing"}
        fetch = fetch_fixture(
            {"https://raw.githubusercontent.com/org/catalogue/main/manifest.json": [APP_ENTRY, second_entry]}
        )

        def invoke(project, name, model, payload, max_attempts=2):
            if name != "generator":
                raise AssertionError("Publisher must not be called when a repository failed.")
            if payload["repository"] == "source/billing":
                return {"error": {"code": "invalid_model_output", "message": "bad summary"}}
            return SUMMARY

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL, "deferPublication": True},
            "generator", "publisher", "gpt-4o", fetch=fetch, invoker=invoke,
        )

        self.assertTrue(result["success"] is False)
        self.assertEqual(1, result["generatedSystemCount"])
        self.assertEqual(1, len(result["failures"]))
        self.assertEqual("source/billing", result["failures"][0]["repository"])

    def test_every_repository_failing_returns_no_systems(self):
        def invoke(project, name, model, payload, max_attempts=2):
            return {"error": {"code": "invalid_model_output", "message": "bad summary"}}

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL, "deferPublication": True},
            "generator", "publisher", "gpt-4o", fetch=fetch_fixture(), invoker=invoke,
        )

        self.assertFalse(result["success"])
        self.assertEqual(0, result["generatedSystemCount"])
        self.assertEqual([], result["systems"])

    def test_repositories_are_summarized_concurrently(self):
        entries = [{"github-repo": f"https://github.com/source/app{index}"} for index in range(3)]
        fetch = fetch_fixture({"https://raw.githubusercontent.com/org/catalogue/main/manifest.json": entries})
        # Fails with BrokenBarrierError unless all three generator calls are in flight at once.
        barrier = threading.Barrier(3, timeout=5)

        def invoke(project, name, model, payload, max_attempts=2):
            barrier.wait()
            return dict(SUMMARY, repository=payload["repository"])

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL, "deferPublication": True},
            "generator", "publisher", "gpt-4o", workflow_concurrency=3, fetch=fetch, invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual(3, result["generatedSystemCount"])

    def test_results_are_processed_in_manifest_order_when_completion_order_differs(self):
        entries = [
            {"github-repo": "https://github.com/source/first"},
            "not an object",
            {"github-repo": "https://github.com/source/broken"},
            {"github-repo": "https://github.com/source/last"},
        ]
        fetch = fetch_fixture({"https://raw.githubusercontent.com/org/catalogue/main/manifest.json": entries})
        last_done = threading.Event()
        published = []

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "publisher":
                published.append(payload)
                return {"success": True, "status": "created"}
            # The first repository only finishes after the last one, so completion order is reversed.
            if payload["repository"] == "source/first":
                self.assertTrue(last_done.wait(timeout=5))
            if payload["repository"] == "source/broken":
                raise RuntimeError("generator unavailable")
            if payload["repository"] == "source/last":
                last_done.set()
            return dict(SUMMARY, repository=payload["repository"])

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL, "deferPublication": True},
            "generator", "publisher", "gpt-4o", workflow_concurrency=4, fetch=fetch, invoker=invoke,
        )

        self.assertEqual(["source/first", "source/last"], [system["repository"] for system in result["systems"]])
        self.assertEqual(
            [("entry[1]", "manifest_entry"), ("source/broken", "system_summary")],
            [(failure["repository"], failure["stage"]) for failure in result["failures"]],
        )
        self.assertEqual([], published)


class TenantTokenTests(unittest.TestCase):
    def test_catalogue_files_are_read_with_the_manifest_repositorys_token(self):
        requests = []
        bodies = {
            "https://api.github.com/repos/org/catalogue/contents/manifest.json?ref=main": [APP_ENTRY],
            "https://api.github.com/repos/org/catalogue/contents/app/db-schema/database.schema.json?ref=main": DATABASE,
            "https://api.github.com/repos/org/catalogue/contents/app/event-catalog/events-and-commands.json?ref=main": EVENTS,
            "https://api.github.com/repos/org/catalogue/contents/app/service-dependencies/service-dependencies.json?ref=main": DEPENDENCIES,
            "https://api.github.com/repos/org/catalogue/contents/app/open-api?ref=main": CONTROLLERS,
        }

        class Response:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return self.body

        def urlopen(request, timeout):
            requests.append(request)
            return Response(json.dumps(bodies[request.full_url]).encode("utf-8"))

        def invoke(project, name, model, payload, max_attempts=2):
            self.assertEqual(DATABASE, payload["database"])
            self.assertEqual(["OrdersController"], payload["apiControllers"])
            return SUMMARY

        with patch("orchestrator.github_app.read_token", side_effect=lambda tenant: f"token-for-{tenant}") as read_token, \
                patch("orchestrator.urllib.request.urlopen", side_effect=urlopen):
            result = run_manifest(
                object(), {"sourceUrl": MANIFEST_URL, "deferPublication": True},
                "generator", "publisher", "gpt-4o", invoker=invoke,
            )
        self.assertTrue(result["success"])
        read_token.assert_called_once_with("org/catalogue")
        self.assertEqual(5, len(requests))
        self.assertTrue(all(request.get_header("Authorization") == "Bearer token-for-org/catalogue" for request in requests))
        accepts = {request.full_url: request.get_header("Accept") for request in requests}
        self.assertEqual("application/vnd.github.raw", accepts["https://api.github.com/repos/org/catalogue/contents/manifest.json?ref=main"])
        self.assertEqual("application/vnd.github+json", accepts["https://api.github.com/repos/org/catalogue/contents/app/open-api?ref=main"])

    def test_without_a_token_files_are_read_anonymously_from_raw(self):
        requests = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return b"[]"

        def urlopen(request, timeout):
            requests.append(request)
            return Response()

        with patch("orchestrator.urllib.request.urlopen", side_effect=urlopen):
            _fetch_json("https://raw.githubusercontent.com/org/catalogue/main/manifest.json")
        self.assertEqual("https://raw.githubusercontent.com/org/catalogue/main/manifest.json", requests[0].full_url)
        self.assertIsNone(requests[0].get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
