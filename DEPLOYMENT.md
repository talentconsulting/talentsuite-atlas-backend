# Deployment

The agents run in an Azure AI Foundry project, which [Foundry infrastructure](#foundry-infrastructure) describes how to create. The OpenAPI pipeline has five hosted agents. Deploy them in this order:

1. `openapi-source-discovery`
2. `openapi-spec-generator`
3. `openapi-spec-pr-creator`
4. `openapi-spec-workflow`
5. `openapi-manifest-orchestrator` (optional manifest entry point)

## Foundry infrastructure

[`infra/main.bicep`](infra/main.bicep) declares the Foundry resources the agents run in:

- the Foundry (AI Services) account and its project
- the `gpt-4o` and `gpt-4.1` model deployments the agents call
- a Log Analytics workspace and Application Insights, connected to the project
- the `atlas-github-app` project connection, when the App ID and private key are supplied
- the usage ledger: a storage account with a `usage` table, and the `atlas-usage-ledger` project connection the agents write to it through (see [Usage and billing](#usage-and-billing))
- a monthly cost budget with email alerts, when `budgetAmount` and `budgetContactEmails` are set
- the deploy identity's roles: **Foundry Project Manager** on the account and **Azure AI Developer** on the resource group

It deploys into an existing resource group in incremental mode. Anything it doesn't declare is left alone, including other model deployments on the account and any other project connections.

[`infra/dev.bicepparam`](infra/dev.bicepparam) describes the current environment in `talent-day-rg`. A what-if against it reports no real changes; the only differences listed are values the Foundry service manages itself (current capacity, endpoints, the default project). Its roles were assigned by hand under different names, so it sets `createRoleAssignments = false`; Azure would reject a second assignment of the same role.

Run **Provision Foundry Infrastructure** from the Actions tab. `what-if` (the default) shows what would change and changes nothing; `deploy` applies it. Its `environment` input picks the GitHub environment, the parameters file (`infra/<environment>.bicepparam`) and the resource group (that environment's `AZURE_RESOURCE_GROUP` variable, defaulting to `talent-day-rg`).

The provisioning identity needs **Contributor** on the resource group, plus **Role Based Access Control Administrator** when `createRoleAssignments` is on. The agent deploy identity (`AZURE_CLIENT_ID`) doesn't have either. Grant them to it, or set the `AZURE_PROVISION_CLIENT_ID` secret in the `dev` environment to a separate app registration with a federated credential for this repository's `dev` environment.

Optional settings for the workflow, in each GitHub environment:

| Name | Kind | Used for |
| --- | --- | --- |
| `AZURE_PROVISION_CLIENT_ID` | Secret | A separate identity for provisioning. Defaults to `AZURE_CLIENT_ID`. |
| `DEPLOY_PRINCIPAL_ID` | Variable | Object ID of the agent deploy identity, for its role assignments. |
| `ATLAS_GITHUB_APP_ID` | Secret | Creates the `atlas-github-app` connection, with the private key. |
| `ATLAS_GITHUB_APP_PRIVATE_KEY` | Secret | The App's PEM private key. |

### What to create before provisioning

Every deploy and run workflow has an `environment` input (`dev` or `test`, defaulting to `dev`) that picks the GitHub environment it runs in, and so the Azure identity, project and settings it uses. To add another environment, also add its name to the `environment` options in each workflow.

The workflow can't create the resource group, the identity it signs in with, or that identity's roles, so these are set up by hand once per environment.

In Azure:

1. Create the resource group: `az group create --name <group> --location uksouth`. The template deploys into an existing group.
2. Create an app registration for GitHub Actions with a federated credential for this repository's environment. The subject is `repo:talentconsulting@80040422/talentsuite-atlas-backend@1281435335:environment:<environment>`; the `Sign in to Azure` step of a workflow run prints it as the subject claim. This step isn't templated, because it needs Entra ID admin rights rather than Azure resource rights.
3. Grant the app's service principal roles on the resource group:

   ```bash
   az role assignment create --assignee <app-client-id> --role Contributor \
     --scope /subscriptions/<subscription-id>/resourceGroups/<group>
   az role assignment create --assignee <app-client-id> --role "Role Based Access Control Administrator" \
     --scope /subscriptions/<subscription-id>/resourceGroups/<group>
   ```

   **Contributor** creates the Foundry account, project, model deployments, Log Analytics and Application Insights. **Role Based Access Control Administrator** is needed only when `createRoleAssignments` is on, so the template can give the identity its Foundry Project Manager and Azure AI Developer roles.
4. Check the subscription can host the resources:
   - The `Microsoft.CognitiveServices`, `Microsoft.Insights` and `Microsoft.OperationalInsights` resource providers are registered (`az provider show --namespace <provider> --query registrationState`). Contributor on a resource group can't register them.
   - There's enough GlobalStandard quota in the region for the model deployments in `modelDeployments` (`az cognitiveservices usage list --location <region>`). A deployment fails if the quota is short.

In GitHub, create a repository environment with the environment's name, and in it:

1. Set the `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_SUBSCRIPTION_ID` secrets for the app registration.
2. Set the `DEPLOY_PRINCIPAL_ID` variable to the service principal's object ID (`az ad sp show --id <app-client-id> --query id`), not its client ID.
3. Set the `ATLAS_GITHUB_APP_ID` and `ATLAS_GITHUB_APP_PRIVATE_KEY` secrets so the template creates the `atlas-github-app` connection. Every agent that reads or writes GitHub references it, and an agent fails to deploy if a connection it references is missing. See [Tenants and the Atlas GitHub App](#tenants-and-the-atlas-github-app).

### Provisioning and deploying the agents

1. Copy `infra/test.bicepparam` to `infra/<environment>.bicepparam` with new, globally unique names.
2. Run **Provision Foundry Infrastructure** for the environment with `what-if`, check the preview, then run it with `deploy`.
3. Set the `AZURE_AI_PROJECT_ENDPOINT` secret to the project endpoint in the run summary.
4. Set the `AZURE_RESOURCE_GROUP`, `AZURE_AI_ACCOUNT_NAME`, `AZURE_AI_PROJECT_NAME` and `AZURE_LOCATION` variables when they differ from the defaults in [GitHub Actions configuration](#github-actions-configuration). The agent workflows don't read them from the parameters file.
5. Check the run created the `atlas-github-app` connection. If the App secrets weren't set, set them and run it again.
6. Deploy the agents with their `deploy-*.agent.yml` workflows, choosing the environment.

### The test environment

`test` was created this way in `atlas-test-rg` (UK South), with the "Atlas test deployments" app registration and `infra/test.bicepparam`. Its Role Based Access Control Administrator assignment has a condition that lets it assign or remove only the three roles the template uses (Foundry Project Manager, Azure AI Developer and Reader).

### The dev environment

The resource group, app registration ("Azure AI deployments"), roles, `atlas-github-app` connection and secrets for `talent-day-rg` already exist. The one thing missing for provisioning is **Contributor** on `talent-day-rg` for that app's service principal: its existing roles (Azure AI Developer and Foundry Project Manager) can't read the resource group or run deployments, so the workflow fails at "Check the resource group exists". `dev.bicepparam` turns role assignments off, so it doesn't need Role Based Access Control Administrator.

## Usage and billing

Tenants are charged for the model tokens their runs use, at cost plus a markup. Each environment keeps its own ledger.

**Recording usage.** Every generator agent that calls a model adds one row to the environment's `usage` table per call: the month (the partition), the tenant, the agent, the model deployment and the input, cached input and output token counts. The tenant is the `tenant` in the agent's request, the owner/repo data repository. A request without one is recorded as `unassigned`. The manifest orchestrators and **Run All Flows Against A Repository** pass the tenant. `system-summary-generator` doesn't take a tenant yet, so its usage is `unassigned`.

The agents write through `USAGE_LEDGER_URL`, which each generator's `azure.yaml` maps from the `atlas-usage-ledger` connection. That URL holds a SAS that can only add rows, so an agent can't read, change or delete usage. Provisioning issues a new one, valid for a year, on every run; run **Provision Foundry Infrastructure** at least once a year. A row that can't be written is logged as a warning and the agent carries on, so a ledger problem never fails a run but can under-bill. The shared code is `usage_ledger.py`, kept identical in every generator.

**Invoicing.** **Usage Billing Report** runs on the 2nd of each month for `dev` and `test` and prices the previous month. Run it by hand to choose the environment and month. It reads the ledger with a one-hour read-only SAS and uploads `invoice.csv` (one line per tenant), `detail.csv` (per tenant and model), `summary.md`, which also appears in the run summary with a charge-by-tenant chart, and `usage.html`, a chart page for the month: headline totals, the daily charge stacked by tenant (hover a day for its breakdown), the charge per tenant and the cost per model. Download the `billing-report-<environment>` artifact and open `usage.html` in a browser; it needs no network access. [`scripts/billing_report.py`](scripts/billing_report.py) does the pricing.

Prices come from [`billing/rates.json`](billing/rates.json): a rate per million input, cached input and output tokens for each model deployment name, a currency and a `markup` (0.25 charges cost plus 25%). The rates start as Azure OpenAI Global Standard list prices in US dollars. Check them against your Azure agreement and billing currency, and set the markup, before invoicing. A model with no rate stops the report rather than pricing it at zero, so add a rate before deploying a new model.

**Azure costs.** Each environment's resource group has a monthly budget (`budgetAmount`, in the subscription's billing currency) that emails `budgetContactEmails` at 80% and 100% of actual spend and when spend is forecast to pass 100%. `budgetStartDate` is the budget's first month; Azure rejects a change to it, so leave it fixed. Compare a month's invoice total with the resource group's cost in Cost Management to see the margin.

## GitHub Actions configuration

Create a GitHub environment named `dev` with these secrets:

| Secret | Used by | Purpose |
| --- | --- | --- |
| `AZURE_CLIENT_ID` | All deployments | Entra application or managed identity client ID for GitHub OIDC. |
| `AZURE_TENANT_ID` | All deployments | Azure tenant ID. |
| `AZURE_SUBSCRIPTION_ID` | All deployments | Azure subscription ID. |
| `AZURE_AI_PROJECT_ENDPOINT` | All deployments | Existing Foundry project endpoint. |

The Azure identity needs deployment access to the Foundry project. The hosted workflow identity needs permission to invoke the other agents in that project.

The agents authenticate to GitHub only through the Atlas GitHub App; see below.

### Tenants and the Atlas GitHub App

Each tenant is identified by its **data repository**: the repository that holds its `manifest.json` and generated catalogues (for DfE, `talentconsulting/talentsuite-atlas`). The manifest orchestrators take the tenant from the manifest URL and pass it down as `tenant` to the agents that read GitHub. When the Atlas GitHub App is configured, every agent mints its own short-lived installation token for that tenant:

- Reads (manifest, source commits, source archives): a read-only token (`contents`, `metadata`) for the App installation covering the tenant's data repository. It can read that organisation's private repositories the App is installed on, and any public repository. It cannot read another tenant's private repositories.
- Pull requests: a token limited to the tenant's data repository (`contents` and `pull_requests` write).

Tokens are created inside the agents and never appear in a request, so they are not stored in Foundry conversation history.

To set it up:

1. Create a GitHub App (for example "TalentSuite Atlas") owned by the organisation that runs the pipeline. Turn off webhooks. Repository permissions: **Contents** read and write, **Pull requests** read and write, **Metadata** read. Generate a private key.
2. Install the App on each tenant's organisation, choosing their data repository plus any private source repositories.
3. Set the `ATLAS_GITHUB_APP_ID` and `ATLAS_GITHUB_APP_PRIVATE_KEY` secrets in the GitHub environment and run **Provision Foundry Infrastructure**, which creates the Foundry Custom keys project connection `atlas-github-app` with two fields: `app_id` (the App ID) and `private_key` (the PEM contents; literal `\n` sequences are accepted).
4. Redeploy the agents. Each one that reads or writes GitHub resolves `GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY` from `atlas-github-app`.

The connection must exist before the agents are deployed, because their definitions reference it. A request without a `tenant` reads public repositories anonymously, which GitHub limits to 60 requests an hour, and can't open pull requests.

The agents used to fall back to personal access tokens in the `openapi-pr-github` and `github-source-read` connections. Their definitions no longer reference them, so once every agent in an environment has been redeployed those connections can be deleted.

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

Authenticate Azure CLI and azd, configure each agent project's azd environment with the existing project endpoint, make sure the `atlas-github-app` connection exists, and deploy in dependency order:

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
