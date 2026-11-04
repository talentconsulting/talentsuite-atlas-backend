import json
import unittest

from workflow import WorkflowError, parse_workflow_request, run_workflow, validate_discovery_output


SOURCE = "https://github.com/source/app/tree/main/src/Api"
API_1 = "https://github.com/source/app/blob/main/src/Api/BidsController.cs"
API_2 = "https://github.com/source/app/blob/main/src/Api/UsersController.cs"
DTO = "https://github.com/source/app/blob/main/src/Api/BidDto.cs"
SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "API", "version": "1.0.0"},
    "paths": {},
    "components": {"schemas": {}},
}


class WorkflowTests(unittest.TestCase):
    def test_input_requires_destination_repository(self):
        with self.assertRaisesRegex(WorkflowError, "targetRepository"):
            parse_workflow_request(json.dumps({"sourceUrl": SOURCE}))

    def test_calls_generator_for_each_api_then_publisher_once(self):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append((name, payload))
            if name == "discovery":
                return [
                    {"apiFile": API_1, "supportingFiles": [DTO]},
                    {"apiFile": API_2, "supportingFiles": []},
                ]
            if name == "generator":
                return dict(SPEC)
            if name == "publisher":
                self.assertEqual(1, max_attempts)
                return {"success": True, "status": "created", "pullRequestUrl": "https://example/pull/1"}
            raise AssertionError(name)

        result = run_workflow(
            object(),
            {"sourceUrl": SOURCE, "targetRepository": "target/specs"},
            "discovery",
            "generator",
            "publisher",
            "gpt-4o",
            invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual(2, result["generatedCount"])
        generator_calls = [call for call in calls if call[0] == "generator"]
        publisher_calls = [call for call in calls if call[0] == "publisher"]
        self.assertEqual(2, len(generator_calls))
        self.assertEqual(1, len(publisher_calls))
        self.assertEqual("app/open-api", publisher_calls[0][1]["targetDirectory"])
        self.assertEqual([API_1, API_2], [item["apiFile"] for item in publisher_calls[0][1]["specifications"]])

    def test_same_named_api_files_publish_to_distinct_paths(self):
        api_web = "https://github.com/source/app/blob/main/src/Web/CertificateController.cs"
        api_api = "https://github.com/source/app/blob/main/src/Api/CertificateController.cs"
        publisher_calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return [
                    {"apiFile": api_web, "supportingFiles": []},
                    {"apiFile": api_api, "supportingFiles": []},
                    {"apiFile": API_2, "supportingFiles": []},
                ]
            if name == "generator":
                return SPEC
            publisher_calls.append(payload)
            return {"success": True}

        run_workflow(
            object(),
            {"sourceUrl": "https://github.com/source/app/tree/main/src", "targetRepository": "target/specs"},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )
        specifications = publisher_calls[0]["specifications"]
        self.assertEqual(
            ["app/open-api/src-Web-CertificateController.openapi.json",
             "app/open-api/src-Api-CertificateController.openapi.json"],
            [item["targetPath"] for item in specifications[:2]],
        )
        self.assertNotIn("targetPath", specifications[2])

    def run_deferred_with_generator(self, responses):
        generator_calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return [{"apiFile": API_1, "supportingFiles": []}]
            generator_calls.append(payload)
            return responses[len(generator_calls) - 1]

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )
        return result, len(generator_calls)

    def test_retries_a_generator_error_once(self):
        error = {"error": {"code": "source_unavailable", "message": "GitHub returned HTTP 502."}}
        result, calls = self.run_deferred_with_generator([error, dict(SPEC)])
        self.assertEqual(2, calls)
        self.assertEqual(1, result["generatedCount"])

    def test_reports_the_generators_own_error(self):
        error = {"error": {"code": "source_unavailable", "message": "GitHub returned HTTP 502."}}
        result, calls = self.run_deferred_with_generator([error, error])
        self.assertEqual(2, calls)
        self.assertFalse(result["success"])
        self.assertEqual(
            "Generator reported source_unavailable: GitHub returned HTTP 502.",
            result["generationErrors"][0]["message"],
        )

    def test_explicit_target_directory_overrides_repository_layout(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return [{"apiFile": API_1, "supportingFiles": []}]
            if name == "generator":
                return dict(SPEC)
            self.assertEqual("custom/specs", payload["targetDirectory"])
            return {"success": True, "status": "created"}

        result = run_workflow(
            object(),
            {
                "sourceUrl": SOURCE,
                "targetRepository": "target/specs",
                "targetDirectory": "custom/specs",
            },
            "discovery",
            "generator",
            "publisher",
            "gpt-4o",
            invoker=invoke,
        )

        self.assertTrue(result["success"])

    def test_partial_generation_publishes_successes_and_reports_failure(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return [
                    {"apiFile": API_1, "supportingFiles": []},
                    {"apiFile": API_2, "supportingFiles": []},
                ]
            if name == "generator" and payload["apiFile"] == API_2:
                raise RuntimeError("generation failed")
            if name == "generator":
                return dict(SPEC)
            self.assertEqual(1, len(payload["specifications"]))
            return {"success": True, "status": "created"}

        result = run_workflow(
            object(),
            {"sourceUrl": SOURCE, "targetRepository": "target/specs"},
            "discovery",
            "generator",
            "publisher",
            "gpt-4o",
            invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual(1, result["generatedCount"])
        self.assertEqual(API_2, result["generationErrors"][0]["apiFile"])

    def test_a_supplied_apifiles_override_skips_calling_discovery(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                raise AssertionError("Discovery must not be called when apiFiles is supplied.")
            if name == "generator":
                return dict(SPEC)
            raise AssertionError("Publisher must not be called in deferred mode.")

        result = run_workflow(
            object(),
            {
                "sourceUrl": SOURCE,
                "deferPublication": True,
                "apiFiles": [{"apiFile": API_1, "supportingFiles": []}],
            },
            "discovery",
            "generator",
            "publisher",
            "gpt-4o",
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual(1, result["discoveredCount"])
        self.assertEqual(API_1, result["specifications"][0]["apiFile"])

    def test_apifiles_override_must_be_an_array(self):
        with self.assertRaisesRegex(WorkflowError, "apiFiles must be an array"):
            parse_workflow_request(json.dumps({"sourceUrl": SOURCE, "deferPublication": True, "apiFiles": "not-a-list"}))

    def test_a_discovery_scan_failure_is_not_treated_as_an_empty_result(self):
        with self.assertRaisesRegex(WorkflowError, "GitHub returned HTTP 404"):
            validate_discovery_output(
                {"error": {"code": "scan_failed", "message": "GitHub returned HTTP 404."}},
                SOURCE,
                100,
            )

    def test_empty_discovery_succeeds_without_calling_the_publisher(self):
        def invoke(project, name, model, payload, max_attempts=2):
            self.assertEqual("discovery", name)
            return []

        result = run_workflow(
            object(),
            {"sourceUrl": SOURCE, "targetRepository": "target/specs"},
            "discovery",
            "generator",
            "publisher",
            "gpt-4o",
            invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual(0, result["discoveredCount"])
        self.assertEqual([], result["specifications"])
        self.assertIsNone(result["pullRequest"])

    def test_deferred_partial_generation_still_succeeds_with_the_good_specifications(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return [
                    {"apiFile": API_1, "supportingFiles": []},
                    {"apiFile": API_2, "supportingFiles": []},
                ]
            if name == "generator" and payload["apiFile"] == API_2:
                raise RuntimeError("generation failed")
            if name == "generator":
                return dict(SPEC)
            raise AssertionError("Publisher must not be called in deferred mode.")

        result = run_workflow(
            object(),
            {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery",
            "generator",
            "publisher",
            "gpt-4o",
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual(1, len(result["specifications"]))
        self.assertEqual(API_2, result["generationErrors"][0]["apiFile"])

    def test_can_defer_publication_and_return_generated_specifications(self):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append(name)
            if name == "discovery":
                return [{"apiFile": API_1, "supportingFiles": []}]
            if name == "generator":
                return dict(SPEC)
            raise AssertionError("Publisher must not be called in deferred mode.")

        result = run_workflow(
            object(),
            {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery",
            "generator",
            "publisher",
            "gpt-4o",
            invoker=invoke,
        )

        self.assertTrue(result["success"])
        self.assertEqual(["discovery", "generator"], calls)
        self.assertEqual(API_1, result["specifications"][0]["apiFile"])
        self.assertIsNone(result["pullRequest"])


class TenantTests(unittest.TestCase):
    def test_tenant_is_passed_to_discovery_and_the_generator(self):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append((name, payload))
            if name == "discovery":
                return [{"apiFile": API_1, "supportingFiles": []}]
            return dict(SPEC)

        run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True, "tenant": "tenant/atlas"},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )
        self.assertEqual({"sourceUrl": SOURCE, "tenant": "tenant/atlas"}, calls[0][1])
        self.assertEqual(("generator", {"apiFile": API_1, "supportingFiles": [], "tenant": "tenant/atlas"}), calls[1])

    def test_tenant_is_passed_to_the_generator_for_supplied_apifiles(self):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append((name, payload))
            return dict(SPEC)

        run_workflow(
            object(),
            {"sourceUrl": SOURCE, "deferPublication": True, "tenant": "tenant/atlas",
             "apiFiles": [{"apiFile": API_1, "supportingFiles": [DTO]}]},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )
        self.assertEqual([("generator", {"apiFile": API_1, "supportingFiles": [DTO], "tenant": "tenant/atlas"})], calls)

    def test_without_a_tenant_the_generator_payload_has_no_tenant(self):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append((name, payload))
            if name == "discovery":
                return [{"apiFile": API_1, "supportingFiles": []}]
            return dict(SPEC)

        run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )
        self.assertEqual({"sourceUrl": SOURCE}, calls[0][1])
        self.assertEqual(("generator", {"apiFile": API_1, "supportingFiles": []}), calls[1])

    def test_tenant_must_be_a_non_empty_string(self):
        with self.assertRaisesRegex(WorkflowError, "tenant"):
            parse_workflow_request(json.dumps({"sourceUrl": SOURCE, "deferPublication": True, "tenant": 1}))


if __name__ == "__main__":
    unittest.main()


class ManifestForwardingTests(unittest.TestCase):
    def test_accepts_and_forwards_the_manifest_update(self):
        import workflow

        request = workflow.parse_workflow_request(json.dumps({
            "sourceUrl": "https://github.com/source/app/tree/main/src",
            "targetRepository": "target/data",
            "manifestFile": {"path": "manifest.json", "content": []},
            "manifestNodes": ["node"],
        }))
        payload = workflow._publisher_payload(request, [])
        self.assertEqual({"path": "manifest.json", "content": []}, payload["manifestFile"])
        self.assertEqual(["node"], payload["manifestNodes"])

    def test_rejects_a_manifest_file_that_is_not_an_object(self):
        import workflow

        with self.assertRaises(workflow.WorkflowError):
            workflow.parse_workflow_request(json.dumps({
                "sourceUrl": "https://github.com/source/app/tree/main/src",
                "targetRepository": "target/data",
                "manifestFile": "manifest.json",
            }))

    def test_keeps_the_manifest_hash_when_a_specification_failed(self):
        published = []

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return [{"apiFile": API_1, "supportingFiles": []}, {"apiFile": API_2, "supportingFiles": []}]
            if name == "generator":
                return SPEC if payload["apiFile"] == API_1 else {"error": {"code": "x", "message": "failed"}}
            published.append(payload)
            return {"success": True}

        run_workflow(
            object(),
            {"sourceUrl": SOURCE, "targetRepository": "target/specs",
             "manifestFile": {"path": "manifest.json", "content": []}, "manifestNodes": ["specs"]},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )
        self.assertNotIn("manifestFile", published[0])
        self.assertNotIn("manifestNodes", published[0])
