# Service Dependency Manifest Orchestrator

Reads a shared public GitHub manifest, selects only `service-dependencies` nodes, compares branch-head commits, runs complete deferred service-dependency workflows, and publishes generated catalogs plus updated commit hashes in one pull request. Each workflow's C4-PlantUML diagram is forwarded to the publisher alongside its catalog when present.

```json
{"sourceUrl":"https://github.com/talentconsulting/atlas-dfe/blob/main/manifest.json"}
```

Manifest node:

```json
{
  "service-dependencies": {
    "path-to-scan": "tree/main/src",
    "last-commit-hash-scanned": ""
  }
}
```

```bash
python3 -m unittest discover -s src/service-dependency-manifest-orchestrator -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy service-dependency-manifest-orchestrator --no-prompt
```
