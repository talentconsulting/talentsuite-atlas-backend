# User Journeys Source Discovery

Deterministically builds the navigation graph of the ASP.NET Core web projects (MVC controllers and Razor Pages) beneath one GitHub tree URL. No model is used.

- **Pages** are named routes (`RouteNames.X`, route constants on controllers, or `Controller.Action` / Razor page paths when unnamed). A GET and POST with the same route name are one page.
- **Transitions** are `RedirectToRoute`/`RedirectToAction`/`RedirectToPage` calls (both sides of `cond ? A : B`), `Url.RouteUrl` links, and `asp-route`/`asp-action`/`asp-page` links and form posts in the views each action renders, following partials. Each transition lists every file and line that evidences it.
- **Site navigation** is the layout's header and footer links, recorded once rather than as a transition from every page.
- **Sign-in** comes from `[Authorize]`/`[AllowAnonymous]` and app-wide rules (`AuthorizeFolder("/")`, a global `AuthorizeFilter`, a fallback policy). With no rule in the repository it is `null`, because it may come from a package.
- Stub, health-check and error pages are left out. References only known at runtime (for example `Model.BackLink`) are listed under `unresolved`.

It also groups pages into `batches` of the files that define them, for the generator to describe.

```json
{"sourceUrl":"https://github.com/owner/repository/tree/main/src"}
```

```json
{"repository":"owner/repository","ref":"main","path":"src","webProjects":["src/App.Web"],
 "graph":{"pages":[{"id":"Apply","kind":"mvc-action","routes":["applications/{id}"],"methods":["GET"],"sourceFile":"src/App.Web/Controllers/ApplyController.cs","line":12,"requiresAuthentication":true,"isEntryPoint":false,"purpose":null}],
          "transitions":[{"from":"Apply","to":"ApplyApprenticeship.Jobs","trigger":"link","condition":null,"evidence":["src/App.Web/Views/Apply/Index.cshtml:3"]}],
          "siteNavigation":[],"unresolved":[],"selfLinkCount":0},
 "batches":[{"pageIds":["Apply"],"files":["https://github.com/owner/repository/blob/main/src/App.Web/Controllers/ApplyController.cs"]}],
 "excludedFiles":[]}
```

Failures are returned as `{"error": {"code": "...", "message": "..."}}`, never as an empty graph.

```bash
python3 -m unittest discover -s src/user-journeys-source-discovery -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy user-journeys-source-discovery --no-prompt
```
