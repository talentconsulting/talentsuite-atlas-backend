# User Journeys PR Creator

Publishes user-journey catalogs to the catalogue repository in one commit and one pull request. For each catalog it writes:

- `<repo>/user-journeys/user-journeys.json`: the catalog as generated.
- `<repo>/user-journeys/user-journeys.md`: each journey as a Mermaid flowchart (GitHub renders these), and a table of pages with route, sign-in and purpose.

```json
{
  "repository": "owner/catalogue",
  "catalogs": [{"sourceUrl": "https://github.com/owner/app/tree/main/src", "catalog": {"repository": "owner/app", "ref": "main", "path": "src", "webProjects": [], "pages": [], "transitions": [], "siteNavigation": [], "unresolved": [], "selfLinkCount": 0, "journeys": [], "journeyWarnings": []}}],
  "manifestFile": {"path": "manifest.json", "content": []},
  "manifestNodes": ["user-journeys"]
}
```

`repository` and a non-empty `catalogs` array are required. Optional fields are `targetDirectory` (default `user-journeys`), `baseBranch`, `branchName`, `pullRequestTitle`, `pullRequestBody`, `manifestFile` and `manifestNodes`.

`manifestNodes` (optional, requires `manifestFile`) names the manifest nodes this flow owns. When it is set, only those nodes are copied from `manifestFile.content` into the manifest as it is on the base branch when the pull request is opened: entries added or removed since the run started, and other flows' nodes, are left as they are, and a node whose `path-to-scan` has changed is skipped so it is rescanned. Without it, `manifestFile.content` is written as-is.

```bash
python3 -m unittest discover -s src/user-journeys-pr-creator -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy user-journeys-pr-creator --no-prompt
```
