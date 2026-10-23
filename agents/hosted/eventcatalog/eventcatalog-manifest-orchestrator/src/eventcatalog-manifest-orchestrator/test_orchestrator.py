import json
import threading
import unittest
from unittest.mock import patch

from orchestrator import ManifestEntry, ManifestError, latest_commit, parse_blob_url, parse_request, run_manifest, validate_manifest


MANIFEST_URL = "https://github.com/target/catalogs/blob/main/repoManifest.json"
REPO_URL = "https://github.com/source/app"
OLD_SHA = "1" * 40
NEW_SHA = "2" * 40
CATALOG = {
    "repository": "source/app",
    "ref": "main",
    "path": "src/Data",
    "commands": [{"name": "CreateOrder"}],
    "events": [],
}


def manifest(last_commit=OLD_SHA):
    return [
        {
            "github-repo": REPO_URL,
            "eventcatalog": {
                "path-to-scan": "tree/main/src/Data",
                "last-commit-hash-scanned": last_commit,
            },
        }
    ]


def eventcatalog_entry(index, last_commit=""):
    return {
        "github-repo": f"https://github.com/source/app{index}",
        "eventcatalog": {
            "path-to-scan": "tree/main/src/Data",
            "last-commit-hash-scanned": last_commit,
        },
    }


def other_pipeline_entry(index):
    return {
        "github-repo": f"https://github.com/other/app{index}",
        "specs": {
            "path-to-scan": "tree/main/src/Api",
            "last-commit-hash-scanned": OLD_SHA,
        },
    }


class ManifestTests(unittest.TestCase):
    def test_latest_commit_rejects_a_ref_that_is_not_the_default_branch(self):
        entry = ManifestEntry(
            index=0, owner="source", repository="app", repository_url="https://github.com/source/app",
            ref="feature-x", scan_path="src", path_to_scan="tree/feature-x/src", last_commit="",
            manifest_node="eventcatalog",
        )

        def fake_read_url(url):
            if url == "https://api.github.com/repos/source/app":
                return json.dumps({"default_branch": "main"}).encode("utf-8")
            raise AssertionError(f"unexpected URL {url}")

        with patch("orchestrator._read_url", side_effect=fake_read_url):
            with self.assertRaisesRegex(ManifestError, "default branch is 'main'"):
                latest_commit(entry)

    def test_latest_commit_resolves_the_sha_for_the_default_branch(self):
        entry = ManifestEntry(
            index=0, owner="source", repository="app", repository_url="https://github.com/source/app",
            ref="main", scan_path="src", path_to_scan="tree/main/src", last_commit="",
            manifest_node="eventcatalog",
        )

        def fake_read_url(url):
            if url == "https://api.github.com/repos/source/app":
                return json.dumps({"default_branch": "main"}).encode("utf-8")
            if url == "https://api.github.com/repos/source/app/commits/main":
                return json.dumps({"sha": NEW_SHA}).encode("utf-8")
            raise AssertionError(f"unexpected URL {url}")

        with patch("orchestrator._read_url", side_effect=fake_read_url):
            self.assertEqual(NEW_SHA, latest_commit(entry))

    def test_input_has_exactly_source_url(self):
        self.assertEqual(MANIFEST_URL, parse_request(json.dumps({"sourceUrl": MANIFEST_URL}))["sourceUrl"])
        with self.assertRaises(ManifestError):
            parse_request(json.dumps({"sourceUrl": MANIFEST_URL, "extra": True}))

    def test_validates_manifest_catalog(self):
        entries, skipped = validate_manifest(manifest(""), 25)
        self.assertEqual([], skipped)
        entry = entries[0]
        self.assertEqual("source/app", entry.repository_name)
        self.assertEqual("main", entry.ref)
        self.assertEqual("https://github.com/source/app/tree/main/src/Data", entry.source_url)

    def test_shared_manifest_ignores_specs_and_entries_without_eventcatalog(self):
        shared = manifest("")
        shared[0]["specs"] = {
            "path-to-scan": "tree/main/src/Api",
            "last-commit-hash-scanned": OLD_SHA,
        }
        shared.append(
            {
                "specs": {
                    "path-to-scan": "tree/main/src/Api",
                    "last-commit-hash-scanned": OLD_SHA,
                },
                "owner": "api-platform",
            }
        )

        entries, skipped = validate_manifest(shared, 25)

        self.assertEqual(1, len(entries))
        self.assertEqual("source/app", entries[0].repository_name)
        self.assertEqual([], skipped)

    def test_rejects_duplicate_repositories(self):
        with self.assertRaisesRegex(ManifestError, "duplicated"):
            validate_manifest(manifest() + manifest(), 25)

    def test_up_to_date_manifest_does_not_invoke_agents(self):
        def fail_invoker(*args, **kwargs):
            raise AssertionError("No agent should be invoked.")

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "workflow",
            "publisher",
            "gpt-4o",
            manifest_loader=lambda blob: manifest(NEW_SHA),
            commit_resolver=lambda entry: NEW_SHA,
            invoker=fail_invoker,
        )
        self.assertTrue(result["success"])
        self.assertEqual("up_to_date", result["status"])
        self.assertIsNone(result["pullRequest"])

    def test_changed_repository_generates_then_publishes_manifest_atomically(self):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append((name, payload, max_attempts))
            if name == "workflow":
                self.assertEqual(
                    {"sourceUrl": "https://github.com/source/app/tree/main/src/Data", "deferPublication": True},
                    payload,
                )
                return {
                    "success": True,
                    "catalogs": [{"sourceUrl": payload["sourceUrl"], "catalog": CATALOG}],
                }
            if name == "publisher":
                self.assertEqual(1, max_attempts)
                self.assertEqual(["eventcatalog", "event-catalog"], payload["manifestNodes"])
                self.assertEqual(
                    NEW_SHA,
                    payload["manifestFile"]["content"][0]["eventcatalog"]["last-commit-hash-scanned"],
                )
                self.assertEqual(
                    "app/event-catalog/events-and-commands.json",
                    payload["catalogs"][0]["targetPath"],
                )
                self.assertEqual(
                    "https://github.com/source/app/tree/main/src/Data",
                    payload["catalogs"][0]["sourceUrl"],
                )
                return {
                    "success": True,
                    "status": "created",
                    "pullRequestUrl": "https://github.com/target/catalogs/pull/1",
                }
            raise AssertionError(name)

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "workflow",
            "publisher",
            "gpt-4o",
            manifest_loader=lambda blob: manifest(),
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual(1, result["generatedRepositoryCount"])
        self.assertEqual(1, result["generatedCatalogCount"])
        self.assertEqual(["workflow", "publisher"], [call[0] for call in calls])

    def test_partial_generation_warnings_are_surfaced_but_still_published(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "workflow":
                return {
                    "success": True,
                    "catalogs": [{"sourceUrl": payload["sourceUrl"], "catalog": CATALOG}],
                    "generationErrors": [
                        {
                            "sourceUrl": payload["sourceUrl"],
                            "files": ["https://github.com/source/app/blob/main/src/Data/Weird.sql"],
                            "errorType": "WorkflowError",
                            "message": "tables[0] has an invalid shape.",
                        }
                    ],
                }
            if name == "publisher":
                return {"success": True, "status": "created"}
            raise AssertionError(name)

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "workflow",
            "publisher",
            "gpt-4o",
            manifest_loader=lambda blob: manifest(),
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual(1, len(result["generatedRepositories"][0]["warnings"]))
        self.assertIn("invalid shape", result["generatedRepositories"][0]["warnings"][0]["message"])

    def test_calls_workflow_once_for_every_changed_eventcatalog_repository(self):
        second = manifest()
        second[0]["github-repo"] = "https://github.com/source/accounts"
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append(name)
            if name == "workflow":
                repository = "source/accounts" if "/accounts/" in payload["sourceUrl"] else "source/app"
                return {
                    "success": True,
                    "catalogs": [{"sourceUrl": payload["sourceUrl"], "catalog": {**CATALOG, "repository": repository}}],
                }
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "workflow",
            "publisher",
            "gpt-4o",
            manifest_loader=lambda blob: manifest() + second,
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual(2, result["generatedRepositoryCount"])
        self.assertEqual(2, result["generatedCatalogCount"])
        self.assertEqual(["workflow", "workflow", "publisher"], calls)

    def test_repositories_are_generated_concurrently(self):
        repos = [eventcatalog_entry(index) for index in range(3)]
        # Fails with BrokenBarrierError unless all three workflows are in flight at once.
        barrier = threading.Barrier(3, timeout=5)

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "workflow":
                barrier.wait()
                repository = "source/" + payload["sourceUrl"].split("/")[4]
                return {
                    "success": True,
                    "catalogs": [{"sourceUrl": payload["sourceUrl"], "catalog": {**CATALOG, "repository": repository}}],
                }
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "workflow",
            "publisher",
            "gpt-4o",
            workflow_concurrency=3,
            manifest_loader=lambda blob: repos,
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual(3, result["generatedRepositoryCount"])

    def test_results_are_processed_in_manifest_order_not_completion_order(self):
        repos = [eventcatalog_entry(index) for index in range(3)]
        later_done = threading.Event()
        finished = []
        published = []

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "workflow":
                repository = "source/" + payload["sourceUrl"].split("/")[4]
                if repository == "source/app0":
                    # The first manifest entry finishes last, but must still be processed first.
                    later_done.wait(timeout=5)
                elif repository == "source/app2":
                    later_done.set()
                finished.append(repository)
                return {
                    "success": True,
                    "catalogs": [{"sourceUrl": payload["sourceUrl"], "catalog": {**CATALOG, "repository": repository}}],
                }
            published.append(payload)
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "workflow",
            "publisher",
            "gpt-4o",
            workflow_concurrency=3,
            manifest_loader=lambda blob: repos,
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertNotEqual("source/app0", finished[0])
        self.assertEqual(
            ["app0/event-catalog/events-and-commands.json", "app1/event-catalog/events-and-commands.json", "app2/event-catalog/events-and-commands.json"],
            [catalog["targetPath"] for catalog in published[0]["catalogs"]],
        )
        self.assertEqual(
            ["source/app0", "source/app1", "source/app2"],
            [repository["repository"] for repository in result["generatedRepositories"]],
        )
        self.assertEqual(
            [NEW_SHA] * 3,
            [entry["eventcatalog"]["last-commit-hash-scanned"] for entry in published[0]["manifestFile"]["content"]],
        )

    def test_legacy_db_catalog_node_remains_supported(self):
        legacy = manifest("")
        legacy[0]["event-catalog"] = legacy[0].pop("eventcatalog")

        entries, _skipped = validate_manifest(legacy, 25)
        entry = entries[0]

        self.assertEqual("event-catalog", entry.manifest_node)

    def test_rejects_ambiguous_eventcatalog_node_aliases(self):
        ambiguous = manifest("")
        ambiguous[0]["event-catalog"] = dict(ambiguous[0]["eventcatalog"])

        with self.assertRaisesRegex(ManifestError, "both eventcatalog and event-catalog"):
            validate_manifest(ambiguous, 25)

    def test_generation_failure_does_not_update_or_publish(self):
        def invoke(project, name, model, payload, max_attempts=2):
            return {
                "success": False,
                "generationErrors": [{"message": "No event or command sources found."}],
                "catalogs": [],
            }

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "workflow",
            "publisher",
            "gpt-4o",
            manifest_loader=lambda blob: manifest(),
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke,
        )
        self.assertFalse(result["success"])
        self.assertEqual("catalog_workflow", result["failures"][0]["stage"])
        self.assertIsNone(result["pullRequest"])

    def test_manifest_exceeding_max_entries_is_truncated_not_rejected(self):
        oversized = [eventcatalog_entry(i) for i in range(30)]

        entries, skipped = validate_manifest(oversized, 25)

        self.assertEqual(25, len(entries))
        self.assertEqual([f"source/app{i}" for i in range(25, 30)], skipped)

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "workflow":
                repo = payload["sourceUrl"].split("/")[4]
                return {
                    "success": True,
                    "catalogs": [{"sourceUrl": payload["sourceUrl"], "catalog": {**CATALOG, "repository": f"source/{repo}"}}],
                }
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "workflow",
            "publisher",
            "gpt-4o",
            manifest_loader=lambda blob: oversized,
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertTrue(result["truncated"])
        self.assertEqual(5, result["skippedCount"])
        self.assertEqual([f"source/app{i}" for i in range(25, 30)], result["skippedRepositories"])
        self.assertEqual(25, result["checkedCount"])
        self.assertEqual(25, result["changedCount"])
        self.assertEqual(25, result["generatedRepositoryCount"])
        self.assertEqual(25, result["generatedCatalogCount"])
        self.assertIsNotNone(result["pullRequest"])

    def test_entries_for_other_pipelines_do_not_count_against_this_caps(self):
        shared = [other_pipeline_entry(i) for i in range(30)] + [eventcatalog_entry(100), eventcatalog_entry(101)]

        entries, skipped = validate_manifest(shared, 25)

        self.assertEqual(2, len(entries))
        self.assertEqual([], skipped)

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "workflow":
                repo = payload["sourceUrl"].split("/")[4]
                return {
                    "success": True,
                    "catalogs": [{"sourceUrl": payload["sourceUrl"], "catalog": {**CATALOG, "repository": f"source/{repo}"}}],
                }
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "workflow",
            "publisher",
            "gpt-4o",
            manifest_loader=lambda blob: shared,
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertFalse(result["truncated"])
        self.assertEqual(0, result["skippedCount"])
        self.assertEqual([], result["skippedRepositories"])
        self.assertEqual(2, result["checkedCount"])

    def test_malformed_entry_beyond_the_cap_still_raises_during_validation(self):
        oversized = [eventcatalog_entry(i) for i in range(31)]
        oversized[30]["eventcatalog"] = {"path-to-scan": "tree/main/src/Data"}  # missing last-commit-hash-scanned

        with self.assertRaisesRegex(ManifestError, "entry 30.event-catalog"):
            validate_manifest(oversized, 25)


if __name__ == "__main__":
    unittest.main()
