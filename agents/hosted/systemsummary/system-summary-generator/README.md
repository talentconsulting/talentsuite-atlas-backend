# System Summary Generator

Reads a repository's already-published `db-schema`, `event-catalog`, and `service-dependencies` catalogs plus its OpenAPI controller names, and returns a factual, evidence-grounded summary of what the system does. Unlike the `dbschema`/`eventcatalog`/`service-dependency` generators, this agent never scans raw source — its input is already-structured catalog JSON supplied by the caller.

```json
{
  "repository": "owner/repository",
  "database": {"tables": [{"name": "Orders"}]},
  "events": {"commands": [{"name": "CreateOrder"}], "events": []},
  "dependencies": {"dependencies": [{"name": "AccountsApi", "kind": "http-api"}]},
  "apiControllers": ["OrdersController"]
}
```

Any of `database`, `events`, or `dependencies` may be `null` when that catalog does not exist for the repository; `apiControllers` may be an empty array. At least one piece of evidence must be present.

An optional `tenant` (the owner/repo data repository the run is for) records the agent's model usage against that tenant in the usage ledger. It isn't sent to the model. The system-summary manifest orchestrator always passes it.

```bash
python3 -m unittest discover -s src/system-summary-generator -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy system-summary-generator --no-prompt
```
