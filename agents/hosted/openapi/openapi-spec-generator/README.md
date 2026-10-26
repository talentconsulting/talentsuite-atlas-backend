# OpenAPI Spec Generator

A Python hosted agent that consumes one element returned by OpenAPI Source Discovery, reads the referenced API and DTO source files, and returns one complete OpenAPI 3.1 document as JSON.

## Input

The input must be a JSON object with exactly these properties:

```json
{
  "apiFile": "https://github.com/owner/repository/blob/main/src/Api/Controllers/BidsController.cs",
  "supportingFiles": [
    "https://github.com/owner/repository/blob/main/src/Contracts/BidResponse.cs",
    "https://github.com/owner/repository/blob/main/src/Contracts/CreateBidRequest.cs"
  ]
}
```

Every URL must be a credential-free HTTPS GitHub blob URL from the same repository and ref. The agent reads only the supplied files; it does not scan the repository for additional context.

An optional `tenant` (the tenant's `owner/repo` data repository) may also be supplied. When the GitHub App is configured (`GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`), the files are read through the GitHub contents API with that tenant's read-only installation token, so private repositories work; otherwise they are read anonymously from raw.githubusercontent.com (or with `GITHUB_READ_TOKEN`, if set).

## Output

The response is the OpenAPI document itself, not a wrapper and not Markdown:

```json
{
  "openapi": "3.1.0",
  "info": {
    "title": "Bids API",
    "version": "1.0.0"
  },
  "paths": {},
  "components": {
    "schemas": {}
  }
}
```

The top-level fields are returned in a deterministic presentation order: `openapi`, `info`, `paths`, any additional OpenAPI sections, and `components` last.

Generation failures remain valid JSON and use an `error` object instead of an incomplete specification.

## Configuration

The hosted process requires:

- `FOUNDRY_PROJECT_ENDPOINT`
- `AZURE_AI_MODEL_DEPLOYMENT_NAME`

The current shared Foundry environment uses the existing `gpt-4o` deployment.

## Test

```bash
python3 -m unittest discover -s src/openapi-spec-generator -p 'test_*.py'
```

## Local run

Create `src/openapi-spec-generator/.env` from `.env.example`, then create the virtual environment beside `requirements.txt`:

```bash
cd src/openapi-spec-generator
python3 -m venv .venv
source .venv/bin/activate
python -m pip install uv
cd ../..
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd ai agent run openapi-spec-generator --no-client
```

Invoke it from a second terminal:

```bash
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd ai agent invoke openapi-spec-generator --local \
  '{"apiFile":"https://github.com/owner/repository/blob/main/src/Api/Controllers/BidsController.cs","supportingFiles":[]}'
```

## Deploy

```bash
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy openapi-spec-generator --no-prompt
```

## GitHub deployment workflow

The root workflow `.github/workflows/deploy-openapi-spec-generator.agent.yml` runs the unit tests, deploys a new hosted-agent version, verifies its status, and invokes it with an OpenAPI JSON contract test. It runs when this agent changes on the repository's default branch and can also be started manually.

Create a GitHub environment named `dev` and add these environment secrets:

- `AZURE_CLIENT_ID`: client ID of the Entra application or managed identity used by GitHub OIDC
- `AZURE_TENANT_ID`: Azure tenant ID
- `AZURE_SUBSCRIPTION_ID`: Azure subscription ID
- `AZURE_AI_PROJECT_ENDPOINT`: full endpoint of the existing Foundry project

The OIDC identity needs `Contributor` and `Foundry User` access on the target Foundry project. Its federated credential must trust the `dev` GitHub environment.

These optional GitHub environment variables override the workflow defaults:

- `AZURE_AI_MODEL_DEPLOYMENT_NAME` (default `gpt-4o`)
- `AZURE_LOCATION` (default `uksouth`)
- `AZURE_RESOURCE_GROUP` (default `talent-day-rg`)
- `AZURE_AI_ACCOUNT_NAME` (default `talent-day-foundry`)
- `AZURE_AI_PROJECT_NAME` (default `talent-day-proj-default`)
- `OPENAPI_SPEC_GENERATOR_TEST_API_FILE`
- `OPENAPI_SPEC_GENERATOR_TEST_SUPPORTING_FILE_1`
- `OPENAPI_SPEC_GENERATOR_TEST_SUPPORTING_FILE_2`
