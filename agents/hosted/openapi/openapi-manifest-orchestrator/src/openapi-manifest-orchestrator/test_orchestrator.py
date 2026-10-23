import json
import threading
import unittest
from unittest.mock import patch

from orchestrator import (
    ManifestEntry,
    ManifestError,
    _spec_target_paths,
    latest_commit,
    parse_blob_url,
    parse_request,
    run_manifest,
    validate_manifest,
)


MANIFEST_URL = "https://github.com/target/specs/blob/main/repoManifest.json"
REPO_URL = "https://github.com/source/app"
OLD_SHA = "1" * 40
NEW_SHA = "2" * 40
API_FILE = "https://github.com/source/app/blob/main/src/Api/BidsController.cs"
SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "API", "version": "1.0.0"},
    "paths": {"/bids": {}},
    "components": {"schemas": {}},
}


def manifest(last_commit=OLD_SHA):
    return [
        {
            "github-repo": REPO_URL,
            "specs": {
                "path-to-scan": "tree/main/src/Api",
                "last-commit-hash-scanned": last_commit,
            },
        }
    ]


def openapi_entry(index, last_commit=""):
    return {
        "github-repo": f"https://github.com/source/app{index}",
        "specs": {
            "path-to-scan": "tree/main/src/Api",
            "last-commit-hash-scanned": last_commit,
        },
    }


def other_pipeline_entry(index):
    return {
        "github-repo": f"https://github.com/other/app{index}",
        "dbschema": {
            "path-to-scan": "tree/main/src/Data",
            "last-commit-hash-scanned": OLD_SHA,
        },
    }


class ManifestTests(unittest.TestCase):
    def test_latest_commit_rejects_a_ref_that_is_not_the_default_branch(self):
        entry = ManifestEntry(
            index=0, owner="source", repository="app", repository_url="https://github.com/source/app",
            ref="feature-x", scan_path="src", path_to_scan="tree/feature-x/src", last_commit="",
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
        )

        def fake_read_url(url):
            if url == "https://api.github.com/repos/source/app":
                return json.dumps({"default_branch": "main"}).encode("utf-8")
            if url == "https://api.github.com/repos/source/app/commits/main":
                return json.dumps({"sha": NEW_SHA}).encode("utf-8")
            raise AssertionError(f"unexpected URL {url}")

        with patch("orchestrator._read_url", side_effect=fake_read_url):
            self.assertEqual(NEW_SHA, latest_commit(entry))

    def test_spec_target_paths_disambiguates_same_named_files_in_different_projects(self):
        entry = ManifestEntry(
            index=0, owner="source", repository="app", repository_url="https://github.com/source/app",
            ref="main", scan_path="src", path_to_scan="tree/main/src", last_commit="",
        )
        api_files = [
            "https://github.com/source/app/blob/main/src/App.Api/Controllers/AgreementController.cs",
            "https://github.com/source/app/blob/main/src/App.Web/Controllers/AgreementController.cs",
        ]

        paths = _spec_target_paths(entry, api_files)

        self.assertEqual(len(set(paths)), 2)
        self.assertTrue(all(path.startswith("app/open-api/") for path in paths))

    def test_spec_target_paths_keeps_the_short_form_when_no_collision(self):
        entry = ManifestEntry(
            index=0, owner="source", repository="app", repository_url="https://github.com/source/app",
            ref="main", scan_path="src", path_to_scan="tree/main/src", last_commit="",
        )
        paths = _spec_target_paths(entry, ["https://github.com/source/app/blob/main/src/Api/BidsController.cs"])
        self.assertEqual(["app/open-api/BidsController.openapi.json"], paths)

    def test_input_has_exactly_source_url(self):
        self.assertEqual(MANIFEST_URL, parse_request(json.dumps({"sourceUrl": MANIFEST_URL}))["sourceUrl"])
        with self.assertRaises(ManifestError):
            parse_request(json.dumps({"sourceUrl": MANIFEST_URL, "extra": True}))

    def test_validates_manifest_schema(self):
        entries, skipped = validate_manifest(manifest(""), 25)
        self.assertEqual([], skipped)
        entry = entries[0]
        self.assertEqual("source/app", entry.repository_name)
        self.assertEqual("main", entry.ref)
        self.assertEqual("https://github.com/source/app/tree/main/src/Api", entry.source_url)

    def test_shared_manifest_ignores_db_schema_and_entries_without_specs(self):
        shared = manifest("")
        shared[0]["db-schema"] = {
            "path-to-scan": "tree/main/src/Data",
            "last-commit-hash-scanned": "",
        }
        shared.append(
            {
                "dbschema": {
                    "path-to-scan": "tree/main/src/Data",
                    "last-commit-hash-scanned": "",
                },
                "owner": "data-platform",
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
            "discovery",
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
            if name == "discovery":
                return [{"apiFile": API_FILE, "supportingFiles": []}]
            if name == "workflow":
                self.assertTrue(payload["deferPublication"])
                self.assertEqual([{"apiFile": API_FILE, "supportingFiles": []}], payload["apiFiles"])
                return {
                    "success": True,
                    "specifications": [{"apiFile": API_FILE, "specification": SPEC}],
                }
            if name == "publisher":
                self.assertEqual(1, max_attempts)
                self.assertEqual(["specs"], payload["manifestNodes"])
                self.assertEqual(NEW_SHA, payload["manifestFile"]["content"][0]["specs"]["last-commit-hash-scanned"])
                self.assertEqual(
                    "app/open-api/BidsController.openapi.json",
                    payload["specifications"][0]["targetPath"],
                )
                return {"success": True, "status": "created", "pullRequestUrl": "https://github.com/target/specs/pull/1"}
            raise AssertionError(name)

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "discovery",
            "workflow",
            "publisher",
            "gpt-4o",
            manifest_loader=lambda blob: manifest(),
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual(1, result["generatedRepositoryCount"])
        self.assertEqual(["discovery", "workflow", "publisher"], [call[0] for call in calls])

    def test_repositories_are_generated_concurrently(self):
        repos = [openapi_entry(index) for index in range(3)]
        # Fails with BrokenBarrierError unless all three workflows are in flight at once.
        barrier = threading.Barrier(3, timeout=5)

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                base = payload["sourceUrl"].replace("/tree/", "/blob/")
                return [{"apiFile": f"{base}/File.cs", "supportingFiles": []}]
            if name == "workflow":
                barrier.wait()
                return {
                    "success": True,
                    "specifications": [{"apiFile": item["apiFile"], "specification": SPEC} for item in payload["apiFiles"]],
                }
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "discovery",
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
        self.assertEqual([], result["failures"])

    def test_results_are_processed_in_manifest_order_not_completion_order(self):
        repos = [openapi_entry(index) for index in range(2)]
        second_done = threading.Event()
        published = []

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                base = payload["sourceUrl"].replace("/tree/", "/blob/")
                return [{"apiFile": f"{base}/File.cs", "supportingFiles": []}]
            if name == "workflow":
                if payload["sourceUrl"].startswith("https://github.com/source/app0/"):
                    # The first repository finishes last, but it must still take the only spec slot.
                    self.assertTrue(second_done.wait(timeout=5))
                else:
                    second_done.set()
                return {
                    "success": True,
                    "specifications": [{"apiFile": item["apiFile"], "specification": SPEC} for item in payload["apiFiles"]],
                }
            published.append(payload)
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "discovery",
            "workflow",
            "publisher",
            "gpt-4o",
            max_specs=1,
            workflow_concurrency=2,
            manifest_loader=lambda blob: repos,
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke,
        )

        self.assertEqual(["source/app0"], [item["repository"] for item in result["generatedRepositories"]])
        self.assertEqual(["source/app1"], [item["repository"] for item in result["failures"]])
        self.assertEqual("ManifestError", result["failures"][0]["errorType"])
        manifest_content = published[0]["manifestFile"]["content"]
        self.assertEqual(NEW_SHA, manifest_content[0]["specs"]["last-commit-hash-scanned"])
        self.assertEqual("", manifest_content[1]["specs"]["last-commit-hash-scanned"])

    def test_a_partial_generation_failure_still_publishes_the_successful_specs(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return [{"apiFile": API_FILE, "supportingFiles": []}]
            if name == "workflow":
                return {
                    "success": True,
                    "specifications": [{"apiFile": API_FILE, "specification": SPEC}],
                    "generationErrors": [{"apiFile": "https://github.com/source/app/blob/main/src/Api/Bad.cs", "errorType": "WorkflowError", "message": "invalid spec"}],
                }
            if name == "publisher":
                self.assertEqual(1, len(payload["specifications"]))
                return {"success": True, "status": "created", "pullRequestUrl": "https://github.com/target/specs/pull/1"}
            raise AssertionError(name)

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "discovery",
            "workflow",
            "publisher",
            "gpt-4o",
            manifest_loader=lambda blob: manifest(),
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual(1, result["generatedRepositoryCount"])
        self.assertEqual(1, len(result["generatedRepositories"][0]["warnings"]))

    def test_a_repository_with_no_api_files_still_advances_and_counts_as_generated(self):
        second = manifest()
        second[0]["github-repo"] = "https://github.com/source/events-only"

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                if payload["sourceUrl"].startswith("https://github.com/source/events-only"):
                    return []
                return [{"apiFile": API_FILE, "supportingFiles": []}]
            if name == "workflow":
                return {"success": True, "specifications": [{"apiFile": API_FILE, "specification": SPEC}]}
            self.assertEqual("publisher", name)
            self.assertEqual(1, len(payload["specifications"]))
            for node in payload["manifestFile"]["content"]:
                self.assertEqual(NEW_SHA, node["specs"]["last-commit-hash-scanned"])
                self.assertNotIn("in-progress", node["specs"])
            return {"success": True, "status": "created", "pullRequestUrl": "https://github.com/target/specs/pull/2"}

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "discovery",
            "workflow",
            "publisher",
            "gpt-4o",
            manifest_loader=lambda blob: manifest() + second,
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual(2, result["generatedRepositoryCount"])
        self.assertEqual(1, result["generatedSpecCount"])

    def test_generation_failure_does_not_update_or_publish(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return [{"apiFile": API_FILE, "supportingFiles": []}]
            return {"success": False, "generationErrors": [{"apiFile": API_FILE}]}

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "discovery",
            "workflow",
            "publisher",
            "gpt-4o",
            manifest_loader=lambda blob: manifest(),
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke,
        )
        self.assertFalse(result["success"])
        self.assertEqual("spec_generation", result["failures"][0]["stage"])
        self.assertIsNone(result["pullRequest"])

    def test_a_failure_with_no_generation_errors_surfaces_the_top_level_error_detail(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return [{"apiFile": API_FILE, "supportingFiles": []}]
            return {
                "success": False,
                "generationErrors": [],
                "errors": [{"code": "too_many_api_files", "message": "Discovery returned more than 100 APIs."}],
            }

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "discovery",
            "workflow",
            "publisher",
            "gpt-4o",
            manifest_loader=lambda blob: manifest(),
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke,
        )
        self.assertIn("Discovery returned more than 100 APIs.", result["failures"][0]["message"])

    def test_a_large_repository_is_processed_in_batches_across_multiple_runs(self):
        all_files = [
            {"apiFile": f"https://github.com/source/app/blob/main/src/Api/Controller{i}.cs", "supportingFiles": []}
            for i in range(5)
        ]

        def invoke_first(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return all_files
            if name == "workflow":
                self.assertEqual(all_files[:2], payload["apiFiles"])
                return {
                    "success": True,
                    "specifications": [{"apiFile": item["apiFile"], "specification": SPEC} for item in all_files[:2]],
                }
            self.assertEqual("publisher", name)
            node = payload["manifestFile"]["content"][0]["specs"]
            self.assertEqual("", node["last-commit-hash-scanned"])
            self.assertEqual({"commit": NEW_SHA, "completed-files": 2}, node["in-progress"])
            return {"success": True, "status": "created", "pullRequestUrl": "https://github.com/target/specs/pull/1"}

        first_manifest = manifest("")
        first_result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "discovery",
            "workflow",
            "publisher",
            "gpt-4o",
            batch_size=2,
            manifest_loader=lambda blob: first_manifest,
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke_first,
        )
        self.assertTrue(first_result["success"])
        self.assertEqual(2, first_result["generatedRepositories"][0]["filesCompleted"])
        self.assertEqual(5, first_result["generatedRepositories"][0]["filesTotal"])

        resumed_manifest = manifest("")
        resumed_manifest[0]["specs"]["in-progress"] = {"commit": NEW_SHA, "completed-files": 2}

        def invoke_second(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return all_files
            if name == "workflow":
                self.assertEqual(all_files[2:4], payload["apiFiles"])
                return {
                    "success": True,
                    "specifications": [{"apiFile": item["apiFile"], "specification": SPEC} for item in all_files[2:4]],
                }
            self.assertEqual("publisher", name)
            node = payload["manifestFile"]["content"][0]["specs"]
            self.assertEqual({"commit": NEW_SHA, "completed-files": 4}, node["in-progress"])
            return {"success": True, "status": "created", "pullRequestUrl": "https://github.com/target/specs/pull/2"}

        second_result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "discovery",
            "workflow",
            "publisher",
            "gpt-4o",
            batch_size=2,
            manifest_loader=lambda blob: resumed_manifest,
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke_second,
        )
        self.assertTrue(second_result["success"])
        self.assertEqual(4, second_result["generatedRepositories"][0]["filesCompleted"])

        final_manifest = manifest("")
        final_manifest[0]["specs"]["in-progress"] = {"commit": NEW_SHA, "completed-files": 4}

        def invoke_third(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return all_files
            if name == "workflow":
                self.assertEqual(all_files[4:], payload["apiFiles"])
                return {
                    "success": True,
                    "specifications": [{"apiFile": item["apiFile"], "specification": SPEC} for item in all_files[4:]],
                }
            self.assertEqual("publisher", name)
            node = payload["manifestFile"]["content"][0]["specs"]
            self.assertEqual(NEW_SHA, node["last-commit-hash-scanned"])
            self.assertNotIn("in-progress", node)
            return {"success": True, "status": "created", "pullRequestUrl": "https://github.com/target/specs/pull/3"}

        third_result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "discovery",
            "workflow",
            "publisher",
            "gpt-4o",
            batch_size=2,
            manifest_loader=lambda blob: final_manifest,
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke_third,
        )
        self.assertTrue(third_result["success"])
        self.assertEqual(5, third_result["generatedRepositories"][0]["filesCompleted"])

    def test_a_new_upstream_commit_restarts_a_batch_in_progress(self):
        all_files = [
            {"apiFile": f"https://github.com/source/app/blob/main/src/Api/Controller{i}.cs", "supportingFiles": []}
            for i in range(3)
        ]
        stale_manifest = manifest()
        stale_manifest[0]["specs"]["in-progress"] = {"commit": OLD_SHA, "completed-files": 2}

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return all_files
            if name == "workflow":
                self.assertEqual(all_files[:2], payload["apiFiles"])
                return {
                    "success": True,
                    "specifications": [{"apiFile": item["apiFile"], "specification": SPEC} for item in all_files[:2]],
                }
            return {"success": True, "status": "created", "pullRequestUrl": "https://github.com/target/specs/pull/1"}

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "discovery",
            "workflow",
            "publisher",
            "gpt-4o",
            batch_size=2,
            manifest_loader=lambda blob: stale_manifest,
            commit_resolver=lambda entry: NEW_SHA,
            invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual(2, result["generatedRepositories"][0]["filesCompleted"])

    def test_manifest_exceeding_max_entries_is_truncated_not_rejected(self):
        oversized = [openapi_entry(i) for i in range(30)]

        entries, skipped = validate_manifest(oversized, 25)

        self.assertEqual(25, len(entries))
        self.assertEqual([f"source/app{i}" for i in range(25, 30)], skipped)

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                base = payload["sourceUrl"].replace("/tree/", "/blob/")
                return [{"apiFile": f"{base}/File.cs", "supportingFiles": []}]
            if name == "workflow":
                return {
                    "success": True,
                    "specifications": [{"apiFile": item["apiFile"], "specification": SPEC} for item in payload["apiFiles"]],
                }
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "discovery",
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
        self.assertIsNotNone(result["pullRequest"])

    def test_entries_for_other_pipelines_do_not_count_against_this_caps(self):
        shared = [other_pipeline_entry(i) for i in range(30)] + [openapi_entry(100), openapi_entry(101)]

        entries, skipped = validate_manifest(shared, 25)

        self.assertEqual(2, len(entries))
        self.assertEqual([], skipped)

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                base = payload["sourceUrl"].replace("/tree/", "/blob/")
                return [{"apiFile": f"{base}/File.cs", "supportingFiles": []}]
            if name == "workflow":
                return {
                    "success": True,
                    "specifications": [{"apiFile": item["apiFile"], "specification": SPEC} for item in payload["apiFiles"]],
                }
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(),
            {"sourceUrl": MANIFEST_URL},
            "discovery",
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
        oversized = [openapi_entry(i) for i in range(31)]
        oversized[30]["specs"] = {"path-to-scan": "tree/main/src/Api"}  # missing last-commit-hash-scanned

        with self.assertRaisesRegex(ManifestError, "entry 30.specs"):
            validate_manifest(oversized, 25)


if __name__ == "__main__":
    unittest.main()
