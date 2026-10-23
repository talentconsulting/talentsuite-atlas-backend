import json
import threading
import unittest
from unittest.mock import patch

from orchestrator import (
    ManifestEntry,
    ManifestError,
    MANIFEST_NODE,
    latest_commit,
    parse_request,
    run_manifest,
    validate_manifest,
)


MANIFEST_URL = "https://github.com/target/catalogs/blob/main/manifest.json"
OLD_SHA = "1" * 40
NEW_SHA = "2" * 40
OTHER_NEW_SHA = "3" * 40


LAST_COMMIT_DATE = "2024-01-01T00:00:00Z"


def make_catalog(
    repository="source/app", ref="main", path="src", last_commit_date=LAST_COMMIT_DATE,
    projects=None, sdks=None, dotnet_support=None,
):
    return {
        "repository": repository,
        "ref": ref,
        "path": path,
        "lastCommitDate": last_commit_date,
        "projects": projects if projects is not None else [{"path": "src/App/App.csproj", "targetFrameworks": ["net8.0"]}],
        "sdks": sdks if sdks is not None else [],
        "dotnetSupport": dotnet_support if dotnet_support is not None else [],
    }


def manifest(last_commit=OLD_SHA, extra_entries=None):
    entries = [{
        "github-repo": "https://github.com/source/app",
        "repo-metadata": {"path-to-scan": "tree/main/src", "last-commit-hash-scanned": last_commit},
    }]
    if extra_entries:
        entries.extend(extra_entries)
    return entries


def repo_metadata_entry(index, last_commit=""):
    return {
        "github-repo": f"https://github.com/source/app{index}",
        MANIFEST_NODE: {"path-to-scan": "tree/main/src", "last-commit-hash-scanned": last_commit},
    }


def other_pipeline_entry(index):
    return {
        "github-repo": f"https://github.com/other/app{index}",
        "c4": {"path-to-scan": "tree/main/src", "last-commit-hash-scanned": OLD_SHA},
    }


class ManifestTests(unittest.TestCase):
    def test_validate_manifest_skips_entries_without_repo_metadata_node(self):
        shared = manifest("")
        shared.append({
            "github-repo": "https://github.com/source/other",
            "c4": {"path-to-scan": "tree/main/src", "last-commit-hash-scanned": ""},
        })
        entries, skipped = validate_manifest(shared, 25)
        self.assertEqual(1, len(entries))
        self.assertEqual("source/app", entries[0].repository_name)
        self.assertEqual([], skipped)

    def test_validate_manifest_rejects_invalid_commit_hash_or_path_to_scan(self):
        bad_commit = manifest("not-a-sha")
        with self.assertRaises(ManifestError):
            validate_manifest(bad_commit, 25)

        bad_path = manifest("")
        bad_path[0][MANIFEST_NODE]["path-to-scan"] = "src"
        with self.assertRaises(ManifestError):
            validate_manifest(bad_path, 25)

    def test_validate_manifest_rejects_duplicate_repositories(self):
        duplicated = manifest("")
        duplicated.append({
            "github-repo": "https://github.com/source/app",
            "repo-metadata": {"path-to-scan": "tree/main/other", "last-commit-hash-scanned": ""},
        })
        with self.assertRaises(ManifestError):
            validate_manifest(duplicated, 25)

    def test_latest_commit_rejects_ref_not_matching_default_branch(self):
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

    def test_input_has_exactly_source_url(self):
        self.assertEqual(MANIFEST_URL, parse_request(json.dumps({"sourceUrl": MANIFEST_URL}))["sourceUrl"])
        with self.assertRaises(ManifestError):
            parse_request(json.dumps({"sourceUrl": MANIFEST_URL, "extra": True}))

    def test_run_manifest_returns_up_to_date_without_invoking_any_agent(self):
        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL}, "workflow", "publisher", "gpt-4o",
            manifest_loader=lambda blob: manifest(NEW_SHA), commit_resolver=lambda entry: NEW_SHA,
            invoker=lambda *args, **kwargs: self.fail("No agent should be invoked."),
        )
        self.assertTrue(result["success"])
        self.assertEqual("up_to_date", result["status"])
        self.assertEqual(0, result["changedCount"])
        self.assertIsNone(result["pullRequest"])

    def test_run_manifest_calls_workflow_once_per_changed_repo_with_defer_publication_true(self):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append((name, payload))
            if name == "workflow":
                return {"success": True, "catalogs": [{"sourceUrl": payload["sourceUrl"], "catalog": make_catalog()}]}
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL}, "workflow", "publisher", "gpt-4o",
            manifest_loader=lambda blob: manifest(), commit_resolver=lambda entry: NEW_SHA, invoker=invoke,
        )
        self.assertTrue(result["success"])
        workflow_calls = [call for call in calls if call[0] == "workflow"]
        self.assertEqual(1, len(workflow_calls))
        self.assertEqual(
            {"sourceUrl": "https://github.com/source/app/tree/main/src", "deferPublication": True},
            workflow_calls[0][1],
        )

    def test_run_manifest_allows_workflow_result_with_empty_projects_and_sdks(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "workflow":
                empty_catalog = make_catalog(projects=[], sdks=[])
                return {"success": True, "catalogs": [{"sourceUrl": payload["sourceUrl"], "catalog": empty_catalog}]}
            self.assertEqual(1, len(payload["catalogs"]))
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL}, "workflow", "publisher", "gpt-4o",
            manifest_loader=lambda blob: manifest(), commit_resolver=lambda entry: NEW_SHA, invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual(1, result["generatedCatalogCount"])
        self.assertEqual([], result["failures"])

    def test_run_manifest_updates_commit_hash_only_on_success(self):
        two_repo_manifest = manifest(OLD_SHA, extra_entries=[{
            "github-repo": "https://github.com/source/other",
            "repo-metadata": {"path-to-scan": "tree/main/src", "last-commit-hash-scanned": OLD_SHA},
        }])

        def commit_resolver(entry):
            return NEW_SHA if entry.repository == "app" else OTHER_NEW_SHA

        published = {}

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "workflow":
                if payload["sourceUrl"].endswith("source/app/tree/main/src"):
                    return {"success": True, "catalogs": [{"sourceUrl": payload["sourceUrl"], "catalog": make_catalog()}]}
                return {"success": False, "generationErrors": [{"message": "Could not scan repository."}], "catalogs": []}
            published.update(payload)
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL}, "workflow", "publisher", "gpt-4o",
            manifest_loader=lambda blob: two_repo_manifest, commit_resolver=commit_resolver, invoker=invoke,
        )
        # One of the two repositories failed, so the overall run is not fully successful even
        # though a pull request was published for the repository that did succeed.
        self.assertFalse(result["success"])
        self.assertIsNotNone(result["pullRequest"])
        self.assertEqual(["repo-metadata"], published["manifestNodes"])
        manifest_sent = published["manifestFile"]["content"]
        self.assertEqual(NEW_SHA, manifest_sent[0][MANIFEST_NODE]["last-commit-hash-scanned"])
        self.assertEqual(OLD_SHA, manifest_sent[1][MANIFEST_NODE]["last-commit-hash-scanned"])
        self.assertEqual(1, len(result["failures"]))
        self.assertEqual("workflow", result["failures"][0]["stage"])

    def test_run_manifest_combines_all_successful_catalogs_into_one_pr_creator_call(self):
        two_repo_manifest = manifest(OLD_SHA, extra_entries=[{
            "github-repo": "https://github.com/source/other",
            "repo-metadata": {"path-to-scan": "tree/main/src", "last-commit-hash-scanned": OLD_SHA},
        }])
        publisher_calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "workflow":
                repository = "app" if payload["sourceUrl"].startswith("https://github.com/source/app") else "other"
                catalog = make_catalog(repository=f"source/{repository}")
                return {"success": True, "catalogs": [{"sourceUrl": payload["sourceUrl"], "catalog": catalog}]}
            publisher_calls.append(payload)
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL}, "workflow", "publisher", "gpt-4o",
            manifest_loader=lambda blob: two_repo_manifest, commit_resolver=lambda entry: NEW_SHA, invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual(1, len(publisher_calls))
        self.assertEqual(2, len(publisher_calls[0]["catalogs"]))
        self.assertEqual(2, result["generatedCatalogCount"])

    def test_run_manifest_reports_failed_status_when_no_catalog_succeeds(self):
        def invoke(project, name, model, payload, max_attempts=2):
            return {"success": False, "generationErrors": [{"message": "No .NET version files found."}], "catalogs": []}

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL}, "workflow", "publisher", "gpt-4o",
            manifest_loader=lambda blob: manifest(), commit_resolver=lambda entry: NEW_SHA, invoker=invoke,
        )
        self.assertFalse(result["success"])
        self.assertEqual("failed", result["status"])
        self.assertEqual("workflow", result["failures"][0]["stage"])
        self.assertIsNone(result["pullRequest"])

    def test_manifest_exceeding_max_entries_is_truncated_not_rejected(self):
        oversized = [repo_metadata_entry(i) for i in range(30)]

        entries, skipped = validate_manifest(oversized, 25)

        self.assertEqual(25, len(entries))
        self.assertEqual([f"source/app{i}" for i in range(25, 30)], skipped)

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "workflow":
                repo = payload["sourceUrl"].split("/")[4]
                catalog = make_catalog(repository=f"source/{repo}")
                return {"success": True, "catalogs": [{"sourceUrl": payload["sourceUrl"], "catalog": catalog}]}
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL}, "workflow", "publisher", "gpt-4o",
            manifest_loader=lambda blob: oversized, commit_resolver=lambda entry: NEW_SHA, invoker=invoke,
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
        shared = [other_pipeline_entry(i) for i in range(30)] + [repo_metadata_entry(100), repo_metadata_entry(101)]

        entries, skipped = validate_manifest(shared, 25)

        self.assertEqual(2, len(entries))
        self.assertEqual([], skipped)

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "workflow":
                repo = payload["sourceUrl"].split("/")[4]
                catalog = make_catalog(repository=f"source/{repo}")
                return {"success": True, "catalogs": [{"sourceUrl": payload["sourceUrl"], "catalog": catalog}]}
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL}, "workflow", "publisher", "gpt-4o",
            manifest_loader=lambda blob: shared, commit_resolver=lambda entry: NEW_SHA, invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertFalse(result["truncated"])
        self.assertEqual(0, result["skippedCount"])
        self.assertEqual([], result["skippedRepositories"])
        self.assertEqual(2, result["checkedCount"])

    def test_repositories_are_generated_concurrently(self):
        repos = [repo_metadata_entry(index) for index in range(3)]
        # Fails with BrokenBarrierError unless all three workflows are in flight at once.
        barrier = threading.Barrier(3, timeout=5)
        published = []

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "workflow":
                barrier.wait()
                repository = payload["sourceUrl"].split("/")[4]
                catalog = make_catalog(repository=f"source/{repository}")
                return {"success": True, "catalogs": [{"sourceUrl": payload["sourceUrl"], "catalog": catalog}]}
            published.append(payload)
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL}, "workflow", "publisher", "gpt-4o",
            workflow_concurrency=3,
            manifest_loader=lambda blob: repos, commit_resolver=lambda entry: NEW_SHA, invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual(3, result["generatedCatalogCount"])
        self.assertEqual(1, len(published))

    def test_results_are_processed_in_manifest_order_not_completion_order(self):
        repos = [repo_metadata_entry(index) for index in range(3)]
        later_done = threading.Event()
        finished = []
        published = []

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "workflow":
                repository = payload["sourceUrl"].split("/")[4]
                if repository == "app0":
                    # The first repository finishes last, but must still come first in the PR.
                    later_done.wait(timeout=5)
                else:
                    finished.append(repository)
                    if len(finished) == 2:
                        later_done.set()
                catalog = make_catalog(repository=f"source/{repository}")
                return {"success": True, "catalogs": [{"sourceUrl": payload["sourceUrl"], "catalog": catalog}]}
            published.append(payload)
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL}, "workflow", "publisher", "gpt-4o",
            workflow_concurrency=3,
            manifest_loader=lambda blob: repos, commit_resolver=lambda entry: NEW_SHA, invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertTrue(later_done.is_set())
        self.assertEqual(
            ["app0/repo-metadata/repo-metadata.json", "app1/repo-metadata/repo-metadata.json", "app2/repo-metadata/repo-metadata.json"],
            [catalog["targetPath"] for catalog in published[0]["catalogs"]],
        )
        self.assertEqual(
            ["source/app0", "source/app1", "source/app2"],
            [repository["repository"] for repository in result["generatedRepositories"]],
        )
        self.assertEqual(
            [NEW_SHA] * 3,
            [entry[MANIFEST_NODE]["last-commit-hash-scanned"] for entry in published[0]["manifestFile"]["content"]],
        )

    def test_malformed_entry_beyond_the_cap_still_raises_during_validation(self):
        oversized = [repo_metadata_entry(i) for i in range(31)]
        oversized[30][MANIFEST_NODE] = {"path-to-scan": "tree/main/src"}  # missing last-commit-hash-scanned

        with self.assertRaisesRegex(ManifestError, f"entry 30.{MANIFEST_NODE}"):
            validate_manifest(oversized, 25)


if __name__ == "__main__":
    unittest.main()
