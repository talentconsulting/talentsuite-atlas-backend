import json
import threading
import unittest
from unittest.mock import patch

from workflow import WorkflowError, _fetch_last_commit_date, merge_catalogs, parse_workflow_request, run_workflow


SOURCE = "https://github.com/source/app/tree/main/src"
LAST_COMMIT_DATE = "2024-01-01T00:00:00Z"


def _file(index: int) -> str:
    return f"https://github.com/source/app/blob/main/src/App{index}/App{index}.csproj"


def _catalog(project_path: str, target_frameworks: list[str]) -> dict:
    return {
        "repository": "source/app",
        "ref": "main",
        "path": "src",
        "lastCommitDate": LAST_COMMIT_DATE,
        "projects": [{"path": project_path, "targetFrameworks": target_frameworks}],
        "sdks": [],
        "dotnetSupport": [],
    }


class WorkflowRequestTests(unittest.TestCase):
    def test_parse_workflow_request_requires_source_url_and_defaults_defer_publication_false(self):
        with self.assertRaisesRegex(WorkflowError, "sourceUrl"):
            parse_workflow_request(json.dumps({"targetRepository": "t/r"}))
        with self.assertRaisesRegex(WorkflowError, "targetRepository"):
            parse_workflow_request(json.dumps({"sourceUrl": SOURCE}))

    def test_parse_workflow_request_requires_target_repository_when_not_deferred(self):
        with self.assertRaisesRegex(WorkflowError, "targetRepository"):
            parse_workflow_request(json.dumps({"sourceUrl": SOURCE, "deferPublication": False}))
        parsed = parse_workflow_request(json.dumps({"sourceUrl": SOURCE, "deferPublication": True}))
        self.assertEqual(SOURCE, parsed["sourceUrl"])
        self.assertTrue(parsed["deferPublication"])


class RunWorkflowTests(unittest.TestCase):
    def test_run_workflow_batches_files_and_merges_catalogs(self):
        files = [_file(index) for index in range(7)]
        discovery = {"repoMetadataFiles": files, "excludedFiles": []}
        generator_calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                self.assertEqual({"sourceUrl": SOURCE}, payload)
                return discovery
            self.assertEqual("generator", name)
            generator_calls.append(payload["sourceFiles"])
            index = files.index(payload["sourceFiles"][0])
            return _catalog(f"src/App{index}/App{index}.csproj", ["net8.0"])

        result = run_workflow(
            object(),
            {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery",
            "generator",
            "pr_creator",
            "gpt-4o",
            generator_batch_size=5,
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertCountEqual([files[0:5], files[5:7]], generator_calls)
        paths = sorted(project["path"] for project in result["catalogs"][0]["catalog"]["projects"])
        self.assertEqual(["src/App0/App0.csproj", "src/App5/App5.csproj"], paths)
        self.assertEqual(LAST_COMMIT_DATE, result["catalogs"][0]["catalog"]["lastCommitDate"])

    def test_run_workflow_merge_dedupes_by_path_first_wins_with_warning(self):
        # Two distinct discovered files whose generator batches happen to report the same
        # project path -- discovery itself guarantees unique URLs, so this simulates a
        # defensive edge case in merge_catalogs rather than something discovery would produce.
        files = [_file(0), _file(1)]
        discovery = {"repoMetadataFiles": files, "excludedFiles": []}
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return discovery
            calls.append(payload["sourceFiles"])
            frameworks = ["net8.0"] if payload["sourceFiles"] == [files[0]] else ["net9.0"]
            return _catalog("src/App0/App0.csproj", frameworks)

        result = run_workflow(
            object(),
            {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery",
            "generator",
            "pr_creator",
            "gpt-4o",
            generator_batch_size=1,
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        projects = result["catalogs"][0]["catalog"]["projects"]
        self.assertEqual(1, len(projects))
        self.assertEqual(["net8.0"], projects[0]["targetFrameworks"])
        self.assertEqual(1, len(result["generationErrors"]))
        self.assertEqual("DuplicateProject", result["generationErrors"][0]["errorType"])

    def test_run_workflow_allows_empty_discovery_result(self):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append(name)
            if name == "discovery":
                return {"repoMetadataFiles": [], "excludedFiles": []}
            raise AssertionError("generator must not be called when discovery finds nothing")

        with patch("workflow._fetch_last_commit_date", return_value=LAST_COMMIT_DATE) as commit_date_loader:
            result = run_workflow(
                object(),
                {"sourceUrl": SOURCE, "deferPublication": True},
                "discovery",
                "generator",
                "pr_creator",
                "gpt-4o",
                invoker=invoke,
            )
            commit_date_loader.assert_called_once_with("source", "app", "main", tenant=None)

        self.assertTrue(result["success"])
        self.assertEqual(["discovery"], calls)
        catalog = result["catalogs"][0]["catalog"]
        self.assertEqual([], catalog["projects"])
        self.assertEqual([], catalog["sdks"])
        self.assertEqual([], catalog["dotnetSupport"])
        self.assertEqual("source/app", catalog["repository"])
        self.assertEqual("main", catalog["ref"])
        self.assertEqual("src", catalog["path"])
        self.assertEqual(LAST_COMMIT_DATE, catalog["lastCommitDate"])
        self.assertEqual(0, result["discoveredFileCount"])

    def test_run_workflow_surfaces_commit_lookup_failure_on_empty_discovery_result(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return {"repoMetadataFiles": [], "excludedFiles": []}
            raise AssertionError("generator must not be called when discovery finds nothing")

        with patch("workflow._fetch_last_commit_date", side_effect=WorkflowError("commit_lookup_failed", "boom")):
            result = run_workflow(
                object(),
                {"sourceUrl": SOURCE, "deferPublication": True},
                "discovery",
                "generator",
                "pr_creator",
                "gpt-4o",
                invoker=invoke,
            )

        self.assertFalse(result["success"])
        self.assertEqual("commit_lookup_failed", result["errors"][0]["code"])

    def test_run_workflow_defers_publication_without_calling_pr_creator(self):
        discovery = {"repoMetadataFiles": [_file(0)], "excludedFiles": []}
        catalog = _catalog("src/App0/App0.csproj", ["net8.0"])
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append(name)
            if name == "discovery":
                return discovery
            if name == "generator":
                return catalog
            raise AssertionError("pr-creator must not be called when deferPublication is true")

        result = run_workflow(
            object(),
            {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery",
            "generator",
            "pr_creator",
            "gpt-4o",
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertIsNone(result["pullRequest"])
        self.assertEqual(["discovery", "generator"], calls)

    def test_run_workflow_calls_pr_creator_once_when_not_deferred(self):
        discovery = {"repoMetadataFiles": [_file(0)], "excludedFiles": []}
        catalog = _catalog("src/App0/App0.csproj", ["net8.0"])
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append(name)
            if name == "discovery":
                return discovery
            if name == "generator":
                return catalog
            self.assertEqual(1, max_attempts)
            self.assertEqual("target/repo", payload["repository"])
            self.assertEqual(
                "app/repo-metadata/repo-metadata.json",
                payload["catalogs"][0]["targetPath"],
            )
            self.assertEqual(SOURCE, payload["catalogs"][0]["sourceUrl"])
            return {"success": True, "status": "created"}

        result = run_workflow(
            object(),
            {"sourceUrl": SOURCE, "targetRepository": "target/repo"},
            "discovery",
            "generator",
            "pr_creator",
            "gpt-4o",
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual(["discovery", "generator", "pr_creator"], calls)

    def test_run_workflow_reports_generation_failed_when_all_batches_fail_and_files_were_discovered(self):
        files = [_file(0), _file(1)]
        discovery = {"repoMetadataFiles": files, "excludedFiles": []}

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return discovery
            return {"error": {"code": "invalid_model_output", "message": "bad output"}}

        result = run_workflow(
            object(),
            {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery",
            "generator",
            "pr_creator",
            "gpt-4o",
            generator_batch_size=1,
            invoker=invoke,
            retry_delay_seconds=0,
        )

        self.assertFalse(result["success"])
        self.assertEqual(0, result["generatedCatalogCount"])
        self.assertEqual("generation_failed", result["errors"][0]["code"])
        self.assertEqual(2, len(result["generationErrors"]))

    def test_batches_are_generated_concurrently(self):
        files = [_file(index) for index in range(9)]
        discovery = {"repoMetadataFiles": files, "excludedFiles": []}
        # Fails with BrokenBarrierError unless all three batches are in flight at once.
        barrier = threading.Barrier(3, timeout=5)

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return discovery
            barrier.wait()
            index = files.index(payload["sourceFiles"][0])
            return _catalog(f"src/App{index}/App{index}.csproj", ["net8.0"])

        result = run_workflow(
            object(),
            {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery",
            "generator",
            "pr_creator",
            "gpt-4o",
            generator_batch_size=3,
            generator_concurrency=3,
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual(3, len(result["catalogs"][0]["catalog"]["projects"]))

    def test_merge_follows_batch_order_not_completion_order(self):
        files = [_file(index) for index in range(6)]
        discovery = {"repoMetadataFiles": files, "excludedFiles": []}
        second_done = threading.Event()

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return discovery
            if payload["sourceFiles"] == files[0:3]:
                # The first batch finishes last, but its targetFrameworks must still win.
                second_done.wait(timeout=5)
                return _catalog("src/App0/App0.csproj", ["net8.0"])
            second_done.set()
            return _catalog("src/App0/App0.csproj", ["net9.0"])

        result = run_workflow(
            object(),
            {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery",
            "generator",
            "pr_creator",
            "gpt-4o",
            generator_batch_size=3,
            generator_concurrency=2,
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual(["net8.0"], result["catalogs"][0]["catalog"]["projects"][0]["targetFrameworks"])
        self.assertEqual("DuplicateProject", result["generationErrors"][0]["errorType"])

    def test_failed_batch_is_retried_with_exponential_backoff(self):
        discovery = {"repoMetadataFiles": [_file(0)], "excludedFiles": []}
        attempts = []

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return discovery
            attempts.append(name)
            if len(attempts) < 3:
                raise RuntimeError("throttled")
            return _catalog("src/App0/App0.csproj", ["net8.0"])

        with patch("workflow.time.sleep") as sleep:
            result = run_workflow(
                object(),
                {"sourceUrl": SOURCE, "deferPublication": True},
                "discovery",
                "generator",
                "pr_creator",
                "gpt-4o",
                invoker=invoke,
                retry_delay_seconds=2,
            )

        self.assertTrue(result["success"])
        self.assertEqual(3, len(attempts))
        self.assertEqual([(2,), (4,)], [call.args for call in sleep.call_args_list])


class MergeCatalogsTests(unittest.TestCase):
    def test_merge_catalogs_combines_disjoint_projects_and_sdks(self):
        first = {
            "repository": "o/r", "ref": "main", "path": "p", "lastCommitDate": LAST_COMMIT_DATE,
            "projects": [{"path": "src/App1/App1.csproj", "targetFrameworks": ["net8.0"]}],
            "sdks": [{"path": "src/global.json", "version": "8.0.100"}],
            "dotnetSupport": [{"targetFramework": "net8.0", "endOfSupport": "2026-11-10", "supportPhase": "LTS"}],
        }
        second = {
            "repository": "o/r", "ref": "main", "path": "p", "lastCommitDate": LAST_COMMIT_DATE,
            "projects": [{"path": "src/App2/App2.csproj", "targetFrameworks": ["net9.0"]}],
            "sdks": [],
            "dotnetSupport": [{"targetFramework": "net9.0", "endOfSupport": "2026-11-10", "supportPhase": "STS"}],
        }

        merged, warnings = merge_catalogs([first, second])

        self.assertEqual("o/r", merged["repository"])
        self.assertEqual([], warnings)
        self.assertEqual(
            ["src/App1/App1.csproj", "src/App2/App2.csproj"],
            [item["path"] for item in merged["projects"]],
        )
        self.assertEqual([{"path": "src/global.json", "version": "8.0.100"}], merged["sdks"])
        self.assertEqual(LAST_COMMIT_DATE, merged["lastCommitDate"])
        self.assertEqual(
            ["net8.0", "net9.0"],
            [item["targetFramework"] for item in merged["dotnetSupport"]],
        )

        with self.assertRaisesRegex(WorkflowError, "disagree"):
            merge_catalogs([first, {**second, "path": "other"}])

    def test_merge_catalogs_rejects_mismatched_last_commit_date(self):
        first = {
            "repository": "o/r", "ref": "main", "path": "p", "lastCommitDate": LAST_COMMIT_DATE,
            "projects": [{"path": "src/App1/App1.csproj", "targetFrameworks": ["net8.0"]}],
            "sdks": [],
            "dotnetSupport": [],
        }
        second = {**first, "lastCommitDate": "2024-02-02T00:00:00Z"}

        with self.assertRaisesRegex(WorkflowError, "disagree"):
            merge_catalogs([first, second])

    def test_merge_catalogs_dedupes_identical_project_without_warning(self):
        catalog = {
            "repository": "o/r", "ref": "main", "path": "p", "lastCommitDate": LAST_COMMIT_DATE,
            "projects": [{"path": "src/App/App.csproj", "targetFrameworks": ["net8.0"]}],
            "sdks": [{"path": "src/global.json", "version": "8.0.100"}],
            "dotnetSupport": [{"targetFramework": "net8.0", "endOfSupport": "2026-11-10", "supportPhase": "LTS"}],
        }

        merged, warnings = merge_catalogs([catalog, catalog])

        self.assertEqual([], warnings)
        self.assertEqual(1, len(merged["projects"]))
        self.assertEqual(1, len(merged["sdks"]))
        self.assertEqual(1, len(merged["dotnetSupport"]))

    def test_merge_catalogs_dedupes_dotnet_support_across_batches_by_target_framework(self):
        first = {
            "repository": "o/r", "ref": "main", "path": "p", "lastCommitDate": LAST_COMMIT_DATE,
            "projects": [{"path": "src/App1/App1.csproj", "targetFrameworks": ["net8.0"]}],
            "sdks": [],
            "dotnetSupport": [{"targetFramework": "net8.0", "endOfSupport": "2026-11-10", "supportPhase": "LTS"}],
        }
        second = {
            "repository": "o/r", "ref": "main", "path": "p", "lastCommitDate": LAST_COMMIT_DATE,
            "projects": [{"path": "src/App2/App2.csproj", "targetFrameworks": ["net8.0"]}],
            "sdks": [],
            "dotnetSupport": [{"targetFramework": "net8.0", "endOfSupport": "2026-11-10", "supportPhase": "LTS"}],
        }

        merged, warnings = merge_catalogs([first, second])

        self.assertEqual([], warnings)
        self.assertEqual(
            [{"targetFramework": "net8.0", "endOfSupport": "2026-11-10", "supportPhase": "LTS"}],
            merged["dotnetSupport"],
        )


class TenantTests(unittest.TestCase):
    def test_tenant_is_passed_to_discovery_and_the_generator_only(self):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append((name, payload))
            if name == "discovery":
                return {"repoMetadataFiles": [_file(0)], "excludedFiles": []}
            return _catalog("src/App0/App0.csproj", ["net8.0"])

        run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True, "tenant": "tenant/atlas"},
            "discovery", "generator", "pr_creator", "gpt-4o", invoker=invoke,
        )
        self.assertEqual({"sourceUrl": SOURCE, "tenant": "tenant/atlas"}, calls[0][1])
        self.assertEqual({"sourceUrl": SOURCE, "sourceFiles": [_file(0)], "tenant": "tenant/atlas"}, calls[1][1])

    def test_without_a_tenant_no_tenant_is_sent(self):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append((name, payload))
            if name == "discovery":
                return {"repoMetadataFiles": [_file(0)], "excludedFiles": []}
            return _catalog("src/App0/App0.csproj", ["net8.0"])

        run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "pr_creator", "gpt-4o", invoker=invoke,
        )
        self.assertTrue(all("tenant" not in payload for _name, payload in calls))

    def test_commit_date_for_an_empty_discovery_is_read_with_the_tenants_token(self):
        def invoke(project, name, model, payload, max_attempts=2):
            return {"repoMetadataFiles": [], "excludedFiles": []}

        with patch("workflow._fetch_last_commit_date", return_value=LAST_COMMIT_DATE) as commit_date_loader:
            run_workflow(
                object(), {"sourceUrl": SOURCE, "deferPublication": True, "tenant": "tenant/atlas"},
                "discovery", "generator", "pr_creator", "gpt-4o", invoker=invoke,
            )
        commit_date_loader.assert_called_once_with("source", "app", "main", tenant="tenant/atlas")

    def test_commit_date_lookup_uses_the_tenants_read_token(self):
        requests = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps({"commit": {"committer": {"date": LAST_COMMIT_DATE}}}).encode("utf-8")

        def urlopen(request, timeout):
            requests.append(request)
            return Response()

        with patch("workflow.github_app.read_token", return_value="installation-token") as read_token, \
                patch("workflow.urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(LAST_COMMIT_DATE, _fetch_last_commit_date("source", "app", "main", tenant="tenant/atlas"))
        read_token.assert_called_once_with("tenant/atlas")
        self.assertEqual("Bearer installation-token", requests[0].get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
