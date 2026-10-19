# User Journeys Generator

Uses the model to describe a navigation graph that `user-journeys-source-discovery` extracted deterministically, and to name its user journeys. Output is validated strictly against the supplied graph: the model can describe pages and transitions but never add them, and journey steps must follow real transitions.

## describe

One batch of pages (with their form fields), their transitions and the GitHub blob URLs of the files that define them. Returns a one-sentence `purpose` per page, a `condition` per transition and a `validation` summary per field (or `null`), using only rules visible in the supplied code.

```json
{"mode":"describe","sourceUrl":"https://github.com/owner/repository/tree/main/src",
 "pages":[{"id":"Apply","routes":["applications/{id}"],"kind":"mvc-action","sourceFile":"src/App.Web/Controllers/ApplyController.cs"}],
 "transitions":[{"from":"Apply","to":"Preview","trigger":"link"}],
 "sourceFiles":["https://github.com/owner/repository/blob/main/src/App.Web/Controllers/ApplyController.cs"]}
```

## journeys

The whole described graph, without source. Returns up to 15 named journeys, either `linear` (ordered `steps`) or `hub` (a task list: `steps` ending at the `hub`, the `sections` it links to, and the path it continues on, `then`). Journeys whose steps do not follow transitions in the graph are dropped and listed in `warnings`.

```json
{"mode":"journeys","repository":"owner/repository","pages":[...],"transitions":[...]}
```

```json
{"journeys":[{"name":"Apply for an apprenticeship","description":"...","kind":"hub","steps":["Vacancy","Apply"],"hub":"Apply","sections":["Jobs","Qualifications"],"then":["Preview","Submitted"]}],"warnings":[]}
```

```bash
python3 -m unittest discover -s src/user-journeys-generator -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy user-journeys-generator --no-prompt
```
