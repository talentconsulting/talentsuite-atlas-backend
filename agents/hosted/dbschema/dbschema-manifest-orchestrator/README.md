# Database Schema Manifest Orchestrator

A manually invokable hosted agent that reads a public GitHub JSON manifest, checks each source repository's branch-head commit, generates database schemas only for changed repositories, and creates one pull request in the manifest repository. The same PR contains all generated schemas and updates `last-commit-hash-scanned` for every successfully processed repository.

No schedule or Foundry routine is created initially. A routine can be attached later without changing this agent.

## Input

The input has exactly one property containing the full GitHub blob URL of the manifest:

```json
{
  "sourceUrl": "https://github.com/owner/spec-repository/blob/main/repoManifest.json"
}
```

The manifest must use this schema:

```json
[
  {
    "github-repo": "https://github.com/talentconsulting/talentsuite-bidmanager",
    "dbschema": {
      "path-to-scan": "tree/main/src/Data",
      "last-commit-hash-scanned": ""
    }
  }
]
```

`path-to-scan` supplies both the branch and path, unless the node names a `branch`, in which case it is just the folder within that branch (see "Scanning a branch other than the default" in the root README). The agent compares `last-commit-hash-scanned` with the current head of that branch. The manifest may be shared with the OpenAPI pipeline: unrelated nodes are ignored, and entries without `dbschema` are skipped. The legacy `db-schema` key remains supported for existing manifests, but an entry cannot contain both keys. Manifest and source reads are currently restricted to public, credential-free GitHub URLs.

## Behavior

For every changed manifest entry, the agent calls `dbschema-workflow` once with that entry's repository tree URL and `deferPublication: true`. Each workflow invokes `dbschema-generator`. Up to `DBSCHEMA_MANIFEST_WORKFLOW_CONCURRENCY` workflows (default 4) run at once; results are still processed in manifest order, so the PR content and manifest hashes do not depend on which repository finishes first. Complete results are combined and sent once to `dbschema-pr-creator`:

- Destination repository: the repository containing the manifest.
- Base branch: the ref in the manifest blob URL.
- Schema path: `<source-repository>/db-schema/database.schema.json` (one database representation per source repository).
- Manifest path: the original path from `sourceUrl`.

Each deferred workflow returns the database representation in its `schemas` array:

```json
{
  "database": {
    "name": "application",
    "engine": "PostgreSQL"
  },
  "tables": [
    {
      "name": "orders",
      "schema": "public",
      "entity": "Order",
      "columns": [
        {
          "name": "id",
          "type": "uuid",
          "nullable": false,
          "primaryKey": true,
          "generated": true,
          "default": null,
          "ordinal": 1
        }
      ],
      "relationships": [],
      "indexes": [],
      "hasPii": false
    }
  ],
  "types": []
}
```

The orchestrator assigns each successful response its deterministic `<repository>/db-schema/database.schema.json` target path. The publisher receives all schemas plus the updated `manifestFile`. Dependency names can be overridden with `DBSCHEMA_WORKFLOW_AGENT_NAME` and `DBSCHEMA_PR_CREATOR_AGENT_NAME`.

The `dbschema-workflow`, `dbschema-generator`, and `dbschema-pr-creator` agents are included in this repository.

Entries are marked scanned only when their full generation succeeds. A failed entry remains unchanged so it will be retried later. If every commit already matches, no branch or pull request is created.

## Manual invocation

```bash
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd ai agent invoke dbschema-manifest-orchestrator \
  '{"sourceUrl":"https://github.com/owner/spec-repository/blob/main/repoManifest.json"}'
```

## Test and deploy

```bash
python3 -m unittest discover -s src/dbschema-manifest-orchestrator -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy dbschema-manifest-orchestrator --no-prompt
```
