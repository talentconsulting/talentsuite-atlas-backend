import base64
import json
import unittest

from github_pr import PublicationError, parse_request, publish


SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "Bids API", "version": "1.0.0"},
    "paths": {},
    "components": {"schemas": {}},
}


def request_payload():
    return {
        "repository": "target/specs",
        "specifications": [
            {
                "apiFile": "https://github.com/source/app/blob/main/src/Api/BidsController.cs",
                "specification": SPEC,
            }
        ],
        "targetDirectory": "openapi",
    }


class FakeClient:
    def __init__(self, existing=None):
        self.existing = existing
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET" and path == "/repos/target/specs":
            return {"default_branch": "main"}
        if method == "GET" and "/contents/" in path:
            if self.existing is None:
                raise PublicationError("github_api_error", "GitHub API returned HTTP 404. Not Found")
            return {"type": "file", "content": base64.b64encode(self.existing).decode("ascii")}
        if method == "GET" and "/git/ref/heads/" in path:
            return {"object": {"sha": "base-sha"}}
        if method == "GET" and "/git/commits/base-sha" in path:
            return {"tree": {"sha": "base-tree"}}
        if method == "POST" and path.endswith("/git/blobs"):
            return {"sha": "blob-sha"}
        if method == "POST" and path.endswith("/git/trees"):
            return {"sha": "tree-sha"}
        if method == "POST" and path.endswith("/git/commits"):
            return {"sha": "commit-sha"}
        if method == "POST" and path.endswith("/git/refs"):
            return {}
        if method == "POST" and path.endswith("/pulls"):
            return {"html_url": "https://github.com/target/specs/pull/7", "number": 7}
        raise AssertionError((method, path, payload))


class GitHubPrTests(unittest.TestCase):
    def test_parse_requires_json_object(self):
        with self.assertRaisesRegex(PublicationError, "one JSON object"):
            parse_request("[]")

    def test_rejects_non_openapi_specification(self):
        payload = request_payload()
        payload["specifications"][0]["specification"] = {"openapi": "3.0.0"}
        with self.assertRaisesRegex(PublicationError, "OpenAPI 3.1.0"):
            publish(payload, FakeClient())

    def test_rejects_invalid_branch_name(self):
        payload = request_payload()
        payload["branchName"] = "../main"
        with self.assertRaisesRegex(PublicationError, "valid Git branch"):
            publish(payload, FakeClient())

    def test_creates_single_commit_and_pull_request(self):
        client = FakeClient()
        result = publish(request_payload(), client, branch_factory=lambda: "openapi-specs/test")
        self.assertEqual("created", result["status"])
        self.assertEqual(7, result["pullRequestNumber"])
        self.assertEqual(
            "openapi/BidsController.openapi.json", result["filesWritten"][0]["path"]
        )
        self.assertEqual("created", result["filesWritten"][0]["action"])
        ref_call = next(call for call in client.calls if call[1].endswith("/git/refs"))
        self.assertEqual("refs/heads/openapi-specs/test", ref_call[2]["ref"])

    def test_does_not_create_branch_when_content_is_unchanged(self):
        content = (json.dumps(SPEC, indent=2) + "\n").encode("utf-8")
        client = FakeClient(existing=content)
        result = publish(request_payload(), client)
        self.assertEqual("unchanged", result["status"])
        self.assertFalse(any(call[1].endswith("/git/refs") for call in client.calls))
        self.assertFalse(any(call[1].endswith("/pulls") for call in client.calls))

    def test_writes_explicit_target_path_and_manifest_in_same_commit(self):
        payload = request_payload()
        payload["specifications"][0]["targetPath"] = "openapi/source/app/Bids.openapi.json"
        payload["manifestFile"] = {
            "path": "repoManifest.json",
            "content": [{"github-repo": "https://github.com/source/app"}],
        }
        client = FakeClient()

        result = publish(payload, client, branch_factory=lambda: "openapi-specs/manifest")

        self.assertEqual(
            ["openapi/source/app/Bids.openapi.json", "repoManifest.json"],
            [item["path"] for item in result["filesWritten"]],
        )
        tree_call = next(call for call in client.calls if call[1].endswith("/git/trees"))
        self.assertEqual(2, len(tree_call[2]["tree"]))


class ManifestMergeTests(unittest.TestCase):
    """manifestNodes: only the flow's own nodes are applied to the manifest currently on the base branch."""

    @staticmethod
    def entry(repo, **nodes):
        return {"github-repo": f"https://github.com/o/{repo}", **{
            name: {"path-to-scan": path, "last-commit-hash-scanned": commit} for name, (path, commit) in nodes.items()
        }}

    def merge(self, current, supplied, nodes):
        from github_pr import _merge_manifest
        existing = (json.dumps(current, indent=2) + "\n").encode("utf-8")
        content = (json.dumps(supplied, indent=2) + "\n").encode("utf-8")
        return json.loads(_merge_manifest(existing, {"content": content, "document": supplied, "nodes": nodes}))

    def test_applies_own_node_and_keeps_entries_added_since_the_run_started(self):
        current = [self.entry("new", flow=("tree/main/src", "")), self.entry("a", flow=("tree/main/src", "old"), other=("tree/main/src", "o2"))]
        supplied = [self.entry("a", flow=("tree/main/src", "new"), other=("tree/main/src", "stale"))]
        merged = self.merge(current, supplied, ["flow"])
        self.assertEqual(["https://github.com/o/new", "https://github.com/o/a"], [e["github-repo"] for e in merged])
        self.assertEqual("new", merged[1]["flow"]["last-commit-hash-scanned"])
        self.assertEqual("o2", merged[1]["other"]["last-commit-hash-scanned"])  # another flow's node is untouched

    def test_does_not_re_add_removed_entries(self):
        merged = self.merge([], [self.entry("gone", flow=("tree/main/src", "x"))], ["flow"])
        self.assertEqual([], merged)

    def test_skips_a_node_whose_path_to_scan_changed(self):
        current = [self.entry("a", flow=("tree/main/api", ""))]
        merged = self.merge(current, [self.entry("a", flow=("tree/main/src", "x"))], ["flow"])
        self.assertEqual("", merged[0]["flow"]["last-commit-hash-scanned"])

    def test_without_nodes_writes_the_supplied_manifest_unchanged(self):
        from github_pr import _merge_manifest
        content = b"[]\n"
        self.assertEqual(content, _merge_manifest(b'[{"github-repo": "x"}]', {"content": content, "document": [], "nodes": None}))

    def test_manifest_nodes_must_be_non_empty_strings(self):
        from github_pr import _manifest_nodes
        self.assertIsNone(_manifest_nodes(None))
        self.assertEqual(["flow"], _manifest_nodes([" flow "]))
        for bad in ([], [""], "flow", [1]):
            with self.assertRaises(PublicationError):
                _manifest_nodes(bad)


if __name__ == "__main__":
    unittest.main()
