import json
import unittest

from workflow import CATALOG_FIELDS, WorkflowError, parse_workflow_request, run_workflow


SOURCE = "https://github.com/source/app/tree/main/src"
BLOB = "https://github.com/source/app/blob/main/src/App.Web/Controllers/{}.cs"


def page(page_id):
    return {"id": page_id, "kind": "mvc-action", "routes": [page_id.lower()], "methods": ["GET"],
            "sourceFile": f"src/App.Web/Controllers/{page_id}.cs", "line": 1, "requiresAuthentication": True,
            "isEntryPoint": page_id == "Home", "purpose": None}


def discovery(pages=("Home", "Apply", "Preview"), batches=None):
    pages = list(pages)
    transitions = [{"from": a, "to": b, "trigger": "link", "condition": None, "evidence": [f"src/x.cs:{i + 1}"]}
                   for i, (a, b) in enumerate(zip(pages, pages[1:]))]
    return {
        "repository": "source/app", "ref": "main", "path": "src", "webProjects": ["src/App.Web"] if pages else [],
        "graph": {"pages": [page(p) for p in pages], "transitions": transitions, "siteNavigation": [],
                  "unresolved": [], "selfLinkCount": 0},
        "batches": batches if batches is not None else ([{"pageIds": pages, "files": [BLOB.format("Home")]}] if pages else []),
        "excludedFiles": [],
    }


class FakeAgents:
    def __init__(self, discovered=None, describe=None, journeys=None, publish=None):
        self.discovered = discovered if discovered is not None else discovery()
        self.describe = describe
        self.journeys = journeys
        self.publish = publish
        self.calls = []

    def __call__(self, project, name, model, payload, max_attempts=2):
        self.calls.append((name, payload))
        if name == "discovery":
            return self.discovered
        if name == "pr":
            return self.publish or {"success": True, "pullRequestUrl": "https://github.com/t/r/pull/1"}
        if payload["mode"] == "describe":
            if self.describe is not None:
                return self.describe(payload)
            return {"pages": [{"id": p["id"], "purpose": f"{p['id']} page"} for p in payload["pages"]],
                    "transitions": [{**t, "condition": "always"} for t in payload["transitions"]]}
        if self.journeys is not None:
            return self.journeys(payload)
        return {"journeys": [{"name": "Apply", "description": "d", "kind": "linear", "steps": ["Home", "Apply", "Preview"],
                              "hub": None, "sections": [], "then": []}], "warnings": []}


def run(agents, **request):
    return run_workflow(object(), {"sourceUrl": SOURCE, "deferPublication": True, **request},
                        "discovery", "generator", "pr", "gpt-4o", invoker=agents, retry_delay_seconds=0)


class WorkflowTests(unittest.TestCase):
    def test_describes_names_and_returns_one_catalog(self):
        agents = FakeAgents()
        result = run(agents)
        self.assertTrue(result["success"])
        catalog = result["catalogs"][0]["catalog"]
        self.assertEqual(set(CATALOG_FIELDS), set(catalog))
        self.assertEqual("Home page", catalog["pages"][0]["purpose"])
        self.assertEqual("always", catalog["transitions"][0]["condition"])
        self.assertEqual(["Apply"], [j["name"] for j in catalog["journeys"]])
        self.assertEqual((3, 1), (result["pageCount"], result["journeyCount"]))
        self.assertNotIn("pr", [name for name, _ in agents.calls])

    def test_journeys_request_carries_described_pages_and_conditions(self):
        agents = FakeAgents()
        run(agents)
        journeys_payload = [p for name, p in agents.calls if name == "generator" and p["mode"] == "journeys"][0]
        self.assertEqual("Apply page", journeys_payload["pages"][1]["purpose"])
        self.assertEqual("always", journeys_payload["transitions"][0]["condition"])

    def test_discovery_errors_fail_the_run_instead_of_publishing_an_empty_graph(self):
        result = run(FakeAgents(discovered={"error": {"code": "source_unavailable", "message": "GitHub returned HTTP 503."}}))
        self.assertFalse(result["success"])
        self.assertEqual("source_unavailable", result["errors"][0]["code"])
        self.assertEqual([], result["catalogs"])

    def test_a_repository_without_pages_is_a_valid_empty_catalog(self):
        agents = FakeAgents(discovered=discovery(pages=()))
        result = run(agents)
        self.assertTrue(result["success"])
        self.assertEqual([], result["catalogs"][0]["catalog"]["pages"])
        self.assertEqual(["discovery"], [name for name, _ in agents.calls])

    def test_one_failed_batch_is_reported_but_the_rest_is_published(self):
        batches = [{"pageIds": ["Home"], "files": [BLOB.format("Home")]},
                   {"pageIds": ["Apply", "Preview"], "files": [BLOB.format("Apply")]}]

        def describe(payload):
            if payload["pages"][0]["id"] == "Home":
                return {"error": {"code": "invalid_model_output", "message": "bad"}}
            return {"pages": [{"id": p["id"], "purpose": "ok"} for p in payload["pages"]], "transitions": []}

        result = run(FakeAgents(discovered=discovery(batches=batches), describe=describe))
        self.assertTrue(result["success"])
        self.assertEqual(1, len(result["generationErrors"]))
        purposes = {p["id"]: p["purpose"] for p in result["catalogs"][0]["catalog"]["pages"]}
        self.assertEqual({"Home": None, "Apply": "ok", "Preview": "ok"}, purposes)

    def test_fails_when_every_batch_fails_so_the_next_run_retries(self):
        result = run(FakeAgents(describe=lambda payload: {"error": {"code": "server_error", "message": "500"}}))
        self.assertFalse(result["success"])
        self.assertEqual("generation_failed", result["errors"][0]["code"])

    def test_a_failed_journeys_call_still_publishes_the_described_graph(self):
        result = run(FakeAgents(journeys=lambda payload: {"error": {"code": "invalid_model_output", "message": "bad"}}))
        self.assertTrue(result["success"])
        self.assertEqual([], result["catalogs"][0]["catalog"]["journeys"])
        self.assertEqual("journeys", result["generationErrors"][0]["stage"])

    def test_rejects_discovery_batches_that_miss_pages(self):
        result = run(FakeAgents(discovered=discovery(batches=[{"pageIds": ["Home"], "files": [BLOB.format("Home")]}])))
        self.assertFalse(result["success"])
        self.assertEqual("invalid_discovery_output", result["errors"][0]["code"])

    def test_publishes_through_the_pr_creator_when_not_deferred(self):
        agents = FakeAgents()
        result = run_workflow(object(), {"sourceUrl": SOURCE, "targetRepository": "target/catalogue"},
                              "discovery", "generator", "pr", "gpt-4o", invoker=agents, retry_delay_seconds=0)
        self.assertTrue(result["success"])
        payload = [p for name, p in agents.calls if name == "pr"][0]
        self.assertEqual("app/user-journeys/user-journeys.json", payload["catalogs"][0]["targetPath"])

    def test_request_validation(self):
        with self.assertRaisesRegex(WorkflowError, "targetRepository"):
            parse_workflow_request(json.dumps({"sourceUrl": SOURCE}))
        with self.assertRaisesRegex(WorkflowError, "sourceUrl"):
            parse_workflow_request(json.dumps({"sourceUrl": "https://github.com/a/b/blob/main/x"}))


if __name__ == "__main__":
    unittest.main()
