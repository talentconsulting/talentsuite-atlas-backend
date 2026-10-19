# User Journeys Workflow

Runs the user-journeys pipeline for one web repository:

1. `user-journeys-source-discovery` builds the navigation graph deterministically (pages, transitions with source evidence, sign-in, entry points) and groups pages into batches.
2. `user-journeys-generator` in `describe` mode adds a purpose to each page and a condition to each transition, one batch per call, concurrently.
3. `user-journeys-generator` in `journeys` mode names the user journeys; steps that do not follow transitions are rejected.
4. Unless `deferPublication` is true, `user-journeys-pr-creator` writes `<repo>/user-journeys/user-journeys.json` and `user-journeys.md`.

```json
{"sourceUrl":"https://github.com/owner/repository/tree/main/src","targetRepository":"owner/catalogue"}
```

A failed batch leaves its pages and transitions undescribed and is listed in `generationErrors`. If every batch fails (for example a model outage), or discovery fails, the run fails so that a manifest run does not record the commit as scanned. A repository with no web projects produces an empty catalog.

```bash
python3 -m unittest discover -s src/user-journeys-workflow -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy user-journeys-workflow --no-prompt
```
