# Local Dev Config Manifest Orchestrator

Reads a shared public GitHub manifest, selects only `local-dev-config` nodes, compares branch-head commits, runs complete deferred local-dev-config workflows, and publishes generated local service and configuration key catalogs plus updated commit hashes in one pull request. A repository whose workflow finds zero local services or configuration keys is a valid, successful outcome. Up to `LOCAL_DEV_CONFIG_MANIFEST_WORKFLOW_CONCURRENCY` workflows (default 4) run at once; results are still processed in manifest order, so the PR content and manifest hashes do not depend on which repository finishes first.

```json
{"sourceUrl":"https://github.com/talentconsulting/atlas-dfe/blob/main/manifest.json"}
```

Manifest node:

```json
{
  "local-dev-config": {
    "path-to-scan": "tree/main/src",
    "last-commit-hash-scanned": ""
  }
}
```

```bash
python3 -m unittest discover -s src/local-dev-config-manifest-orchestrator -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy local-dev-config-manifest-orchestrator --no-prompt
```
