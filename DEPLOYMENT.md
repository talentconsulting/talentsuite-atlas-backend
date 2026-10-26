# Deployment

The OpenAPI pipeline uses an existing Azure AI Foundry project and five hosted agents. Deploy them in this order:

1. `openapi-source-discovery`
2. `openapi-spec-generator`
3. `openapi-spec-pr-creator`
4. `openapi-spec-workflow`
5. `openapi-manifest-orchestrator` (optional manifest entry point)

## GitHub Actions configuration

Create a GitHub environment named `dev` with these secrets:

| Secret | Used by | Purpose |
| --- | --- | --- |
| `AZURE_CLIENT_ID` | All deployments | Entra application or managed identity client ID for GitHub OIDC. |
| `AZURE_TENANT_ID` | All deployments | Azure tenant ID. |
| `AZURE_SUBSCRIPTION_ID` | All deployments | Azure subscription ID. |
| `AZURE_AI_PROJECT_ENDPOINT` | All deployments | Existing Foundry project endpoint. |

The Azure identity needs deployment access to the Foundry project. The hosted workflow identity needs permission to invoke the other agents in that project.

Create a Foundry Custom keys project connection named `openapi-pr-github` with a write-only `github_token` field. Grant that fine-grained token access only to the destination specification repository, with:

- Metadata: read
- Contents: read and write
- Pull requests: read and write

The PR creator resolves the token from `${{connections.openapi-pr-github.credentials.github_token}}` when its sandbox starts. It is not stored in the agent definition, passed to the orchestration agent, or accepted in an agent request.

The database-schema PR creator reuses `openapi-pr-github`, because it publishes to the same catalogue repository with the same permissions. No second GitHub secret or Foundry connection is required.

### Source-repository read connection

Each `*-source-discovery` agent (`openapi`, `dbschema`, `eventcatalog`, `service-dependency`, `local-dev-config`, `repo-metadata`) downloads a GitHub archive of whatever repository `sourceUrl` points at, which can be any repository the pipeline is asked to analyze -- not just the destination catalogue repository. These agents previously reused `openapi-pr-github` for `GITHUB_READ_TOKEN`, but that connection is a fine-grained PAT deliberately scoped to write access on the destination catalogue repository only. Presenting it to an unrelated source repository doesn't fail closed with a clear permissions error -- GitHub returns a plain `404` for a token with no grant on that repository, which is indistinguishable from the repository not existing, even when it's public.

Create a second Foundry Custom keys project connection named `github-source-read` with a `github_token` field holding a token that can read whatever source repositories the pipeline will be pointed at:

- A classic PAT with the `public_repo` scope covers any public repository and raises the rate limit well above the 60 requests/hour anonymous limit.
- If source repositories are private, use a fine-grained PAT with read-only `Contents` and `Metadata` access granted across the relevant organization(s)/repositories instead.

Each `*-source-discovery` agent resolves `GITHUB_READ_TOKEN` from `${{connections.github-source-read.credentials.github_token}}`. `openapi-pr-github` remains scoped to writes on the destination catalogue repository only; nothing in this pipeline should reuse it for reading arbitrary source repositories.

Note: the `*-manifest-orchestrator` agents also read `GITHUB_READ_TOKEN` (currently still `openapi-pr-github`), but only to fetch `manifest.json` from the same destination catalogue repository that token is already scoped to -- that usage is correctly scoped and doesn't need `github-source-read`. However, those orchestrators separately check each manifest entry's *source* repository branch-head commit via the GitHub API using the same token, which has the identical scoping problem described above; that call site is not yet updated to use `github-source-read`.

### Tenants and the Atlas GitHub App

Each tenant is identified by its **data repository**: the repository that holds its `manifest.json` and generated catalogues (for DfE, `talentconsulting/talentsuite-atlas`). The manifest orchestrators take the tenant from the manifest URL and pass it down as `tenant` to the agents that read GitHub. When the Atlas GitHub App is configured, every agent mints its own short-lived installation token for that tenant:

- Reads (manifest, source commits, source archives): a read-only token (`contents`, `metadata`) for the App installation covering the tenant's data repository. It can read that organisation's private repositories the App is installed on, and any public repository. It cannot read another tenant's private repositories.
- Pull requests: a token limited to the tenant's data repository (`contents` and `pull_requests` write).

Tokens are created inside the agents and never appear in a request, so they are not stored in Foundry conversation history.

To set it up:

1. Create a GitHub App (for example "TalentSuite Atlas") owned by the organisation that runs the pipeline. Turn off webhooks. Repository permissions: **Contents** read and write, **Pull requests** read and write, **Metadata** read. Generate a private key.
2. Install the App on each tenant's organisation, choosing their data repository plus any private source repositories.
3. Create a Foundry Custom keys project connection named `atlas-github-app` with two fields: `app_id` (the App ID) and `private_key` (the PEM contents; literal `\n` sequences are accepted).
4. Redeploy the agents. Each one that reads or writes GitHub resolves `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY` from `atlas-github-app`.

The connection must exist before the agents are deployed, because their definitions reference it. Until the App is configured, agents fall back to `GITHUB_READ_TOKEN` (`github-source-read`) and `GITHUB_PR_TOKEN` (`openapi-pr-github`). Once runs succeed with the App, remove those environment variables from each `azure.yaml`, redeploy, and then delete the two connections.

Optional `dev` environment variables are:

| Variable | Default |
| --- | --- |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | `gpt-4o` |
| `AZURE_LOCATION` | `uksouth` |
| `AZURE_RESOURCE_GROUP` | `talent-day-rg` |
| `AZURE_AI_ACCOUNT_NAME` | `talent-day-foundry` |
| `AZURE_AI_PROJECT_NAME` | `talent-day-proj-default` |

## Deployment workflows

The following workflows run on changes to their agent on the default branch and support manual dispatch:

- `.github/workflows/deploy-openapi-source-discovery.agent.yml`
- `.github/workflows/deploy-openapi-spec-generator.agent.yml`
- `.github/workflows/deploy-openapi-spec-pr-creator.agent.yml`
- `.github/workflows/deploy-openapi-spec-workflow.agent.yml`
- `.github/workflows/deploy-openapi-manifest-orchestrator.agent.yml`
- `.github/workflows/deploy-dbschema-manifest-orchestrator.agent.yml`
- `.github/workflows/deploy-dbschema-generator.agent.yml`
- `.github/workflows/deploy-dbschema-pr-creator.agent.yml`
- `.github/workflows/deploy-dbschema-workflow.agent.yml`
- `.github/workflows/deploy-system-summary-generator.agent.yml`
- `.github/workflows/deploy-system-summary-pr-creator.agent.yml`
- `.github/workflows/deploy-system-summary-manifest-orchestrator.agent.yml`

The PR creator and workflow smoke tests use invalid input deliberately. This verifies the hosted Responses endpoint without writing to a repository during deployment.

## Local deployment

Authenticate Azure CLI and azd, configure each agent project's azd environment with the existing project endpoint, create the `openapi-pr-github` connection, and deploy in dependency order:

```bash
cd agents/hosted/openapi/openapi-spec-pr-creator
azd deploy openapi-spec-pr-creator --no-prompt
```

Then deploy the orchestrator:

```bash
cd ../openapi-spec-workflow
azd deploy openapi-spec-workflow --no-prompt
```

No destination repository is fixed at deployment time. Supply it as `targetRepository` whenever the workflow is invoked.

`openapi-manifest-orchestrator` instead derives its destination from the repository containing its manifest. No routine or schedule is deployed initially; invoke the hosted agent directly while testing.

Deploy the database-schema agents in this order: `dbschema-generator`, `dbschema-pr-creator`, `dbschema-workflow`, then `dbschema-manifest-orchestrator`. Their validation smoke tests do not read GitHub, invoke the model, or change repositories.
