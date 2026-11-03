# OpenAPI Spec Workflow

A hosted orchestration agent that calls `openapi-source-discovery`, invokes `openapi-spec-generator` once for every discovered API, and sends all successfully generated specifications to `openapi-spec-pr-creator` in one request.

## Input

```json
{
  "sourceUrl": "https://github.com/owner/application/tree/main/src/Api",
  "targetRepository": "owner/api-specifications",
  "targetDirectory": "application/open-api",
  "targetBaseBranch": "main",
  "pullRequestTitle": "Update generated OpenAPI specifications"
}
```

`sourceUrl` and `targetRepository` are required. By default, specifications are written directly beneath `<source-repository>/open-api` in the target repository; source directories are not reproduced. When two API files share a name, those files instead take their full relative path, joined with hyphens (for example `src-Api-CertificateController.openapi.json`), as the manifest orchestrator does. `targetDirectory` can override that directory; it and `targetBaseBranch`, `branchName`, `pullRequestTitle`, and `pullRequestBody` are forwarded to the PR creator.

Internal orchestrators can set `deferPublication: true`. In that mode only `sourceUrl` is required, the workflow returns its generated `specifications` array, and it does not call the PR creator. This allows a parent workflow to combine multiple repositories into one atomic pull request.

## Output

```json
{
  "success": true,
  "sourceUrl": "https://github.com/owner/application/tree/main/src/Api",
  "discoveredCount": 2,
  "generatedCount": 2,
  "generationErrors": [],
  "pullRequest": {
    "success": true,
    "status": "created",
    "pullRequestUrl": "https://github.com/owner/api-specifications/pull/12"
  }
}
```

Generation is concurrent but bounded. Result ordering follows discovery ordering. If individual generations fail, successfully generated documents are still published together and the workflow returns `success: false` with `generationErrors`. If discovery yields no APIs, the PR creator is not called.

Discovery and generation calls may be retried once for transient failures. The mutating PR-creation call is never retried automatically, avoiding duplicate branches or pull requests after an ambiguous timeout.

## Agent dependencies

Deploy these agents in this order before invoking the workflow:

1. `openapi-source-discovery`
2. `openapi-spec-generator`
3. `openapi-spec-pr-creator`
4. `openapi-spec-workflow`

The workflow's hosted identity needs permission to invoke agents in the Foundry project. The GitHub token belongs only to `openapi-spec-pr-creator`.

## Test and deploy

```bash
python3 -m unittest discover -s src/openapi-spec-workflow -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy openapi-spec-workflow --no-prompt
```

The root workflow `.github/workflows/deploy-openapi-spec-workflow.agent.yml` deploys this agent using the `dev` GitHub environment.
