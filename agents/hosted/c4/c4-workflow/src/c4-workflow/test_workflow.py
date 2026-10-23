import unittest

from workflow import WorkflowError, parse_workflow_request, run_workflow


SOURCE = "https://github.com/source/app/tree/main/src"
FILES = [f"https://github.com/source/app/blob/main/src/File{index}.cs" for index in range(2)]
DRAWIO = '<mxfile><diagram name="Context"><mxGraphModel><root /></mxGraphModel></diagram></mxfile>'


def catalog():
    return {
        "repository": "source/app",
        "ref": "main",
        "path": "src",
        "c4Model": {
            "systemName": "Application",
            "description": "Application under analysis.",
            "people": [],
            "systems": [{"id": "application", "name": "Application", "description": "System under analysis.", "external": False, "evidence": []}],
            "containers": [],
            "relationships": [],
            "evidence": [],
        },
        "diagrams": {
            "context": {"format": "drawio", "filename": "context.drawio", "drawioXml": DRAWIO},
            "container": {"format": "drawio", "filename": "container.drawio", "drawioXml": DRAWIO},
        },
    }


class WorkflowTests(unittest.TestCase):
    def test_direct_request_requires_target_repository(self):
        with self.assertRaisesRegex(WorkflowError, "targetRepository"):
            parse_workflow_request('{"sourceUrl":"%s"}' % SOURCE)
        self.assertTrue(parse_workflow_request('{"sourceUrl":"%s","deferPublication":true}' % SOURCE)["deferPublication"])

    def test_deferred_workflow_invokes_generator_once_with_all_files(self):
        generator_payloads = []

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return {"sourceFiles": FILES, "excludedFiles": []}
            if name == "generator":
                generator_payloads.append(payload)
                return catalog()
            raise AssertionError(name)

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o", generator_batch_size=1, invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual([FILES], [payload["sourceFiles"] for payload in generator_payloads])
        self.assertEqual("context.drawio", result["catalogs"][0]["catalog"]["diagrams"]["context"]["filename"])

    def test_generation_error_fails_closed(self):
        publisher_called = False

        def invoke(project, name, model, payload, max_attempts=2):
            nonlocal publisher_called
            if name == "discovery":
                return {"sourceFiles": FILES, "excludedFiles": []}
            if name == "publisher":
                publisher_called = True
            return {"error": {"code": "invalid_model_output", "message": "bad c4"}}

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )
        self.assertFalse(result["success"])
        self.assertEqual("invalid_model_output", result["errors"][0]["code"])
        self.assertEqual([], result["catalogs"])
        self.assertFalse(publisher_called)

    def test_direct_workflow_publishes_deterministic_directory(self):
        def invoke(project, name, model, payload, max_attempts=2):
            if name == "discovery":
                return {"sourceFiles": FILES[:1], "excludedFiles": []}
            if name == "generator":
                return catalog()
            self.assertEqual("app/c4", payload["catalogs"][0]["targetDirectory"])
            return {"success": True, "status": "created"}

        result = run_workflow(
            object(), {"sourceUrl": SOURCE, "targetRepository": "target/catalog"},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )
        self.assertTrue(result["success"])


class TenantTests(unittest.TestCase):
    def test_tenant_is_passed_to_discovery_only(self):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append((name, payload))
            if name == "discovery":
                return {"sourceFiles": FILES, "excludedFiles": []}
            return catalog()

        run_workflow(
            object(), {"sourceUrl": SOURCE, "deferPublication": True, "tenant": "tenant/atlas"},
            "discovery", "generator", "publisher", "gpt-4o", invoker=invoke,
        )
        self.assertEqual({"sourceUrl": SOURCE, "tenant": "tenant/atlas"}, calls[0][1])
        self.assertNotIn("tenant", calls[1][1])


if __name__ == "__main__":
    unittest.main()
