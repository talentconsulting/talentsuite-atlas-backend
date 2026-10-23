import json
import threading
import unittest
import urllib.error
from io import BytesIO
from unittest.mock import patch

from orchestrator import ManifestEntry, ManifestError, _read_url, latest_commit, parse_request, run_manifest, validate_manifest


MANIFEST_URL = "https://github.com/target/catalogs/blob/main/manifest.json"
OLD_SHA = "1" * 40
NEW_SHA = "2" * 40
DRAWIO = '<mxfile><diagram name="Context"><mxGraphModel><root /></mxGraphModel></diagram></mxfile>'
CATALOG = {
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


def repositories(*names):
    return [dict(manifest()[0], **{"github-repo": f"https://github.com/source/{name}"}) for name in names]


def workflow_result(source_url):
    repository = source_url.split("/")[4]
    return {"success": True, "catalogs": [{"sourceUrl": source_url, "catalog": dict(CATALOG, repository=f"source/{repository}")}]}


def manifest(last_commit=OLD_SHA):
    return [{
        "github-repo": "https://github.com/source/app",
        "specs": {"path-to-scan": "tree/main/src/Api", "last-commit-hash-scanned": OLD_SHA},
        "c4": {"path-to-scan": "tree/main/src", "last-commit-hash-scanned": last_commit},
    }]


class ManifestTests(unittest.TestCase):
    def test_github_error_includes_api_message(self):
        error = urllib.error.HTTPError(
            "https://api.github.com/repos/source/app/commits/main",
            422,
            "Unprocessable Content",
            {},
            BytesIO(b'{"message":"No commit found for SHA: main"}'),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(ManifestError, "No commit found for SHA: main"):
                _read_url(error.url)

    def test_latest_commit_rejects_a_ref_that_is_not_the_default_branch(self):
        entry = ManifestEntry(
            index=0, owner="source", repository="app", repository_url="https://github.com/source/app",
            ref="feature-x", scan_path="src", path_to_scan="tree/feature-x/src", last_commit="",
        )

        def fake_read_url(url, token=None, accept=None):
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

        def fake_read_url(url, token=None, accept=None):
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

    def test_shared_manifest_selects_only_c4_nodes(self):
        shared = manifest("")
        shared.append({
            "github-repo": "https://github.com/source/other",
            "eventcatalog": {"path-to-scan": "tree/main/src", "last-commit-hash-scanned": ""},
        })
        entries = validate_manifest(shared, 25)
        self.assertEqual(1, len(entries))
        self.assertEqual("source/app", entries[0].repository_name)

    def test_up_to_date_manifest_does_not_invoke_agents(self):
        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL}, "workflow", "publisher", "gpt-4o",
            manifest_loader=lambda blob: manifest(NEW_SHA), commit_resolver=lambda entry: NEW_SHA,
            invoker=lambda *args, **kwargs: self.fail("No agent should be invoked."),
        )
        self.assertTrue(result["success"])
        self.assertEqual("up_to_date", result["status"])

    def test_changed_repository_generates_and_publishes_atomically(self):
        calls = []

        def invoke(project, name, model, payload, max_attempts=2):
            calls.append((name, payload))
            if name == "workflow":
                self.assertEqual(
                    {"sourceUrl": "https://github.com/source/app/tree/main/src", "deferPublication": True, "tenant": "target/catalogs"},
                    payload,
                )
                return {"success": True, "catalogs": [{"sourceUrl": payload["sourceUrl"], "catalog": CATALOG}]}
            self.assertEqual(["c4"], payload["manifestNodes"])
            self.assertEqual(NEW_SHA, payload["manifestFile"]["content"][0]["c4"]["last-commit-hash-scanned"])
            self.assertEqual("app/c4", payload["catalogs"][0]["targetDirectory"])
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL}, "workflow", "publisher", "gpt-4o",
            manifest_loader=lambda blob: manifest(), commit_resolver=lambda entry: NEW_SHA, invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual(["workflow", "publisher"], [call[0] for call in calls])

    def test_repositories_are_generated_concurrently(self):
        # Fails with BrokenBarrierError unless all three workflows are in flight at once.
        barrier = threading.Barrier(3, timeout=5)

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "workflow":
                barrier.wait()
                return workflow_result(payload["sourceUrl"])
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL}, "workflow", "publisher", "gpt-4o",
            workflow_concurrency=3,
            manifest_loader=lambda blob: repositories("app", "app2", "app3"),
            commit_resolver=lambda entry: NEW_SHA, invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual(3, result["generatedCatalogCount"])

    def test_results_are_published_in_manifest_order_not_completion_order(self):
        first_may_finish = threading.Event()
        published = []

        def invoke(project, name, model, payload, max_attempts=2):
            if name == "workflow":
                if payload["sourceUrl"].startswith("https://github.com/source/app/"):
                    # The first repository finishes last, but must still be published first.
                    first_may_finish.wait(timeout=5)
                else:
                    first_may_finish.set()
                return workflow_result(payload["sourceUrl"])
            published.append(payload)
            return {"success": True, "status": "created"}

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL}, "workflow", "publisher", "gpt-4o",
            workflow_concurrency=2,
            manifest_loader=lambda blob: repositories("app", "app2"),
            commit_resolver=lambda entry: NEW_SHA, invoker=invoke,
        )
        self.assertTrue(result["success"])
        self.assertEqual(["app/c4", "app2/c4"], [item["targetDirectory"] for item in published[0]["catalogs"]])
        self.assertEqual(["source/app", "source/app2"], [item["repository"] for item in result["generatedRepositories"]])

    def test_generation_failure_does_not_update_or_publish(self):
        def invoke(project, name, model, payload, max_attempts=2):
            return {"success": False, "generationErrors": [{"message": "No C4 diagrams found."}], "catalogs": []}

        result = run_manifest(
            object(), {"sourceUrl": MANIFEST_URL}, "workflow", "publisher", "gpt-4o",
            manifest_loader=lambda blob: manifest(), commit_resolver=lambda entry: NEW_SHA, invoker=invoke,
        )
        self.assertFalse(result["success"])
        self.assertEqual("catalog_workflow", result["failures"][0]["stage"])
        self.assertIsNone(result["pullRequest"])


class TenantTokenTests(unittest.TestCase):
    def test_manifest_and_commits_are_read_with_the_manifest_repositorys_token(self):
        reads = []

        def fake_read_url(url, token=None, accept=None):
            reads.append((url, token, accept))
            if url.endswith("/repos/source/app"):
                return json.dumps({"default_branch": "main"}).encode("utf-8")
            if url.endswith("/commits/main"):
                return json.dumps({"sha": NEW_SHA}).encode("utf-8")
            return json.dumps(manifest(NEW_SHA)).encode("utf-8")

        with patch("orchestrator.github_app.read_token", side_effect=lambda tenant: f"token-for-{tenant}") as read_token, \
                patch("orchestrator._read_url", side_effect=fake_read_url):
            result = run_manifest(object(), {"sourceUrl": MANIFEST_URL}, "workflow", "publisher", "gpt-4o")
        self.assertEqual("up_to_date", result["status"])
        self.assertEqual({"target/catalogs"}, {call.args[0] for call in read_token.call_args_list})
        self.assertEqual(
            ("https://api.github.com/repos/target/catalogs/contents/manifest.json?ref=main", "token-for-target/catalogs", "application/vnd.github.raw"),
            reads[0],
        )
        self.assertTrue(all(token == "token-for-target/catalogs" for _url, token, _accept in reads))


if __name__ == "__main__":
    unittest.main()
