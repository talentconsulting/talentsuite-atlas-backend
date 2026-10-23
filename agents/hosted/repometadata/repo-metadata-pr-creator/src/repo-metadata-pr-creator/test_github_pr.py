import base64
import json
import unittest

from github_pr import PublicationError, parse_request, publish, validate_request


LAST_COMMIT_DATE = "2024-01-01T00:00:00Z"

CATALOG = {
    "repository": "source/app",
    "ref": "main",
    "path": "src",
    "lastCommitDate": LAST_COMMIT_DATE,
    "projects": [{"path": "src/App/App.csproj", "targetFrameworks": ["net8.0"]}],
    "sdks": [{"path": "src/global.json", "version": "8.0.100"}],
    "dotnetSupport": [{"targetFramework": "net8.0", "endOfSupport": "2026-11-10", "supportPhase": "LTS"}],
}


def payload():
    return {
        "repository": "target/catalogue",
        "catalogs": [{
            "sourceUrl": "https://github.com/source/app/tree/main/src",
            "catalog": CATALOG,
            "targetPath": "app/repo-metadata/repo-metadata.json",
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
    def test_parse_requires_json_object(self):
        with self.assertRaisesRegex(PublicationError, "one JSON object"):
            parse_request("[]")

    def test_validate_request_rejects_invalid_catalog_shape(self):
        request = payload()
        request["catalogs"][0]["catalog"] = {"repository": "source/app"}
        with self.assertRaisesRegex(PublicationError, "repository, ref, path, lastCommitDate, projects, sdks, and dotnetSupport"):
            validate_request(request)

    def test_validate_request_rejects_missing_last_commit_date(self):
        request = payload()
        request["catalogs"][0]["catalog"] = {key: value for key, value in CATALOG.items() if key != "lastCommitDate"}
        with self.assertRaisesRegex(PublicationError, "repository, ref, path, lastCommitDate, projects, sdks, and dotnetSupport"):
            validate_request(request)

    def test_validate_request_defaults_target_path_to_repo_first_convention(self):
        request = {
            "repository": "target/catalogue",
            "catalogs": [{
                "sourceUrl": "https://github.com/source/app/tree/main/src",
                "catalog": CATALOG,
            }],
        }
        result = validate_request(request)
        self.assertEqual(
            "app/repo-metadata/repo-metadata.json",
            result["catalogs"][0]["path"],
        )

    def test_validate_request_honors_target_directory_override(self):
        request = {
            "repository": "target/catalogue",
            "targetDirectory": "custom-dir",
            "catalogs": [{
                "sourceUrl": "https://github.com/source/app/tree/main/src",
                "catalog": CATALOG,
            }],
        }
        result = validate_request(request)
        self.assertEqual(
            "app/custom-dir/repo-metadata.json",
            result["catalogs"][0]["path"],
        )

    def test_validate_request_rejects_duplicate_target_paths(self):
        request = payload()
        request["catalogs"].append(dict(request["catalogs"][0]))
        with self.assertRaisesRegex(PublicationError, "Multiple catalogs map to"):
            validate_request(request)

    def test_publish_returns_unchanged_when_nothing_differs(self):
        existing = (json.dumps(CATALOG, indent=2) + "\n").encode("utf-8")
        client = FakeClient(existing)
        result = publish(payload(), client)
        self.assertEqual("unchanged", result["status"])
        self.assertEqual("", result["branchName"])
        self.assertEqual("", result["commitSha"])
        self.assertEqual("", result["pullRequestUrl"])
        self.assertEqual(0, result["pullRequestNumber"])
        self.assertFalse(any(call[0] == "POST" for call in client.calls))

    def test_publish_creates_one_commit_and_one_pull_request_when_content_differs(self):
        client = FakeClient()
        result = publish(payload(), client, branch_factory=lambda: "repo-metadata/test")
        self.assertEqual("created", result["status"])
        self.assertEqual(7, result["pullRequestNumber"])
        self.assertEqual("commit-sha", result["commitSha"])
        self.assertEqual("app/repo-metadata/repo-metadata.json", result["filesWritten"][0]["path"])
        self.assertEqual(1, len([call for call in client.calls if call[1].endswith("/pulls")]))
        self.assertEqual(1, len([call for call in client.calls if call[1].endswith("/git/commits") and call[0] == "POST"]))

    def test_publish_includes_manifest_file_in_the_same_tree_as_catalogs(self):
        request = payload()
        request["manifestFile"] = {"path": "manifest.json", "content": []}
        client = FakeClient()
        result = publish(request, client, branch_factory=lambda: "repo-metadata/manifest")
        self.assertEqual(2, len(result["filesWritten"]))
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
