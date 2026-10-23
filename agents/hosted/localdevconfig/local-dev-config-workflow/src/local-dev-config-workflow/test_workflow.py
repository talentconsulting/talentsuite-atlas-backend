import json
import threading
import unittest

from workflow import WorkflowError, merge_catalogs, parse_workflow_request, run_workflow


SOURCE = "https://github.com/source/app/tree/main/src/Data"


def _file(index: int) -> str:
    return f"https://github.com/source/app/blob/main/config/File{index}.env"


def _catalog(name: str, kind: str, technology: str, config_keys=None, evidence=None) -> dict:
    return {
        "repository": "source/app",
        "ref": "main",
        "path": "src/Data",
        "localServices": [
            {
                "name": name,
                "kind": kind,
                "technology": technology,
                "configurationKeys": config_keys or [],
                "evidence": evidence or [],
            }
        ],
        "configurationKeys": config_keys or [],
    }


class WorkflowRequestTests(unittest.TestCase):
    def test_parse_workflow_request_requires_source_url_and_defaults_defer_publication_false(self):
        with self.assertRaisesRegex(WorkflowError, "sourceUrl"):
            parse_workflow_request(json.dumps({"targetRepository": "t/r"}))
        # sourceUrl present but deferPublication omitted -> defaults to False, so
        # targetRepository becomes mandatory.
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
        discovery = {"localDevConfigFiles": files, "excludedFiles": []}
        generator_calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                self.assertEqual({"sourceUrl": SOURCE}, payload)
                return discovery
            self.assertEqual("generator", name)
            generator_calls.append(payload["sourceFiles"])
            index = 1 if payload["sourceFiles"] == files[0:5] else 2
            return _catalog(f"service-{index}", "database", "postgres")

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
        names = sorted(service["name"] for service in result["catalogs"][0]["catalog"]["localServices"])
        self.assertEqual(["service-1", "service-2"], names)

    def test_run_workflow_merge_dedupes_by_kind_and_name_first_wins_with_warning(self):
        files = [_file(0), _file(1)]
        discovery = {"localDevConfigFiles": files, "excludedFiles": []}
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return discovery
            calls.append(payload["sourceFiles"])
            technology = "redis6" if payload["sourceFiles"] == [files[0]] else "redis7"
            return _catalog("Redis", "cache", technology)

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
        services = result["catalogs"][0]["catalog"]["localServices"]
        self.assertEqual(1, len(services))
        self.assertEqual("redis6", services[0]["technology"])
        self.assertEqual(1, len(result["generationErrors"]))
        self.assertEqual("DuplicateLocalService", result["generationErrors"][0]["errorType"])

    def test_batches_are_generated_concurrently(self):
        files = [_file(index) for index in range(9)]
        discovery = {"localDevConfigFiles": files, "excludedFiles": []}
        # Fails with BrokenBarrierError unless all three batches are in flight at once.
        barrier = threading.Barrier(3, timeout=5)

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return discovery
            barrier.wait()
            return _catalog(payload["sourceFiles"][0].rsplit("/", 1)[1], "database", "postgres")

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
        self.assertEqual(3, len(result["catalogs"][0]["catalog"]["localServices"]))

    def test_merge_follows_batch_order_not_completion_order(self):
        files = [_file(index) for index in range(6)]
        discovery = {"localDevConfigFiles": files, "excludedFiles": []}
        second_done = threading.Event()

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return discovery
            if payload["sourceFiles"] == files[0:3]:
                # The first batch finishes last, but its technology for "Redis" must still win.
                second_done.wait(timeout=5)
                return _catalog("Redis", "cache", "redis6")
            second_done.set()
            return _catalog("Redis", "cache", "redis7")

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
        self.assertEqual("redis6", result["catalogs"][0]["catalog"]["localServices"][0]["technology"])

    def test_run_workflow_allows_empty_discovery_result(self):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append(name)
            if name == "discovery":
                return {"localDevConfigFiles": [], "excludedFiles": []}
            raise AssertionError("generator must not be called when discovery finds nothing")

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
        self.assertEqual(["discovery"], calls)
        catalog = result["catalogs"][0]["catalog"]
        self.assertEqual([], catalog["localServices"])
        self.assertEqual([], catalog["configurationKeys"])
        self.assertEqual("source/app", catalog["repository"])
        self.assertEqual("main", catalog["ref"])
        self.assertEqual("src/Data", catalog["path"])
        self.assertEqual(0, result["discoveredFileCount"])

    def test_run_workflow_defers_publication_without_calling_pr_creator(self):
        discovery = {"localDevConfigFiles": [_file(0)], "excludedFiles": []}
        catalog = _catalog("Postgres", "database", "postgres14")
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
        discovery = {"localDevConfigFiles": [_file(0)], "excludedFiles": []}
        catalog = _catalog("Postgres", "database", "postgres14")
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
                "app/local-dev-config/local-dev-config.json",
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
        discovery = {"localDevConfigFiles": files, "excludedFiles": []}

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

    def test_run_workflow_fails_when_discovered_files_produce_an_empty_catalog(self):
        discovery = {"localDevConfigFiles": [_file(0), _file(1)], "excludedFiles": []}

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return discovery
            return {**_catalog("unused", "database", "postgres"), "localServices": [], "configurationKeys": []}

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
        self.assertEqual([], result["catalogs"])
        self.assertEqual("empty_catalog", result["errors"][0]["code"])


class MergeCatalogsTests(unittest.TestCase):
    def test_merge_catalogs_unit_tests(self):
        first = {
            "repository": "o/r",
            "ref": "main",
            "path": "p",
            "localServices": [
                {
                    "name": "Postgres",
                    "kind": "database",
                    "technology": "postgres14",
                    "configurationKeys": ["DB_HOST"],
                    "evidence": [{"sourceFile": "f1", "reason": "r"}],
                }
            ],
            "configurationKeys": [{"key": "DB_HOST", "sourceFile": "f1", "reason": "r"}],
        }
        second = {
            "repository": "o/r",
            "ref": "main",
            "path": "p",
            "localServices": [
                {
                    "name": "postgres",
                    "kind": "database",
                    "technology": "postgres16",
                    "configurationKeys": ["db_port"],
                    "evidence": [
                        {"sourceFile": "f1", "reason": "r"},
                        {"sourceFile": "f2", "reason": "r2"},
                    ],
                },
                {
                    "name": "Redis",
                    "kind": "cache",
                    "technology": "redis",
                    "configurationKeys": [],
                    "evidence": [],
                },
            ],
            "configurationKeys": [{"key": "db_port", "sourceFile": "f2", "reason": "r2"}],
        }

        merged, warnings = merge_catalogs([first, second])

        self.assertEqual("o/r", merged["repository"])
        self.assertEqual("main", merged["ref"])
        self.assertEqual("p", merged["path"])
        self.assertEqual(2, len(merged["localServices"]))
        self.assertEqual(["cache", "database"], [service["kind"] for service in merged["localServices"]])

        postgres = next(service for service in merged["localServices"] if service["kind"] == "database")
        self.assertEqual("postgres14", postgres["technology"])
        self.assertEqual(["DB_HOST", "db_port"], postgres["configurationKeys"])
        self.assertEqual(2, len(postgres["evidence"]))

        self.assertEqual(1, len(warnings))
        self.assertEqual("DuplicateLocalService", warnings[0]["errorType"])
        self.assertIn("postgres", warnings[0]["message"].lower())

        self.assertEqual(["DB_HOST", "db_port"], [item["key"] for item in merged["configurationKeys"]])

        with self.assertRaisesRegex(WorkflowError, "disagree"):
            merge_catalogs([first, {**second, "path": "other"}])

    def test_merge_catalogs_dedupes_evidence_and_configuration_keys_exactly(self):
        catalog = {
            "repository": "o/r",
            "ref": "main",
            "path": "p",
            "localServices": [
                {
                    "name": "Cache",
                    "kind": "cache",
                    "technology": "redis",
                    "configurationKeys": ["REDIS_URL", "REDIS_URL"],
                    "evidence": [
                        {"sourceFile": "f1", "reason": "r"},
                        {"sourceFile": "f1", "reason": "r"},
                    ],
                }
            ],
            "configurationKeys": [
                {"key": "REDIS_URL", "sourceFile": "f1", "reason": "r"},
                {"key": "REDIS_URL", "sourceFile": "f1", "reason": "r"},
            ],
        }

        merged, warnings = merge_catalogs([catalog, catalog])

        self.assertEqual([], warnings)
        self.assertEqual(1, len(merged["localServices"]))
        self.assertEqual(1, len(merged["localServices"][0]["configurationKeys"]))
        self.assertEqual(1, len(merged["localServices"][0]["evidence"]))
        self.assertEqual(1, len(merged["configurationKeys"]))


if __name__ == "__main__":
    unittest.main()
