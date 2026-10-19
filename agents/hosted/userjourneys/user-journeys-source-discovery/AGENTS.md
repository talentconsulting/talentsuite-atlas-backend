# User Journeys Source Discovery

This project was built with the microsoft-foundry skill. Before working on or answering questions about Foundry agents, read the microsoft-foundry skill first.

- Keep runtime code under `src/user-journeys-source-discovery/`; the extractor is `navigation.py`.
- Preserve the exact one-property `sourceUrl` input contract.
- Stay deterministic: no model calls. Every transition must cite the file and line it came from.
- Return failures as `{"error": {...}}`, never as an empty graph.
- Never return secret values or file contents, only the graph, file locations and exclusion reasons.
- Run unit tests before deployment.
