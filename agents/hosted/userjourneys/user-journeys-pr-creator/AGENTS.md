# User Journeys PR Creator

This project was built with the microsoft-foundry skill. Before working on or answering questions about Foundry agents, read the microsoft-foundry skill first.

- Keep runtime code under `src/user-journeys-pr-creator/`.
- Never commit GitHub credentials; read `GITHUB_PR_TOKEN` from the environment.
- Commit every catalog, its Markdown rendering and the optional manifest update together, then create at most one branch and pull request.
- With `manifestNodes`, apply only those manifest nodes to the current base-branch manifest.
- Never approve or merge a pull request.
- Run the unit tests before deployment.
