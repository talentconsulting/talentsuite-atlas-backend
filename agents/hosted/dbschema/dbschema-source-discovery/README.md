# Database Schema Source Discovery

Deterministically selects database DDL, migrations, ORM mappings, contexts, entities, and named types beneath one public GitHub tree URL. It returns `schemaFiles` as same-repository, same-ref GitHub blob URLs and reports safety-limit exclusions in `excludedFiles`. No model is used.

When the tree contains a SQL database project (a `.sqlproj`) whose folder holds `.sql` table scripts, only that project's files are returned. Entity Framework mappings of the same tables use the class names and no schema, so describing both would turn one table into several; the other files are listed in `excludedFiles` with the reason `covered_by_database_project`.

```json
{"sourceUrl":"https://github.com/owner/repository/tree/main/src/Database"}
```

Deploy with:

```bash
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy dbschema-source-discovery --no-prompt
```
