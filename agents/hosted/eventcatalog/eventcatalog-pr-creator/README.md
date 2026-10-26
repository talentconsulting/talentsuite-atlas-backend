# Event and Command Catalog PR Creator

Commits up to 100 validated catalogs plus an optional manifest update in one GitHub commit, then opens one pull request. Unchanged content creates neither a branch nor a pull request, and the agent never approves or merges PRs.

```json
{
  "repository": "owner/catalogue",
  "catalogs": [
    {
      "sourceUrl": "https://github.com/owner/application/tree/main/src/Application",
      "catalog": {
        "repository": "owner/application",
        "ref": "main",
        "path": "src/Application",
        "commands": [],
        "events": []
      },
      "targetPath": "application/event-catalog/events-and-commands.json"
    }
  ],
  "baseBranch": "main"
}
```

The hosted service mints its pull-request token from the Atlas GitHub App through the `atlas-github-app` Foundry custom-keys connection.

```bash
python3 -m unittest discover -s src/eventcatalog-pr-creator -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy eventcatalog-pr-creator --no-prompt
```
