import base64
import json
import unittest

from github_pr import PublicationError, parse_request, publish, render_markdown, validate_request


def page(page_id, auth=True, purpose=None, fields=()):
    return {"id": page_id, "kind": "mvc-action", "routes": [page_id.lower()], "methods": ["GET"], "sourceFile": "src/x.cs",
            "line": 1, "requiresAuthentication": auth, "isEntryPoint": False, "purpose": purpose, "fields": list(fields)}


CATALOG = {
    "repository": "source/app", "ref": "main", "path": "src", "webProjects": ["src/App.Web"],
    "pages": [page("Vacancy", auth=False, purpose="Shows a vacancy."), page("Apply"), page("Jobs", fields=[{"name": "HasJobs", "control": "radios", "label": "Do you have jobs?",
                                       "options": [{"value": "true", "label": "Yes"}, {"value": "false", "label": "No"}],
                                       "dataType": "bool?", "required": True, "constraints": ["max 5 entries"],
                                       "validation": "select an option", "line": 4, "sourceFile": "src/v.cshtml"}]), page("Skills"),
              page("Preview", purpose="Check answers | submit")],
    "transitions": [
        {"from": "Vacancy", "to": "Apply", "trigger": "link", "condition": None, "evidence": ["src/x.cs:1"]},
        {"from": "Apply", "to": "Jobs", "trigger": "link", "condition": None, "evidence": ["src/x.cs:2"]},
        {"from": "Apply", "to": "Skills", "trigger": "link", "condition": None, "evidence": ["src/x.cs:3"]},
        {"from": "Jobs", "to": "Apply", "trigger": "redirect", "condition": "after saving", "evidence": ["src/x.cs:4"]},
        {"from": "Apply", "to": "Preview", "trigger": "link", "condition": "when every section is complete", "evidence": ["src/x.cs:5"]},
    ],
    "siteNavigation": [], "unresolved": [], "selfLinkCount": 0,
    "journeys": [{"name": "Apply for a job", "description": "Fill in and check an application.", "kind": "hub",
                  "steps": ["Vacancy", "Apply"], "hub": "Apply", "sections": ["Jobs", "Skills"], "then": ["Preview"]}],
    "journeyWarnings": [],
}
JSON_PATH = "app/user-journeys/user-journeys.json"
MD_PATH = "app/user-journeys/user-journeys.md"


def payload():
    return {"repository": "target/catalogue",
            "catalogs": [{"sourceUrl": "https://github.com/source/app/tree/main/src", "catalog": CATALOG}]}


class FakeClient:
    def __init__(self, existing=None):
        self.existing = existing or {}   # path -> bytes
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path == "/repos/target/catalogue":
            return {"default_branch": "main"}
        if method == "GET" and "/contents/" in path:
            file_path = path.split("/contents/", 1)[1].split("?", 1)[0]
            if file_path not in self.existing:
                raise PublicationError("github_api_error", "GitHub API returned HTTP 404. Not Found")
            return {"type": "file", "content": base64.b64encode(self.existing[file_path]).decode("ascii")}
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

    def test_rejects_catalogs_without_the_user_journeys_fields(self):
        request = payload()
        request["catalogs"][0]["catalog"] = {"repository": "source/app"}
        with self.assertRaisesRegex(PublicationError, "must contain exactly"):
            validate_request(request)

    def test_writes_json_and_markdown_next_to_each_other(self):
        result = validate_request(payload())
        self.assertEqual([JSON_PATH, MD_PATH], [item["path"] for item in result["catalogs"]])

    def test_honours_target_directory_override(self):
        request = payload()
        request["targetDirectory"] = "journeys"
        self.assertEqual("app/journeys/user-journeys.json", validate_request(request)["catalogs"][0]["path"])

    def test_publish_returns_unchanged_when_nothing_differs(self):
        files = {item["path"]: item["content"] for item in validate_request(payload())["catalogs"]}
        client = FakeClient(files)
        result = publish(payload(), client)
        self.assertEqual("unchanged", result["status"])
        self.assertFalse(any(call[0] == "POST" for call in client.calls))

    def test_publish_creates_one_commit_with_both_files_and_the_manifest(self):
        request = payload()
        request["manifestFile"] = {"path": "manifest.json", "content": []}
        client = FakeClient()
        result = publish(request, client, branch_factory=lambda: "user-journeys/test")
        self.assertEqual("created", result["status"])
        self.assertEqual([JSON_PATH, MD_PATH, "manifest.json"], [item["path"] for item in result["filesWritten"]])
        tree_call = next(call for call in client.calls if call[1].endswith("/git/trees"))
        self.assertEqual(3, len(tree_call[2]["tree"]))
        self.assertEqual(1, len([call for call in client.calls if call[1].endswith("/pulls")]))


class MarkdownTests(unittest.TestCase):
    def setUp(self):
        self.markdown = render_markdown(CATALOG)

    def test_one_mermaid_flowchart_per_journey(self):
        self.assertIn("## Apply for a job", self.markdown)
        self.assertEqual(1, self.markdown.count("```mermaid"))

    def test_hub_journeys_draw_sections_with_dotted_returns_and_conditions(self):
        self.assertIn('-.->|"after saving"|', self.markdown)                 # Jobs back to the hub
        self.assertIn('-->|"when every section is complete"|', self.markdown)
        self.assertIn("Apply 🔒", self.markdown)
        self.assertNotIn("Vacancy 🔒", self.markdown)                         # anonymous page

    def test_form_fields_section(self):
        self.assertIn("## Form fields", self.markdown)
        self.assertIn("| `HasJobs` | radios | Do you have jobs? | Yes | max 5 entries; select an option | Yes, No |", self.markdown)

    def test_page_table_escapes_pipes(self):
        self.assertIn("Check answers \\| submit", self.markdown)
        self.assertIn("| `Vacancy` | `vacancy` | No | Shows a vacancy. |", self.markdown)


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
