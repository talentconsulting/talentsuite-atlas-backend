# OpenAPI Spec PR Creator

A Python hosted agent that accepts generated OpenAPI specifications, writes them as deterministic JSON files in a configurable GitHub repository, and opens a pull request. It never approves or merges the pull request.

## Input

```json
{
  "repository": "owner/api-specifications",
  "specifications": [
    {
      "apiFile": "https://github.com/owner/source/blob/main/src/Api/BidsController.cs",
      "specification": {
        "openapi": "3.1.0",
        "info": {"title": "Bids API", "version": "1.0.0"},
        "paths": {},
        "components": {"schemas": {}}
      }
    }
  ],
  "targetDirectory": "openapi",
  "baseBranch": "main",
  "pullRequestTitle": "Update generated OpenAPI specifications"
}
```

`repository` and `specifications` are required. `repository` can be `owner/repository` or an HTTPS GitHub repository URL. The other properties are optional:

- `targetDirectory` defaults to `openapi`.
- `baseBranch` defaults to the destination repository's default branch.
- `branchName` defaults to a unique `openapi-specs/...` branch.
- `pullRequestTitle` and `pullRequestBody` have generated defaults.
- Each specification may provide `targetPath` to override its deterministic default path.
- `manifestFile` can provide one JSON `path` and JSON-serializable `content`; it is committed atomically with the specifications.
- `manifestNodes` (optional, requires `manifestFile`) names the manifest nodes this flow owns, for example `["local-dev-config"]`. When it is set, only those nodes are copied from `manifestFile.content` into the manifest as it is on the base branch when the pull request is opened: entries added or removed since the run started, and other flows' nodes, are left as they are, and a node whose `path-to-scan` has changed is skipped so it is rescanned. Without it, `manifestFile.content` is written as-is.

For each source API, the agent writes the API filename directly beneath `targetDirectory` and changes the extension to `.openapi.json`. Source directories are not reproduced. Duplicate API filenames are rejected to prevent accidental overwrites.

## Output

The agent always returns JSON. A successful change includes the created pull request; identical files return `status: "unchanged"` without creating a branch or pull request.

```json
{
  "success": true,
  "status": "created",
  "repository": "owner/api-specifications",
  "branchName": "openapi-specs/20260803-120000-abc12345",
  "commitSha": "0123456789abcdef0123456789abcdef01234567",
  "pullRequestUrl": "https://github.com/owner/api-specifications/pull/12",
  "pullRequestNumber": 12,
  "filesWritten": [{"path": "openapi/BidsController.openapi.json", "action": "created"}],
  "errors": []
}
```

## Authentication

The agent opens pull requests with a short-lived token it mints from the Atlas GitHub App, through the Foundry **Custom keys** connection `atlas-github-app` (fields `app_id` and `private_key`, mapped to `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY`). The token is limited to the tenant's data repository, with contents and pull-request write access. See [Tenants and the Atlas GitHub App](../../../../DEPLOYMENT.md#tenants-and-the-atlas-github-app).

The token is not accepted in the request body and is never returned. Ordinary azd environment substitution must not be used for the deployed token.

## Test and deploy

```bash
python3 -m unittest discover -s src/openapi-spec-pr-creator -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy openapi-spec-pr-creator --no-prompt
```

The root workflow `.github/workflows/deploy-openapi-spec-pr-creator.agent.yml` performs the same deployment using the `dev` GitHub environment. The `atlas-github-app` connection must already exist in the target Foundry project.
