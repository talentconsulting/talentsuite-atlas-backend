# User Journeys Workflow

This project was built with the microsoft-foundry skill. Before working on or answering questions about Foundry agents, read the microsoft-foundry skill first.

- Keep runtime code under `src/user-journeys-workflow/`.
- Call discovery first and treat its graph as the source of truth; the generator only describes it and names journeys.
- Fail the run when discovery fails or every describe batch fails, so nothing undescribed or empty is recorded as scanned.
- In deferred mode, return the catalog without invoking the PR creator; in direct mode, publish only through `user-journeys-pr-creator`.
- Run the unit tests before deployment.
