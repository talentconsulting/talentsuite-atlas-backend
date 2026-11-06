# OpenAPI Manifest Orchestrator

A manually invokable hosted agent that reads a public GitHub JSON manifest, checks each source repository's branch-head commit, generates specifications only for changed repositories, and creates one pull request in the manifest repository. The same PR contains all generated specs and updates `last-commit-hash-scanned` for every successfully processed repository.

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
    "specs": {
      "path-to-scan": "tree/main/src/TalentSuite.Server",
      "last-commit-hash-scanned": ""
    }
  }
]
```

`path-to-scan` supplies both the branch and path, unless the node names a `branch`, in which case it is just the folder within that branch (see "Scanning a branch other than the default" in the root README). The agent compares `last-commit-hash-scanned` with the current head of that branch. The manifest may be shared with other pipelines: unrelated nodes are ignored, and entries without `specs` are skipped. Manifest and source reads are currently restricted to public, credential-free GitHub URLs.

## Behavior

For every changed manifest entry, the agent calls `openapi-spec-workflow` with deferred publication. Up to `OPENAPI_MANIFEST_WORKFLOW_CONCURRENCY` repositories (default 4) are discovered and generated at once; results are still processed in manifest order, so the PR content and manifest hashes do not depend on which repository finishes first. Complete results are combined and sent once to `openapi-spec-pr-creator`:

- Destination repository: the repository containing the manifest.
- Base branch: the ref in the manifest blob URL.
- Spec path: `<source-repository>/open-api/<api-filename>.openapi.json` (flat; source directories are not reproduced).
- Manifest path: the original path from `sourceUrl`.

Entries are marked scanned only when their full generation succeeds. A failed entry remains unchanged so it will be retried later. If every commit already matches, no branch or pull request is created.

## Manual invocation

```bash
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd ai agent invoke openapi-manifest-orchestrator \
  '{"sourceUrl":"https://github.com/owner/spec-repository/blob/main/repoManifest.json"}'
```

## Test and deploy

```bash
python3 -m unittest discover -s src/openapi-manifest-orchestrator -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy openapi-manifest-orchestrator --no-prompt
```
