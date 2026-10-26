# Service Dependency Workflow

Coordinates deterministic source discovery, model-backed service-dependency extraction, deterministic merging, validation, and optional pull-request publication for one repository path. Any failed generation batch causes the workflow to fail closed.

Alongside the JSON catalog, the workflow deterministically renders a C4-PlantUML diagram from the merged, already-deduplicated `containers`/`dependencies` (no model call involved): each container and each unique dependency `targetId` is declared once, and every dependency row becomes one `Rel()` edge into that declaration -- so a database or queue used by several containers renders as one node with several edges, not one node per row. It is returned as `puml` on each catalog item and, when publishing, written by the PR creator next to the JSON file with a `.puml` extension.

```json
{"sourceUrl":"https://github.com/owner/repository/tree/main/src","targetRepository":"talentconsulting/atlas-dfe"}
```

Set `deferPublication` to `true` when called by the manifest orchestrator.

```bash
python3 -m unittest discover -s src/service-dependency-workflow -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy service-dependency-workflow --no-prompt
```
