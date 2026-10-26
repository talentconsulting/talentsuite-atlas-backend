# Local Dev Config PR Creator

A hosted Foundry agent that commits validated local-development-configuration JSON catalogs and an optional updated manifest to one GitHub branch, then opens one pull request. It never approves or merges the pull request.

## Input

```json
{
  "repository": "owner/local-dev-config-catalogue",
  "catalogs": [
    {
      "sourceUrl": "https://github.com/owner/application/tree/main/src",
      "catalog": {
        "repository": "owner/application",
        "ref": "main",
        "path": "src",
        "localServices": [
          {
            "name": "PostgreSQL",
            "kind": "database",
            "technology": "postgresql",
            "configurationKeys": ["DATABASE_URL"],
            "evidence": [{"sourceFile": "src/docker-compose.yml", "reason": "postgres service defined"}]
          }
        ],
        "configurationKeys": [
          {"key": "DATABASE_URL", "sourceFile": "src/.env.example", "reason": "PostgreSQL connection string"}
        ]
      },
      "targetPath": "application/local-dev-config/local-dev-config.json"
    }
  ],
  "baseBranch": "main",
  "manifestFile": {"path": "manifest.json", "content": []}
}
```

`repository` and a non-empty `catalogs` array are required. Every catalog requires its original GitHub tree `sourceUrl`, the generated `catalog` (with `repository`, `ref`, `path`, `localServices`, and `configurationKeys`), and a safe JSON `targetPath`. When `targetPath` is omitted it defaults to `{sourceRepoName}/{targetDirectory or "local-dev-config"}/local-dev-config.json` — the source repository name comes first, then the target directory. Optional fields are `targetDirectory`, `baseBranch`, `branchName`, `pullRequestTitle`, `pullRequestBody`, and `manifestFile`.

`manifestNodes` (optional, requires `manifestFile`) names the manifest nodes this flow owns, for example `["local-dev-config"]`. When it is set, only those nodes are copied from `manifestFile.content` into the manifest as it is on the base branch when the pull request is opened: entries added or removed since the run started, and other flows' nodes, are left as they are, and a node whose `path-to-scan` has changed is skipped so it is rescanned. Without it, `manifestFile.content` is written as-is.

The agent accepts at most 100 catalogs and 10 MiB of generated JSON per request. Unchanged files do not create a branch or pull request.

## GitHub connection

The agent opens pull requests with a short-lived token it mints from the Atlas GitHub App, through the Foundry **Custom keys** connection `atlas-github-app` (fields `app_id` and `private_key`, mapped to `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY`). The token is limited to the tenant's data repository, with contents and pull-request write access. See [Tenants and the Atlas GitHub App](../../../../DEPLOYMENT.md#tenants-and-the-atlas-github-app).

## Test and deploy

```bash
python3 -m unittest discover -s src/local-dev-config-pr-creator -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy local-dev-config-pr-creator --no-prompt
```

The root workflow `.github/workflows/deploy-local-dev-config-pr-creator.agent.yml` performs the same deployment using the `dev` GitHub environment. The `atlas-github-app` connection must already exist in the target Foundry project.
