import base64
import json
import unittest

from github_pr import PublicationError, parse_request, publish


CATALOG = {
    "repository": "source/app", "ref": "main", "path": "src",
    "systemName": "App", "containers": [], "dependencies": [],
}


def payload():
    return {
        "repository": "target/catalogue",
        "catalogs": [{
            "sourceUrl": "https://github.com/source/app/tree/main/src",
            "catalog": CATALOG,
            "targetPath": "app/service-dependencies/service-dependencies.json",
        }],
    }


class FakeClient:
    def __init__(self, existing=None):
        self.existing = existing
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path == "/repos/target/catalogue":
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
            return {"html_url": "https://github.com/target/catalogue/pull/7", "number": 7}
        raise AssertionError((method, path, body))


class PublisherTests(unittest.TestCase):
    def test_rejects_invalid_catalog(self):
        request = payload()
        request["catalogs"][0]["catalog"] = {"dependencies": []}
        with self.assertRaisesRegex(PublicationError, "repository, ref, path"):
            publish(request, FakeClient())

    def test_creates_one_commit_and_pull_request(self):
        client = FakeClient()
        result = publish(payload(), client, branch_factory=lambda: "service-dependencies/test")
        self.assertEqual("created", result["status"])
        self.assertEqual("app/service-dependencies/service-dependencies.json", result["filesWritten"][0]["path"])
        self.assertEqual(1, len([call for call in client.calls if call[1].endswith("/pulls")]))

    def test_unchanged_content_does_not_create_pull_request(self):
        client = FakeClient((json.dumps(CATALOG, indent=2) + "\n").encode("utf-8"))
        result = publish(payload(), client)
        self.assertEqual("unchanged", result["status"])
        self.assertFalse(any(call[1].endswith("/pulls") for call in client.calls))

    def test_catalog_and_manifest_share_one_tree(self):
        request = payload()
        request["manifestFile"] = {"path": "manifest.json", "content": []}
        result = publish(request, FakeClient(), branch_factory=lambda: "service-dependencies/manifest")
        self.assertEqual(2, len(result["filesWritten"]))

    def test_puml_is_written_alongside_the_catalog_at_a_derived_path(self):
        request = payload()
        request["catalogs"][0]["puml"] = "@startuml\n@enduml"
        result = publish(request, FakeClient(), branch_factory=lambda: "service-dependencies/puml")
        paths = [item["path"] for item in result["filesWritten"]]
        self.assertEqual(
            ["app/service-dependencies/service-dependencies.json", "app/service-dependencies/service-dependencies.puml"],
            paths,
        )

    def test_puml_is_optional(self):
        result = publish(payload(), FakeClient(), branch_factory=lambda: "service-dependencies/no-puml")
        self.assertEqual(1, len(result["filesWritten"]))

    def test_rejects_a_non_string_puml(self):
        request = payload()
        request["catalogs"][0]["puml"] = 12345
        with self.assertRaisesRegex(PublicationError, "puml must be a string"):
            publish(request, FakeClient())

    def test_parse_requires_json_object(self):
        with self.assertRaisesRegex(PublicationError, "one JSON object"):
            parse_request("[]")


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
