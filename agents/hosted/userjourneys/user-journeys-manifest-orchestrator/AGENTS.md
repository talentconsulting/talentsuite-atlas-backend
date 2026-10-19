# User Journeys Manifest Orchestrator

This project was built with the microsoft-foundry skill. Before working on or answering questions about Foundry agents, read the microsoft-foundry skill first.

- Keep runtime code under `src/user-journeys-manifest-orchestrator/`.
- Process only `user-journeys` manifest nodes, invoking `user-journeys-workflow` in deferred mode once per changed repository.
- Update a repository's commit hash only when its workflow succeeded.
- Publish all catalogs and the manifest update through `user-journeys-pr-creator` in one pull request, sending `manifestNodes`.
- Run the unit tests before deployment.
