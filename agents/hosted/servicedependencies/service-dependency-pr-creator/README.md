# Service Dependency PR Creator

Commits up to 100 validated service-dependency catalogs plus an optional manifest update in one GitHub commit, then opens one pull request. Unchanged content creates neither a branch nor a pull request. The hosted service mints its pull-request token from the Atlas GitHub App through the `atlas-github-app` Foundry custom-keys connection.

When a catalog item includes an optional `puml` string, it is written alongside the JSON at the same path with a `.puml` extension (for example `app/service-dependencies/service-dependencies.json` and `app/service-dependencies/service-dependencies.puml`).

```bash
python3 -m unittest discover -s src/service-dependency-pr-creator -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy service-dependency-pr-creator --no-prompt
```
