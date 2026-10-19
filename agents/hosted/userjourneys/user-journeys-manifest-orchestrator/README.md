# User Journeys Manifest Orchestrator

Reads the shared catalogue `manifest.json`, finds entries with a `user-journeys` node whose branch head has changed since `last-commit-hash-scanned`, runs `user-journeys-workflow` (deferred) for each, and publishes every successful catalog plus the updated commit hashes through `user-journeys-pr-creator` in one pull request. Only those `user-journeys` nodes are applied to the manifest as it is when the pull request is opened (`manifestNodes`), so entries added during the run are kept.

Add the node only to web repositories:

```json
{
  "github-repo": "https://github.com/owner/app-web",
  "user-journeys": {"path-to-scan": "tree/main/src", "last-commit-hash-scanned": ""}
}
```

```json
{"sourceUrl":"https://github.com/owner/catalogue/blob/main/manifest.json"}
```

A repository whose workflow fails keeps its old hash, so the next run retries it.

```bash
python3 -m unittest discover -s src/user-journeys-manifest-orchestrator -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy user-journeys-manifest-orchestrator --no-prompt
```
